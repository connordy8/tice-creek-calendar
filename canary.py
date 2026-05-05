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


WEEKDAY_MIN_CLASSES = 5  # Tice Creek normally has 10+ per weekday
WEEK_MIN_BETH_MATCHES = 5  # Across a full week she normally has 10+

# Coverage check: every day WITHIN MINDBODY'S BOOKING WINDOW (next 7
# days) should have >= 1 calendar event. We don't check beyond 7 days
# because Tice Creek's reservation window hasn't opened — those days
# WILL fill in as the window slides forward. Alerting about days
# beyond the window would produce false positives every single day.
COVERAGE_DAYS = 7
DAYS_EMPTY_TOLERANCE = 0  # any empty day in the booking window = red flag


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
    empty_days = []
    for offset in range(COVERAGE_DAYS):
        d = today + timedelta(days=offset)
        ds = d.strftime("%Y-%m-%d")
        weekday = d.strftime("%A")
        n = len(by_date.get(ds, []))
        log.info("  cal coverage {} {} → {} events".format(
            weekday, ds, n))
        if n == 0:
            empty_days.append("{} {}".format(weekday, ds))

    alerts = []
    if len(empty_days) > DAYS_EMPTY_TOLERANCE:
        alerts.append(
            "Beth's calendar has {} empty day(s) in the next {} days: "
            "{}. Every day should have at least one event (class, "
            "appointment, or drop-in). Check that auto-book is "
            "running and matching her preferences correctly."
            .format(len(empty_days), COVERAGE_DAYS,
                    ", ".join(empty_days)))
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
        # Mon-Sat are usually busy; Sundays can be light
        if d.weekday() < 6 and n < WEEKDAY_MIN_CLASSES:
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
