"""Private Telegram scheduler and Calendly booking attempt for Midtown padel."""
import asyncio
import logging
import os
import re
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
CALENDLY_URL = os.getenv('CALENDLY_URL', 'https://calendly.com/mesk-paddel-court/60min').split('?')[0].rstrip('/')
VERSION = 'v4.7-live-2026-11-23'
PREWARM_SECONDS = 30
RETRY_WINDOW_SECONDS = 22
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
    await update.message.reply_text(f'🎾 Midtown Padel {VERSION}\n/book YYYY-MM-DD — queue 7 PM slot\n/upcoming — review attempts\n/cancel YYYY-MM-DD — remove attempt\n/test YYYY-MM-DD — check Calendly form (never submits)\n/help — instructions\n\nLive booking requires BOOKING_ENABLED=true and all booking variables in Railway.')


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
            await context.bot.send_message(OWNER, f'🎾 {VERSION}: preparing {day} 7:00 PM booking…')
            message = await perform_booking(day, scheduled_for(datetime.strptime(day, '%Y-%m-%d').date()))
            outcome = ('confirmed' if message.startswith('CONFIRMED:')
                       else 'failed' if message.startswith('❌') else 'unverified')
    except Exception as exc:
        log.exception('Booking attempt failed')
        outcome, message = 'failed', f'❌ Booking failed: {type(exc).__name__}: {str(exc)[:350]}'
    with connection() as db:
        db.execute('UPDATE bookings SET status=? WHERE day=?', (outcome, day))
    await context.bot.send_message(OWNER, f'{day} 7 PM — {message}')


async def advance_from_time_to_form(page):
    """Advance Calendly's selected time using its visible Next control.

    The Next element can have a different accessible name from its inner text,
    so checking get_by_role(button, name='Next') alone is insufficient.
    """
    submit = page.get_by_role('button', name=re.compile(r'Schedule Event', re.I)).first
    if await submit.count() and await submit.is_visible():
        return 'already on form'

    # Wait for Calendly's selected-slot UI to settle before looking for Next.
    await page.wait_for_timeout(300)
    attempts = [
        ('text: Next', page.get_by_text('Next', exact=True).first),
        ('button containing Next', page.locator('button').filter(has_text=re.compile(r'^\s*Next\s*$', re.I)).first),
        ('role button with Next', page.get_by_role('button', name=re.compile(r'Next', re.I)).first),
        ('text: Confirm', page.get_by_text('Confirm', exact=True).first),
        ('text: Continue', page.get_by_text('Continue', exact=True).first),
    ]
    failures = []
    for label, candidate in attempts:
        try:
            await candidate.wait_for(state='visible', timeout=1100)
            await candidate.click(timeout=3000)
            try:
                await submit.wait_for(state='visible', timeout=9000)
                return f'clicked {label}; form visible'
            except Exception:
                return f'clicked {label}; form not yet visible'
        except Exception as exc:
            failures.append(f'{label}: {type(exc).__name__}')
    return 'no usable Next/Confirm found (' + '; '.join(failures[:3]) + ')'


