#!/usr/bin/env python3
"""Calendar healthcheck — validates Beth's calendar is healthy.

Checks:
1. Google Calendar has events in the next 7 days
2. Events include fitness classes (not just movies)
3. ICS file is accessible via GitHub Pages
4. No duplicate events

Sends an alert if any check fails.
"""

import json
import logging
import os
import sys
import urllib.request
from datetime import datetime, timedelta

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


def check_google_calendar():
    """Verify Beth's Google Calendar has upcoming events."""
    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_KEY")
    calendar_id = os.environ.get("GOOGLE_CALENDAR_ID")

    if not creds_json or not calendar_id:
        return False, "Google Calendar credentials not configured"

    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds_info = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(
        creds_info,
        scopes=["https://www.googleapis.com/auth/calendar.readonly"])
    service = build(
        "calendar", "v3", credentials=creds, cache_discovery=False)

    now = datetime.utcnow()
    time_min = now.isoformat() + "Z"
    time_max = (now + timedelta(days=7)).isoformat() + "Z"

    try:
        resp = service.events().list(
            calendarId=calendar_id,
            timeMin=time_min,
            timeMax=time_max,
            maxResults=100,
            singleEvents=True,
            orderBy="startTime",
        ).execute()
    except Exception as e:
        return False, "Google Calendar API error: {}".format(e)

    events = resp.get("items", [])
    if not events:
        return False, "No events found in the next 7 days"

    # Check for fitness classes (emoji indicators)
    fitness_count = sum(
        1 for e in events
        if any(marker in e.get("summary", "")
               for marker in ["\U0001f3cb", "\U0001f3ca", "\u2705", "\u23f3"]))
    movie_count = sum(
        1 for e in events
        if "\U0001f3ac" in e.get("summary", ""))
    concert_count = sum(
        1 for e in events
        if any(marker in e.get("summary", "")
               for marker in ["\U0001f3b5", "\U0001f3b6"]))

    issues = []
    if fitness_count == 0:
        issues.append(
            "No fitness classes found in next 7 days "
            "(auto-booker may be failing)")

    # Check for duplicates
    seen = {}
    for e in events:
        key = (e.get("summary", ""),
               e.get("start", {}).get("dateTime", ""))
        if key in seen:
            issues.append(
                "Duplicate event: {} at {}".format(key[0], key[1]))
        seen[key] = True

    total = len(events)
    summary = (
        "{} events in next 7 days "
        "({} fitness, {} movies, {} concerts)"
    ).format(total, fitness_count, movie_count, concert_count)

    if issues:
        return False, "{}. Issues: {}".format(
            summary, "; ".join(issues))

    return True, summary


def check_ics_file():
    """Verify the ICS file is accessible via GitHub Pages."""
    ics_url = (
        "https://connordy8.github.io/tice-creek-calendar/"
        "beth-calendar.ics"
    )

    try:
        req = urllib.request.Request(
            ics_url,
            headers={"User-Agent": "TiceCreekHealthcheck/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            content = resp.read().decode("utf-8", errors="replace")
            if "BEGIN:VCALENDAR" not in content:
                return False, "ICS file exists but is not a valid calendar"
            event_count = content.count("BEGIN:VEVENT")
            if event_count == 0:
                return False, "ICS file has 0 events"
            return True, "ICS file OK ({} events)".format(event_count)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False, "ICS file not found (GitHub Pages may be down)"
        return False, "ICS file HTTP error: {}".format(e.code)
    except Exception as e:
        return False, "ICS file check failed: {}".format(e)


def main():
    log.info("=" * 60)
    log.info("Beth's Calendar - Healthcheck")
    log.info("=" * 60)

    all_ok = True
    results = []

    # Check 1: Google Calendar
    log.info("Checking Google Calendar...")
    ok, msg = check_google_calendar()
    results.append(("Google Calendar", ok, msg))
    log.info("  {} — {}".format("PASS" if ok else "FAIL", msg))
    if not ok:
        all_ok = False

    # Check 2: ICS file
    log.info("Checking ICS file...")
    ok, msg = check_ics_file()
    results.append(("ICS File", ok, msg))
    log.info("  {} — {}".format("PASS" if ok else "FAIL", msg))
    if not ok:
        all_ok = False

    # Send alert if anything failed
    if not all_ok:
        from notify import send_alert

        failures = [
            "{}: {}".format(name, msg)
            for name, ok, msg in results if not ok
        ]
        send_alert(
            "Healthcheck",
            "One or more checks failed",
            extra_context="\n".join(failures),
        )
        log.error("Healthcheck FAILED")
        sys.exit(1)
    else:
        log.info("")
        log.info("All checks passed!")
        for name, ok, msg in results:
            log.info("  {} — {}".format(name, msg))


if __name__ == "__main__":
    main()
