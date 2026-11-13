"""Private Telegram date selector for Midtown padel court booking.

Safe pilot: scheduled attempts are DRY RUN ONLY until the Calendly form is
inspected and its final submission logic explicitly implemented and tested.
"""
import logging
import os
import sqlite3
from datetime import datetime, timedelta, time
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

TZ = ZoneInfo('Asia/Dubai')
TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
OWNER = int(os.environ['TELEGRAM_CHAT_ID'])
DB_PATH = os.getenv('DB_PATH', '/data/padel.db')
CALENDLY_URL = 'https://calendly.com/paddle-court-booking/60min?month=2026-11'
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
    await update.message.reply_text('🎾 Midtown Padel pilot\n/book YYYY-MM-DD — queue 7 PM slot\n/upcoming — review attempts\n/cancel YYYY-MM-DD — remove attempt\n/help — instructions\n\n⚠️ Booking submissions are NOT enabled yet.')


async def book(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    try:
        day = datetime.strptime(context.args[0], '%Y-%m-%d').date()
    except (IndexError, ValueError):
        await update.message.reply_text('Use /book YYYY-MM-DD, e.g. /book 2026-11-20')
        return
    opening = scheduled_for(day)
    if opening <= datetime.now(TZ):
        await update.message.reply_text('The 72-hour opening time has already passed for this date.')
        return
    with connection() as db:
        db.execute('INSERT OR REPLACE INTO bookings(day,status) VALUES (?,?)', (day.isoformat(), 'queued'))
    schedule_job(context.application, day)
    await update.message.reply_text(f'Queued: {day:%A %d %b} at 7:00 PM\nOpening: {opening:%A %d %b, %I:%M %p} Dubai time\n⚠️ Dry-run only until booking integration is validated.')


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
        db.execute('UPDATE bookings SET status="dry_run" WHERE day=?', (day,))
    await context.bot.send_message(OWNER, f'🧪 Dry-run triggered for {day} at 7 PM.\nCalendly: {CALENDLY_URL}\nNo booking was submitted. Booking step still needs form validation.')


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
