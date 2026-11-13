"""Private Telegram scheduler and Calendly booking attempt for Midtown padel."""
import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timedelta, time
from pathlib import Path
from zoneinfo import ZoneInfo
from playwright.async_api import async_playwright

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

TZ = ZoneInfo('Asia/Dubai')
TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
OWNER = int(os.environ['TELEGRAM_CHAT_ID'])
DB_PATH = os.getenv('DB_PATH', '/data/padel.db')
CALENDLY_URL = 'https://calendly.com/paddle-court-booking/60min'
REQUIRED = ('BOOKING_NAME', 'BOOKING_EMAIL', 'BOOKING_BUILDING', 'BOOKING_UNIT', 'BOOKING_PHONE')
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


def connection():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.execute('CREATE TABLE IF NOT EXISTS bookings (day TEXT PRIMARY KEY, status TEXT NOT NULL)')
    return db


def scheduled_for(day):
    return datetime.combine(day, time(19, 0), TZ) - timedelta(hours=72)


def allowed(update):
    return update.effective_chat and update.effective_chat.id == OWNER


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    await update.message.reply_text('🎾 Midtown Padel\n/book YYYY-MM-DD — queue 7 PM slot\n/upcoming — review attempts\n/cancel YYYY-MM-DD — remove attempt\n/help — instructions\n\nLive booking requires BOOKING_ENABLED=true and all booking variables in Railway.')


async def book(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    try:
        day = datetime.strptime(context.args[0], '%Y-%m-%d').date()
    except (IndexError, ValueError):
        await update.message.reply_text('Use /book YYYY-MM-DD, e.g. /book 2026-11-16')
        return
    opening = scheduled_for(day)
    if opening <= datetime.now(TZ):
        await update.message.reply_text('The 72-hour opening time has already passed for this date.')
        return
    with connection() as db:
        db.execute('INSERT OR REPLACE INTO bookings(day,status) VALUES (?,?)', (day.isoformat(), 'queued'))
    for job in context.job_queue.get_jobs_by_name(day.isoformat()):
        job.schedule_removal()
    schedule_job(context.application, day)
    live = os.getenv('BOOKING_ENABLED', '').lower() == 'true'
    await update.message.reply_text(f'Queued: {day:%A %d %b} at 7:00 PM\nOpening: {opening:%A %d %b, %I:%M %p} Dubai time\nMode: {"LIVE" if live else "DRY RUN"}')


async def upcoming(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    with connection() as db:
        rows = db.execute('SELECT day,status FROM bookings ORDER BY day').fetchall()
    await update.message.reply_text('\n'.join(f'{day}: {status}' for day, status in rows) or 'No scheduled attempts.')


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    if not context.args:
        await update.message.reply_text('Use /cancel YYYY-MM-DD')
        return
    day = context.args[0]
    with connection() as db:
        db.execute('DELETE FROM bookings WHERE day=? AND status="queued"', (day,))
    for job in context.job_queue.get_jobs_by_name(day):
        job.schedule_removal()
    await update.message.reply_text(f'Removed queued attempt for {day} (if present).')


async def execute_attempt(context: ContextTypes.DEFAULT_TYPE):
    day = context.job.data
    with connection() as db:
        status = db.execute('SELECT status FROM bookings WHERE day=?', (day,)).fetchone()
        if not status or status[0] != 'queued':
            return
        db.execute('UPDATE bookings SET status="running" WHERE day=?', (day,))
    try:
        if os.getenv('BOOKING_ENABLED', '').lower() != 'true':
            outcome = 'dry_run'
            message = '🧪 Dry run only; no reservation submitted.'
        else:
            missing = [name for name in REQUIRED if not os.getenv(name)]
            if missing:
                raise RuntimeError('Missing Railway variables: ' + ', '.join(missing))
            await context.bot.send_message(OWNER, f'🎾 Attempting {day} 7:00 PM now…')
            message = await perform_booking(day)
            outcome = 'confirmed' if message.startswith('CONFIRMED:') else 'unverified'
    except Exception as exc:
        log.exception('Booking attempt failed')
        outcome, message = 'failed', f'❌ Booking failed: {type(exc).__name__}: {str(exc)[:350]}'
    with connection() as db:
        db.execute('UPDATE bookings SET status=? WHERE day=?', (outcome, day))
    await context.bot.send_message(OWNER, f'{day} 7 PM — {message}')


async def perform_booking(day):
    """Use the visible booking UI. Never infer success from a button click alone."""
    target = datetime.strptime(day, '%Y-%m-%d').date()
    date_label = target.strftime('%A, %B %-d')
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox'])
        page = await browser.new_page(timezone_id='Asia/Dubai', locale='en-US')
        try:
            await page.goto(f'{CALENDLY_URL}?month={target:%Y-%m}', wait_until='domcontentloaded', timeout=30000)
            date_button = page.get_by_role('button', name=__import__('re').compile(rf'{__import__("re").escape(date_label)}.*', __import__('re').I))
            # A few gentle retries cover the release moment without rapid polling.
            for attempt in range(4):
                try:
                    await date_button.click(timeout=3500)
                    break
                except Exception:
                    if attempt == 3:
                        raise RuntimeError(f'{date_label} unavailable when booking opened')
                    await asyncio.sleep(3)
                    await page.reload(wait_until='domcontentloaded')
            # Calendly uses a button for each offered start time.
            time_button = page.get_by_role('button', name=__import__('re').compile(r'^(7:00\s*pm|19:00)$', __import__('re').I)).first
            await time_button.click(timeout=5000)
            await page.get_by_role('button', name=__import__('re').compile('Next', __import__('re').I)).click(timeout=5000)
            for label, value in (
                ('Name', os.environ['BOOKING_NAME']),
                ('Email', os.environ['BOOKING_EMAIL']),
                ('Building No.', os.environ['BOOKING_BUILDING']),
                ('Unit No.', os.environ['BOOKING_UNIT']),
                ('Number of Guest Players', os.getenv('BOOKING_GUESTS', '3')),
            ):
                await page.get_by_role('textbox', name=__import__('re').compile(__import__('re').escape(label), __import__('re').I)).fill(value, timeout=5000)
            phone = page.get_by_role('textbox', name=__import__('re').compile('Phone No', __import__('re').I))
            await phone.fill(os.environ['BOOKING_PHONE'], timeout=5000)
            await page.get_by_role('button', name='Schedule Event').click(timeout=5000)
            try:
                await page.get_by_text(__import__('re').compile(r'(You are scheduled|Confirmed|Meeting scheduled)', __import__('re').I)).first.wait_for(timeout=15000)
                return 'CONFIRMED: Calendly displayed booking confirmation.'
            except Exception:
                return '⚠️ Submission attempted, but confirmation not verified. Check Calendly confirmation email before retrying.'
        finally:
            await browser.close()


def schedule_job(app, day):
    opening = scheduled_for(day)
    if opening > datetime.now(TZ):
        app.job_queue.run_once(execute_attempt, when=opening, data=day.isoformat(), name=day.isoformat())


async def startup(app):
    with connection() as db:
        rows = db.execute('SELECT day FROM bookings WHERE status="queued"').fetchall()
    for (day,) in rows:
        schedule_job(app, datetime.strptime(day, '%Y-%m-%d').date())


def main():
    app = Application.builder().token(TOKEN).post_init(startup).build()
    app.add_handler(CommandHandler(['start', 'help'], start))
    app.add_handler(CommandHandler('book', book))
    app.add_handler(CommandHandler('upcoming', upcoming))
    app.add_handler(CommandHandler('cancel', cancel))
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
