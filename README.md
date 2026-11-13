# Midtown Padel Telegram Bot — safe pilot

This starter lets the owner queue individual 7:00 PM playing dates through Telegram. A trigger is scheduled **exactly 72 hours beforehand** in `Asia/Dubai`, including after restarts. **It currently reports a dry-run; it does not book a court.** The live Calendly page showed no available November dates during inspection, so its date/time selection and booking form could not be safely verified.

## Railway configuration

1. Deploy this repository using its `Dockerfile`.
2. Set service variables `TELEGRAM_BOT_TOKEN` (from BotFather) and `TELEGRAM_CHAT_ID` (your numeric Telegram chat ID; obtain with a trusted Telegram ID bot, without sharing your token).
3. Add a Railway persistent volume mounted at `/data` so queued dates survive restarts/redeploys.
4. Keep the service at **one replica**. No public domain is required because Telegram is polled.
5. Send `/start` to your bot; then `/book 2026-11-20`, `/upcoming`, or `/cancel 2026-11-20`.

## Before enabling real bookings

- Inspect an actually available Calendly slot and its full form, including any mandatory custom fields, using the authorized registered email.
- Add and verify Playwright booking logic with an explicit submission toggle and at least one supervised real booking.
- Respect the property's one-booking-per-day limit; ensure no lottery or rule change has superseded first-come-first-served.
- The 60-minute link is not automatically assumed to support the 7:00 PM slot.
- Consider monitored failure alerts, retries without aggressive request rates, and a fallback slot policy.

**Never commit your Telegram token, booking email or private form details to GitHub.** Store them in Railway variables only.
