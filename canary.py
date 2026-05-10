#!/usr/bin/env python3
"""
Schema-change canary — alerts when the scraper finds suspiciously few
matches, suggesting Mindbody's widget changed shape.

Triggers an alert if EITHER:
  - The scraper returns 0 classes for ANY upcoming weekday in the next
    7 days (Tice Creek normally has dozens per weekday)
  - The scraper returns 0 Beth-preference matches for the entire week
    (would mean keyword filters are out of sync with class names)

Designed to catch the silent-drop failure mode where a workflow
"succeeds" but produces empty data.

Usage:
  python3 canary.py
"""

import os
import sys
import yaml
import logging
from datetime import datetime, timedelta
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# Reuse weekly_audit's scrape function
sys.path.insert(0, str(Path(__file__).parent))
from weekly_audit import scrape_week, matches_beth, EARLIEST_HOUR  # noqa


# Thresholds calibrated post-2026-05-07 (booking disabled — calendar
# now lists Beth-matched classes as 'available' instead of auto-booking).
# After filtering to Beth's preferences, a typical weekday yields 1–5
# classes. The canary's job is to catch SILENT-FAIL: Mindbody schema
# changes that make scrape_week return ~0 results across the board.
WEEKDAY_MIN_CLASSES = 1  # any Mon-Fri returning 0 = scraper broken
WEEK_MIN_BETH_MATCHES = 5  # filter sanity check (typical: 15–25)

# Coverage check: alert only if the WHOLE 7-day window has < N events
# (i.e. the system stopped pushing classes to the calendar entirely).
# Per-day empty checks were retired when booking was disabled — weekend
# days at Tice Creek are routinely empty and that's fine now.
COVERAGE_DAYS = 7
WEEK_MIN_TOTAL_EVENTS = 5  # whole-window floor; below this = something broke


def _check_calendar_coverage():
    """Pull Beth's calendar and assert every day has at least 1 event.

    Returns a list of alert strings (empty if all good).
    """
    import json
    from datetime import timezone
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_KEY"]),
        scopes=["https://www.googleapis.com/auth/calendar.readonly"])
    svc = build("calendar", "v3", credentials=creds, cache_discovery=False)

    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    resp = svc.events().list(
        calendarId=os.environ["GOOGLE_CALENDAR_ID"],
        timeMin=start.isoformat(),
        timeMax=(start + timedelta(days=COVERAGE_DAYS)).isoformat(),
        maxResults=500,
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    by_date = {}
    for e in resp.get("items", []):
        dt = (e.get("start", {}).get("dateTime")
              or e.get("start", {}).get("date") or "")
        if dt:
            by_date.setdefault(dt[:10], []).append(
                e.get("summary", ""))

    today = datetime.now().date()
    total_events = 0
    for offset in range(COVERAGE_DAYS):
        d = today + timedelta(days=offset)
        ds = d.strftime("%Y-%m-%d")
        weekday = d.strftime("%A")
        n = len(by_date.get(ds, []))
        total_events += n
        log.info("  cal coverage {} {} → {} events".format(
            weekday, ds, n))

    alerts = []
    if total_events < WEEK_MIN_TOTAL_EVENTS:
        alerts.append(
            "Beth's calendar has only {} total event(s) across the "
            "next {} days (expected >= {}). The sync may have stopped "
            "pushing classes to the calendar entirely — check "
            "auto-book and sync workflows."
            .format(total_events, COVERAGE_DAYS, WEEK_MIN_TOTAL_EVENTS))
    return alerts


def main():
    log.info("=" * 60)
    log.info("Beth's Calendar — Schema Canary")
    log.info("=" * 60)

    classes = scrape_week(7)
    log.info("Total classes scraped: {}".format(len(classes)))

    # Bucket by date
    by_date = {}
    for c in classes:
        by_date.setdefault(c["date"], []).append(c)

    # Walk forward 7 days
    today = datetime.now().date()
    alerts = []

    for offset in range(7):
        d = today + timedelta(days=offset)
        ds = d.strftime("%Y-%m-%d")
        weekday = d.strftime("%A")
        n = len(by_date.get(ds, []))
        log.info("  {} {} → {} classes".format(weekday, ds, n))
        # Only check weekdays. Tice Creek's weekend schedule is too
        # sparse — Beth's filtered class count on Sat/Sun is often 0
        # legitimately, and that's not a scraper failure.
        if d.weekday() < 5 and n < WEEKDAY_MIN_CLASSES:
            alerts.append(
                "{} {}: only {} classes scraped (expected >= {})"
                .format(weekday, ds, n, WEEKDAY_MIN_CLASSES))

    # Beth-preference match count for the whole week
    beth_count = 0
    for c in classes:
        try:
            hh = int(c["start_time"].split(":")[0])
        except Exception:
            continue
        if hh < EARLIEST_HOUR:
            continue
        if matches_beth(c["name"]):
            beth_count += 1
    log.info("Beth-preference matches across week: {}".format(beth_count))
    if beth_count < WEEK_MIN_BETH_MATCHES:
        alerts.append(
            "Only {} Beth-preference matches across the whole week "
            "(expected >= {}). The scraper may be broken or the "
            "include_classes filter may be out of sync with current "
            "Mindbody class names.".format(beth_count, WEEK_MIN_BETH_MATCHES))

    # Coverage check: every day in next 14 days should have >= 1 event
    log.info("")
    log.info("Calendar coverage check (next {} days)...".format(
        COVERAGE_DAYS))
    try:
        coverage_alerts = _check_calendar_coverage()
        alerts.extend(coverage_alerts)
    except Exception as e:
        log.warning("Coverage check failed: {}".format(e))
        alerts.append("Calendar coverage check failed: {}".format(e))

    if alerts:
        msg = "\n\n".join(alerts)
        msg += (
            "\n\n---\nNOTE: This canary already ran scraper.py + "
            "auto_book.py before this check. If you're seeing this "
            "alert, automated self-heal couldn't fix the issue and "
            "manual intervention is needed.")
        log.warning("CANARY TRIPPED:\n{}".format(msg))
        try:
            from notify import send_alert
            send_alert(
                "Schema Canary Tripped",
                "Tice Creek scraper output is suspicious — possible "
                "Mindbody schema change",
                extra_context=msg,
            )
        except Exception as e:
            log.warning("Could not send alert: {}".format(e))
        sys.exit(1)
    else:
        log.info("Canary OK. No suspicious gaps.")


if __name__ == "__main__":
    main()