async def perform_booking(day, opening):
    """Prewarm the browser, then check availability at release; report what was seen."""
    target = datetime.strptime(day, '%Y-%m-%d').date()
    date_label = target.strftime('%A, %B %-d')
    started = datetime.now(TZ)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox'])
        page = await browser.new_page(timezone_id='Asia/Dubai', locale='en-US')
        try:
            await page.goto(f'{CALENDLY_URL}?month={target:%Y-%m}', wait_until='domcontentloaded', timeout=30000)
            date_button = page.get_by_role('button', name=re.compile(rf'{re.escape(date_label)}.*', re.I)).first
            # Select in advance where possible. Do not wait past release to prewarm.
            try:
                await date_button.click(timeout=1800)
            except Exception:
                pass
            remaining = (opening - datetime.now(TZ)).total_seconds()
            if remaining > 0:
                await asyncio.sleep(remaining)
            # Reload once at release to force a current availability check.
            await page.reload(wait_until='domcontentloaded', timeout=15000)
            deadline = asyncio.get_running_loop().time() + RETRY_WINDOW_SECONDS
            visible_times = []
            clicked = False
            while asyncio.get_running_loop().time() < deadline:
                date_button = page.get_by_role('button', name=re.compile(rf'{re.escape(date_label)}.*', re.I)).first
                try:
                    await date_button.click(timeout=1200)
                except Exception:
                    await asyncio.sleep(0.8)
                    await page.reload(wait_until='domcontentloaded', timeout=12000)
                    continue
                # Calendly's slot buttons expose a stable data-start-time attribute.
                slots = page.locator('button[data-container="time-button"]')
                visible_times = [t.strip() for t in await slots.all_text_contents()]
                time_button = slots.filter(has_text=re.compile(r'^\s*(?:7:00\s*pm|19:00)\s*$', re.I)).first
                if await time_button.count() and await time_button.is_visible():
                    try:
                        await time_button.click(timeout=1600)
                        clicked = True
                        break
                    except Exception:
                        log.exception('Time slot click failed')
                await asyncio.sleep(1.0)
                await page.reload(wait_until='domcontentloaded', timeout=12000)
            if not clicked:
                sample = ', '.join(visible_times[:18]) or 'none detected'
                return f'❌ 19:00 not selectable after {RETRY_WINDOW_SECONDS}s. Times seen: {sample}. Page load started {started:%H:%M:%S} Dubai.'
            transition = await advance_from_time_to_form(page)
            submit_button = page.get_by_role('button', name=re.compile(r'^Schedule Event$', re.I)).first
            if not await submit_button.count() or not await submit_button.is_visible():
                raise RuntimeError(f'Booking form not displayed after selecting time ({transition})')
            for label, value in (
                ('Name', os.environ['BOOKING_NAME']),
                ('Email', os.environ['BOOKING_EMAIL']),
                ('Building No.', os.environ['BOOKING_BUILDING']),
                ('Unit No.', os.environ['BOOKING_UNIT']),
                ('Number of Guest Players', os.getenv('BOOKING_GUESTS', '3')),
            ):
                await page.get_by_role('textbox', name=re.compile(re.escape(label), re.I)).fill(value, timeout=5000)
            phone = page.get_by_role('textbox', name=re.compile('Phone No', re.I))
            await phone.fill(os.environ['BOOKING_PHONE'], timeout=5000)
            # Only consider a booking confirmed when a dedicated confirmation page is seen.
            await submit_button.click(timeout=7000)
            try:
                await page.wait_for_url(re.compile(r'/(scheduled|confirmation|invitee)/', re.I), timeout=12000)
                return f'CONFIRMED: Calendly navigated to {page.url}'
            except Exception:
                pass
            try:
                confirmation = page.get_by_text(re.compile(r'(You are scheduled|Event confirmed|Booking confirmed|You\'re scheduled)', re.I)).first
                await confirmation.wait_for(state='visible', timeout=4000)
                return 'CONFIRMED: Calendly showed a booking confirmation.'
            except Exception:
                return f'⚠️ Schedule Event clicked; confirmation not verified. Current page: {page.url[:180]}. Check email before attempting again.'
        finally:
            await browser.close()


