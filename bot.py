"""Private Telegram scheduler and Calendly booking attempt for Midtown padel."""
import asyncio
import json
from io import BytesIO
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
VERSION = 'v5.1-release-monitor-2026-11-27'
PREWARM_SECONDS = 45
RETRY_WINDOW_SECONDS = 25
REQUIRED = ('BOOKING_NAME', 'BOOKING_EMAIL', 'BOOKING_BUILDING', 'BOOKING_UNIT', 'BOOKING_PHONE')
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


def connection():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.execute('CREATE TABLE IF NOT EXISTS bookings (day TEXT PRIMARY KEY, status TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS extra_slots (day TEXT NOT NULL, slot TEXT NOT NULL, status TEXT NOT NULL, PRIMARY KEY(day,slot))')
    return db


def scheduled_for(day, slot='19:00'):
    hh, mm = map(int, slot.split(':'))
    return datetime.combine(day, time(hh, mm), TZ) - timedelta(hours=72)


def parse_slot(raw):
    raw = raw.strip()
    try:
        parsed = datetime.strptime(raw, '%H:%M')
    except ValueError:
        raise ValueError('Use 24-hour HH:MM, for example 20:00 or 21:00')
    return parsed.strftime('%H:%M')


def allowed(update):
    return update.effective_chat and update.effective_chat.id == OWNER


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    await update.message.reply_text(f'🎾 Midtown Padel {VERSION}\n/book YYYY-MM-DD — queue 7 PM slot\n/book YYYY-MM-DD 20:00 — queue 8 PM\n/book YYYY-MM-DD 21:00 — queue 9 PM\n/upcoming — review attempts\n/cancel YYYY-MM-DD — remove attempt\n/test YYYY-MM-DD — check Calendly form (never submits)\n/help — instructions\n\nLive booking requires BOOKING_ENABLED=true and all booking variables in Railway.')


async def book(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    try:
        day = datetime.strptime(context.args[0], '%Y-%m-%d').date()
        slot = parse_slot(context.args[1]) if len(context.args) > 1 else '19:00'
    except (IndexError, ValueError) as exc:
        await update.message.reply_text('Use /book YYYY-MM-DD [HH:MM], e.g. /book 2026-11-26 20:00. ' + str(exc))
        return
    opening = scheduled_for(day, slot)
    now = datetime.now(TZ)
    if opening <= now:
        await update.message.reply_text(f'Opening already passed: {opening:%A %d %b, %I:%M %p} Dubai time. No attempt queued.')
        return
    with connection() as db:
        if slot == '19:00':
            db.execute('INSERT OR REPLACE INTO bookings(day,status) VALUES (?,?)', (day.isoformat(), 'queued'))
        else:
            db.execute('INSERT OR REPLACE INTO extra_slots(day,slot,status) VALUES (?,?,?)', (day.isoformat(),slot,'queued'))
    name = day.isoformat() if slot == '19:00' else f'slot:{day}:{slot}'
    for job in context.job_queue.get_jobs_by_name(name):
        job.schedule_removal()
    schedule_job(context.application, day, slot)
    live = os.getenv('BOOKING_ENABLED', '').lower() == 'true'
    await update.message.reply_text(f'Queued: {day:%A %d %b} at {opening + timedelta(hours=72):%I:%M %p}\nOpening: {opening:%A %d %b, %I:%M %p} Dubai time\nMode: {"LIVE" if live else "DRY RUN"}\nOne booking per day: confirmed earlier slot will stop later attempts.')


async def upcoming(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    with connection() as db:
        rows = [(day,'19:00',status) for day,status in db.execute('SELECT day,status FROM bookings').fetchall()]
        rows += db.execute('SELECT day,slot,status FROM extra_slots').fetchall()
    rows.sort(key=lambda r: (r[0], r[1]))
    await update.message.reply_text('\n'.join(f'{day} {slot}: {status}' for day, slot, status in rows) or 'No scheduled attempts.')


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    try:
        day = datetime.strptime(context.args[0], '%Y-%m-%d').date().isoformat()
        slot = parse_slot(context.args[1]) if len(context.args) > 1 else None
    except (IndexError, ValueError):
        await update.message.reply_text('Use /cancel YYYY-MM-DD [HH:MM]. Without a time, all queued slots for that date are cancelled.')
        return
    with connection() as db:
        if slot is None or slot == '19:00':
            db.execute('DELETE FROM bookings WHERE day=? AND status="queued"', (day,))
        if slot is None:
            db.execute('DELETE FROM extra_slots WHERE day=? AND status="queued"', (day,))
        elif slot != '19:00':
            db.execute('DELETE FROM extra_slots WHERE day=? AND slot=? AND status="queued"', (day,slot))
    names = [day, *[f'slot:{day}:{t}' for t in ('20:00','21:00')]] if slot is None else [day if slot=='19:00' else f'slot:{day}:{slot}']
    for name in names:
        for job in context.job_queue.get_jobs_by_name(name):
            job.schedule_removal()
    await update.message.reply_text(f'Removed queued attempt(s) for {day}' + (f' {slot}' if slot else '') + ' (if present).')


async def execute_attempt(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    day, slot = (data if isinstance(data, tuple) else (data,'19:00'))
    table = 'bookings' if slot == '19:00' else 'extra_slots'
    with connection() as db:
        if slot == '19:00':
            row = db.execute('SELECT status FROM bookings WHERE day=?', (day,)).fetchone()
        else:
            row = db.execute('SELECT status FROM extra_slots WHERE day=? AND slot=?', (day,slot)).fetchone()
        if not row or row[0] != 'queued':
            return
        # One confirmed reservation per playing date: do not book another time that day.
        confirmed_legacy = db.execute('SELECT 1 FROM bookings WHERE day=? AND status="confirmed"', (day,)).fetchone()
        confirmed_extra = db.execute('SELECT 1 FROM extra_slots WHERE day=? AND status="confirmed" LIMIT 1', (day,)).fetchone()
        if confirmed_legacy or confirmed_extra:
            if slot == '19:00':
                db.execute('UPDATE bookings SET status="skipped_existing_booking" WHERE day=?', (day,))
            else:
                db.execute('UPDATE extra_slots SET status="skipped_existing_booking" WHERE day=? AND slot=?', (day,slot))
            await context.bot.send_message(OWNER, f'{day} {slot} skipped: another slot was already confirmed for this date.')
            return
        if slot == '19:00':
            db.execute('UPDATE bookings SET status="running" WHERE day=?', (day,))
        else:
            db.execute('UPDATE extra_slots SET status="running" WHERE day=? AND slot=?', (day,slot))
    try:
        if os.getenv('BOOKING_ENABLED','').lower() != 'true':
            outcome, message = 'dry_run', '🧪 Dry run only; no reservation submitted.'
        else:
            missing = [name for name in REQUIRED if not os.getenv(name)]
            if missing:
                raise RuntimeError('Missing Railway variables: ' + ', '.join(missing))
            await context.bot.send_message(OWNER, f'🎾 {VERSION}: preparing {day} {slot} booking…')
            opening = scheduled_for(datetime.strptime(day,'%Y-%m-%d').date(),slot)
            message = await perform_booking(day, opening, slot)
            outcome = 'confirmed' if message.startswith('CONFIRMED:') else 'failed' if message.startswith('❌') else 'unverified'
    except Exception as exc:
        log.exception('Booking attempt failed')
        outcome, message = 'failed', f'❌ Booking failed: {type(exc).__name__}: {str(exc)[:350]}'
    with connection() as db:
        if slot == '19:00':
            db.execute('UPDATE bookings SET status=? WHERE day=?', (outcome, day))
        else:
            db.execute('UPDATE extra_slots SET status=? WHERE day=? AND slot=?', (outcome,day,slot))
        # If booking appears successful or may have submitted, stop other slots.
        if outcome in ('confirmed', 'unverified'):
            db.execute('UPDATE bookings SET status="cancelled_after_submission" WHERE day=? AND status="queued"', (day,))
            db.execute('UPDATE extra_slots SET status="cancelled_after_submission" WHERE day=? AND status="queued"', (day,))
    await context.bot.send_message(OWNER, f'{day} {slot} — {message}')


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


async def perform_booking(day, opening, slot="19:00"):
    """Observe a booking release and refresh *even when daytime slots are visible*.

    Logs contain only public date/time availability, no booking form values.
    Booking and confirmation checks are kept from the working V5 code.
    """
    target = datetime.strptime(day, '%Y-%m-%d').date()
    requested = f'{CALENDLY_URL}?month={target:%Y-%m}&date={day}'
    hh, mm = map(int, slot.split(':'))
    display = f'{hh % 12 or 12}:{mm:02d} {"am" if hh < 12 else "pm"}'
    started = datetime.now(TZ)
    events = []
    latest_times = []
    latest_state = 'unknown'
    latest_http = 'unknown'
    latest_title = 'unknown'
    tries = 0
    refreshes = 0
    previous_signature = None
    release_shot = None
    end_shot = None
    confirmed_submit = False
    perf = {}

    def log_event(kind, **details):
        events.append({'at': datetime.now(TZ).isoformat(timespec='milliseconds'),
                       'kind': kind, **details})

    async def slot_snapshot(page, stage):
        nonlocal latest_times, latest_state, latest_title, previous_signature
        latest_title = (await page.title())[:90]
        # Query the real slot elements, not every button (which includes dates).
        latest_times = [x.strip() for x in await page.locator('button[data-container="time-button"]').all_text_contents()]
        state = await page.evaluate("""() => {
          const selected = document.querySelector('button[aria-pressed="true"]');
          const target = [...document.querySelectorAll('button[aria-label]')]
            .find(x => (x.getAttribute('aria-label') || '').includes('DATE_LABEL'));
          return {dateButtonPresent: !!target, dateButtonDisabled: target ? !!target.disabled : null,
            selectedDateText: selected ? (selected.getAttribute('aria-label') || selected.innerText || '').slice(0,100) : null};
        }""".replace('DATE_LABEL', f'{target:%B} {target.day}'))
        latest_state = str(state)
        signature = (tuple(latest_times), latest_title)
        if signature != previous_signature:
            log_event('availability_change', stage=stage, times=latest_times[:25],
                      title=latest_title, date_state=state)
            previous_signature = signature
        return latest_times

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox'])
        page = await browser.new_page(timezone_id='Asia/Dubai', locale='en-US', viewport={'width': 1100, 'height': 850})
        try:
            response = await page.goto(requested, wait_until='domcontentloaded', timeout=30000)
            latest_http = response.status if response else 'unknown'
            log_event('preloaded', url=page.url, http=latest_http)
            try:
                await page.locator('button[data-container="time-button"]').first.wait_for(state='visible', timeout=3500)
            except Exception:
                pass
            await slot_snapshot(page, 'preopening')
            remaining = (opening - datetime.now(TZ)).total_seconds()
            if remaining > 0:
                await asyncio.sleep(remaining)
            perf['release'] = datetime.now(TZ)
            log_event('release', lag_ms=round((perf['release'] - opening).total_seconds() * 1000))
            # A full reload forces Calendly to request availability after release.
            # The old version never reloaded when daytime slots were already visible.
            deadline = asyncio.get_running_loop().time() + RETRY_WINDOW_SECONDS
            next_reload_at = 0.0
            selected = False
            while asyncio.get_running_loop().time() < deadline:
                tries += 1
                now_loop = asyncio.get_running_loop().time()
                if now_loop >= next_reload_at:
                    try:
                        begin = asyncio.get_running_loop().time()
                        response = await page.reload(wait_until='domcontentloaded', timeout=8000)
                        refreshes += 1
                        latest_http = response.status if response else 'unknown'
                        log_event('reloaded', sequence=refreshes, duration_ms=round((asyncio.get_running_loop().time() - begin)*1000), http=latest_http)
                    except Exception as exc:
                        log_event('reload_error', error=type(exc).__name__)
                    # At most one full reload every ~1.5 s, regardless of times found.
                    next_reload_at = asyncio.get_running_loop().time() + 1.5
                try:
                    # Date query param usually auto-selects, but sometimes slots need a click.
                    slots = page.locator('button[data-container="time-button"]')
                    if await slots.count() == 0:
                        day_button = page.get_by_role('button', name=re.compile(rf'{target:%A}, {target:%B} {target.day}', re.I)).first
                        if await day_button.count() and await day_button.is_enabled():
                            await day_button.click(timeout=1500)
                    try:
                        await slots.first.wait_for(state='visible', timeout=700)
                    except Exception:
                        pass
                    await slot_snapshot(page, f'release_check_{tries}')
                    time_button = page.locator('button[data-container="time-button"]').filter(
                        has_text=re.compile(r'^\s*' + re.escape(display) + r'\s*$', re.I)).first
                    if await time_button.count() and await time_button.is_visible() and await time_button.is_enabled():
                        await time_button.click(timeout=1800)
                        perf['slot_clicked'] = datetime.now(TZ)
                        log_event('slot_clicked', slot=slot, elapsed_ms=round((perf['slot_clicked']-perf['release']).total_seconds()*1000))
                        selected = True
                        break
                except Exception as exc:
                    log_event('scan_error', error=type(exc).__name__, snippet=str(exc)[:130])
                await asyncio.sleep(0.2)

            if not selected:
                log_event('not_selected', slot=slot, checks=tries, refreshes=refreshes, latest_times=latest_times)
                # This snapshot contains only public date/time choices; no form details.
                try:
                    end_shot = await page.screenshot(full_page=True, animations='disabled')
                except Exception:
                    pass
                summary = (f'❌ {slot} not found after {RETRY_WINDOW_SECONDS}s; '
                           f'times: {", ".join(latest_times[:16]) or "none"}; '
                           f'reloads: {refreshes}; checks: {tries}; HTTP: {latest_http}; '
                           f'title: {latest_title}; page: {page.url[:130]}. '
                           'Release log attached.')
                return summary

            transition = await advance_from_time_to_form(page)
            submit = page.get_by_role('button', name=re.compile(r'^Schedule Event$', re.I)).first
            if not await submit.count() or not await submit.is_visible():
                log_event('form_missing', transition=transition, url=page.url)
                return f'❌ Time selected but form missing ({transition}); release log attached.'
            for label, value in (
                ('Name', os.environ['BOOKING_NAME']),
                ('Email', os.environ['BOOKING_EMAIL']),
                ('Building No.', os.environ['BOOKING_BUILDING']),
                ('Unit No.', os.environ['BOOKING_UNIT']),
                ('Number of Guest Players', os.getenv('BOOKING_GUESTS', '3')),
                ('Phone No.', os.environ['BOOKING_PHONE']),
            ):
                await page.get_by_role('textbox', name=re.compile(re.escape(label), re.I)).fill(value, timeout=4000)
            perf['form_filled'] = datetime.now(TZ)
            log_event('form_filled', elapsed_ms=round((perf['form_filled']-perf['release']).total_seconds()*1000))
            await submit.click(timeout=7000)
            confirmed_submit = True
            perf['submitted'] = datetime.now(TZ)
            log_event('schedule_event_clicked', elapsed_ms=round((perf['submitted']-perf['release']).total_seconds()*1000))
            metrics = (f'slot_ms={(perf["slot_clicked"]-perf["release"]).total_seconds()*1000:.0f}, '
                       f'submit_ms={(perf["submitted"]-perf["release"]).total_seconds()*1000:.0f}')
            try:
                await page.wait_for_url(re.compile(r'/(scheduled|confirmation|invitee)/', re.I), timeout=12000)
                log_event('confirmation_url', url=page.url[:170])
                return f'CONFIRMED: Calendly navigated to {page.url}; {metrics}'
            except Exception:
                pass
            try:
                confirmation = page.get_by_text(re.compile(r'(You are scheduled|Event confirmed|Booking confirmed|You\'re scheduled)', re.I)).first
                await confirmation.wait_for(state='visible', timeout=4000)
                log_event('confirmation_text')
                return f'CONFIRMED: Calendly displayed confirmation; {metrics}'
            except Exception:
                log_event('confirmation_not_verified', url=page.url[:170])
                return f'⚠️ Schedule Event clicked but confirmation unverified. {metrics}. Check email before retrying.'
        finally:
            # Persist evidence even when an unexpected error interrupts the attempt.
            # Never send screenshots of filled-in personal information.
            try:
                log_event('finished', submitted=confirmed_submit)
                output = Path('/data/diagnostics')
                output.mkdir(parents=True, exist_ok=True)
                filename = f'{day}-{slot.replace(":", "")}-{datetime.now(TZ):%H%M%S}.json'
                payload = json.dumps({'version':VERSION, 'day':day, 'slot':slot,
                                      'requested_url':requested, 'events':events}, ensure_ascii=False, indent=2)
                (output / filename).write_text(payload)
                # Send only public slot information; no names/emails/phone numbers.
                bio = BytesIO(payload.encode('utf-8'))
                bio.name = filename
                await _notify_diagnostics(bio, end_shot)
            except Exception:
                log.exception('Diagnostic report delivery failed')
            await browser.close()


async def _notify_diagnostics(log_file, screenshot=None):
    # Use Telegram bot API directly to avoid requiring JobQueue context in perform_booking.
    from telegram import Bot
    async with Bot(TOKEN) as bot:
        await bot.send_document(chat_id=OWNER, document=log_file,
                                caption=f'{VERSION}: timestamped release log')
        if screenshot:
            img = BytesIO(screenshot)
            img.name = 'calendar-after-release.png'
            await bot.send_document(chat_id=OWNER, document=img,
                                    caption='Calendar after unsuccessful attempt; no personal booking details')


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


def schedule_job(app, day, slot='19:00'):
    opening = scheduled_for(day, slot)
    if opening > datetime.now(TZ):
        warmup = opening - timedelta(seconds=PREWARM_SECONDS)
        if warmup <= datetime.now(TZ):
            warmup = datetime.now(TZ) + timedelta(seconds=1)
        name = day.isoformat() if slot == '19:00' else f'slot:{day}:{slot}'
        app.job_queue.run_once(execute_attempt, when=warmup, data=(day.isoformat(),slot), name=name)


async def startup(app):
    with connection() as db:
        rows = [(day,'19:00') for (day,) in db.execute('SELECT day FROM bookings WHERE status="queued"').fetchall()]
        rows += db.execute('SELECT day,slot FROM extra_slots WHERE status="queued"').fetchall()
    for day,slot in rows:
        schedule_job(app, datetime.strptime(day,'%Y-%m-%d').date(), slot)


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