async def diagnostic_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Inspect public navigation without submitting.
    if not allowed(update):
        return
    try:
        target = datetime.strptime(context.args[0], '%Y-%m-%d').date()
    except (IndexError, ValueError):
        await update.message.reply_text('Use /test YYYY-MM-DD, e.g. /test 2026-11-25')
        return
    if target < datetime.now(TZ).date():
        await update.message.reply_text('Choose today or a future date for /test.')
        return
    await update.message.reply_text(f'🔎 {VERSION}: inspecting {target.isoformat()} without booking…')
    from io import BytesIO
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox'])
        page = await browser.new_page(timezone_id='Asia/Dubai', locale='en-US', viewport={'width': 1100, 'height': 850})
        errors = []
        page.on('pageerror', lambda err: errors.append(str(err)[:160]))
        selected = False
        stage = 'loading calendar'
        landing_status = 'unknown'
        redirect_chain = []
        page_title = 'unknown'
        requested_url = f'{CALENDLY_URL}?month={target:%Y-%m}'
        try:
            response = await page.goto(requested_url, wait_until='domcontentloaded', timeout=30000)
            landing_status = str(response.status) if response else 'no main-document response'
            if response:
                req = response.request
                while req:
                    redirect_chain.append(req.url)
                    req = req.redirected_from
                redirect_chain.reverse()
            await page.wait_for_timeout(2500)
            page_title = await page.title()
            stage = 'finding date'
            # Calendly accessible day labels often include weekday and month; use full date first.
            date_names = [
                target.strftime('%A, %B ') + str(target.day),
                target.strftime('%B ') + str(target.day),
            ]
            for name in date_names:
                match = page.get_by_role('button', name=re.compile(re.escape(name), re.I)).first
                if await match.count():
                    try:
                        await match.click(timeout=2000)
                        selected = True
                        break
                    except Exception as ex:
                        errors.append('Date click: ' + type(ex).__name__)
            if not selected:
                # Sometimes day buttons only show the numeric day with contextual aria-label.
                match = page.locator(f'button[aria-label*="{target.strftime("%B")} {target.day}"]').first
                if await match.count():
                    try:
                        await match.click(timeout=2000)
                        selected = True
                    except Exception as ex:
                        errors.append('Date aria-label click: ' + type(ex).__name__)
            if selected:
                stage = 'reading time choices'
                await page.wait_for_timeout(1800)
            else:
                stage = 'date not selected'
            details = await page.get_by_role('button').evaluate_all('els => els.slice(0, 100).map(e => ({text: (e.innerText || "").trim().slice(0,65), aria: e.getAttribute("aria-label") || "", disabled: e.disabled}))')
            visible = [f'{x["text"] or "(empty)"} | aria={x["aria"][:55]} | disabled={x["disabled"]}' for x in details]
            times = [x for x in details if re.fullmatch(r'\s*(?:[01]?\d|2[0-3]):[0-5]\d\s*(?:am|pm)?\s*', x['text'], re.I) and not x['disabled']]
            chosen_time = 'none'
            form_found = False
            fields_filled = []
            form_labels = []
            transition = 'not attempted'
            transition_snapshot = {}
            post_selection_buttons = []
            if selected and times:
                chosen_time = times[0]['text'].strip()
                try:
                    await page.get_by_role('button', name=chosen_time, exact=True).first.click(timeout=3500)
                    # First inspect the DOM immediately after selecting a time, without assuming
                    # Calendly represents its confirmation control as a button.
                    await page.wait_for_timeout(2000)
                    transition_snapshot = await page.evaluate("""() => ({
                      url: location.href,
                      controls: [...document.querySelectorAll('button,[role=button],input[type=submit],a')].filter(e=>{
                        const r=e.getBoundingClientRect(); return r.width>0 && r.height>0;
                      }).slice(-45).map(e=>({
                        tag:e.tagName.toLowerCase(), role:e.getAttribute('role')||'',
                        text:(e.innerText||e.value||'').trim().replace(/\\s+/g,' ').slice(0,75),
                        aria:(e.getAttribute('aria-label')||'').slice(0,75),
                        disabled:!!e.disabled, html:e.outerHTML.slice(0,280)
                      })),
                      textTail:(document.body.innerText||'').slice(-1500),
                      iframes:document.querySelectorAll('iframe').length
                    })""")
                    transition = await advance_from_time_to_form(page)
                    await page.wait_for_timeout(1400)
                    page_title = await page.title()
                    post_selection_buttons = [x.strip() for x in await page.get_by_role('button').all_text_contents() if x.strip()][:24]
                    form_labels = await page.locator('label').all_text_contents()
                    form_found = await page.get_by_role('button', name=re.compile('Schedule Event', re.I)).count() > 0
                    if not form_found:
                        errors.append('Time selected but Schedule Event button not found')
                        # Preserve a full-resolution view as a Telegram document,
                        # not a compressed photo. No booking details have been filled.
                        shot = BytesIO(await page.screenshot(full_page=True, animations='disabled'))
                        shot.name = 'calendly-after-time-click.png'
                        await context.bot.send_document(chat_id=OWNER, document=shot, caption='Time selected: diagnostic screenshot (no booking submitted)')
                    else:
                        # Fill the form in a private browser session; never send values or a filled-form screenshot.
                        # Do not click Schedule Event under any circumstances in this test.
                        field_pairs = [
                            ('Name', 'BOOKING_NAME'), ('Email', 'BOOKING_EMAIL'),
                            ('Building No.', 'BOOKING_BUILDING'), ('Unit No.', 'BOOKING_UNIT'),
                            ('Number of Guest Players', 'BOOKING_GUESTS'), ('Phone No.', 'BOOKING_PHONE'),
                        ]
                        for label, key in field_pairs:
                            value = os.getenv(key, '3' if key == 'BOOKING_GUESTS' else '')
                            if not value:
                                errors.append(f'{label}: missing Railway variable {key}')
                                continue
                            try:
                                control = page.get_by_role('textbox', name=re.compile(re.escape(label), re.I)).first
                                if not await control.count():
                                    control = page.get_by_label(re.compile(re.escape(label), re.I)).first
                                await control.fill(value, timeout=2500)
                                fields_filled.append(label)
                            except Exception as ex:
                                errors.append(f'{label} field: {type(ex).__name__}')
                    stage = 'booking form checked' if form_found else 'time chosen, form not found'
                except Exception as ex:
                    stage = 'time selection failed'
                    errors.append(f'Time click: {type(ex).__name__}: {str(ex)[:100]}')
            elif selected:
                stage = 'no selectable time found'
            # The screenshot is intentionally captured only when no personal fields have been entered.
            # After filling, report field names only, with no screenshot or values.
            if not fields_filled:
                image = BytesIO(await page.screenshot(full_page=True, animations='disabled'))
                image.name = 'calendly-diagnostic.png'
                await context.bot.send_photo(chat_id=OWNER, photo=image, caption=f'{VERSION}: {target.isoformat()} — {stage}. No booking submitted.')
            report = (f'🔎 Diagnostic for {target.isoformat()}\n'
                      f'Stage: {stage}\nDate clicked: {selected}\n'
                      f'HTTP status: {landing_status}\nPage title: {page_title[:100]}\n'
                      f'Final URL: {page.url[:160]}\n'
                      f'Times detected: {len(times)}\n'
                      f'Chosen test time: {chosen_time}\n'
                      f'Time confirmation: {transition}\n'
                      f'Page text tail: {str(transition_snapshot.get("textTail", ""))[-650:]}\n'
                      f'Visible controls after time click: {str(transition_snapshot.get("controls", []))[-1900:]}\n'
                      f'Iframes: {transition_snapshot.get("iframes", "unknown")}\n'
                      f'Buttons after selection: {", ".join(post_selection_buttons)[:220] or "none"}\n'
                      f'Booking form found: {form_found}\n'
                      f'Fields filled: {", ".join(fields_filled) or "none"}\n'
                      f'Form labels: {", ".join(x.strip() for x in form_labels[:12])[:300] or "none"}\n'
                      f'Errors: {chr(10).join(errors[:8]) or "none"}\n'
                      'SAFE TEST: Schedule Event was NOT clicked. No event submitted.')
            for i in range(0, len(report), 3500):
                await context.bot.send_message(OWNER, report[i:i+3500])
        except Exception as exc:
            await context.bot.send_message(OWNER, f'❌ Diagnostic failed at {stage}: {type(exc).__name__}: {str(exc)[:550]}')
            # No error screenshot: this page might contain private booking details.
        finally:
            await browser.close()


def schedule_job(app, day):
    opening = scheduled_for(day)
    if opening > datetime.now(TZ):
        warmup = opening - timedelta(seconds=PREWARM_SECONDS)
        if warmup <= datetime.now(TZ):
            warmup = datetime.now(TZ) + timedelta(seconds=1)
        app.job_queue.run_once(execute_attempt, when=warmup, data=day.isoformat(), name=day.isoformat())


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
    app.add_handler(CommandHandler('test', diagnostic_test))
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
