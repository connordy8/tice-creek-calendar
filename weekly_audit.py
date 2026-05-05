#!/usr/bin/env python3
"""
Weekly audit — compares Tice Creek's actual schedule against Beth's
calendar and emails Connor a digest of any gaps.

For each day in the next N days:
  - Scrape Mindbody for all classes (uses scraper logic)
  - Filter to Beth's preferences (config.yaml include_classes,
    earliest_hour)
  - Pull her actual Google Calendar events for that day
  - Diff: which preference-matching classes exist at Tice Creek but
    are NOT on her calendar?

Sends a single email to connordy@gmail.com with the gap report. If
there are no gaps, sends nothing (no spam on healthy weeks).

Usage:
  python3 weekly_audit.py             # Default 7 days, send email
  python3 weekly_audit.py --days 14   # Audit two weeks
  python3 weekly_audit.py --no-email  # Print only
"""

import json
import os
import re
import sys
import yaml
import logging
from datetime import datetime, timedelta, time as dtime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------
DAYS = 7
NO_EMAIL = False
for i, a in enumerate(sys.argv):
    if a == "--days" and i + 1 < len(sys.argv):
        DAYS = int(sys.argv[i + 1])
    if a == "--no-email":
        NO_EMAIL = True


# ---------------------------------------------------------------------
# Load Beth's preferences
# ---------------------------------------------------------------------
CONFIG_PATH = Path(__file__).parent / "config.yaml"
with open(CONFIG_PATH) as f:
    CFG = yaml.safe_load(f)

INCLUDE = [s.lower() for s in CFG.get("include_classes", [])]
EXCLUDE = [s.lower() for s in CFG.get("exclude_classes", [])]
EARLIEST_HOUR = CFG.get("earliest_hour", 11)


def matches_beth(name):
    n = name.lower()
    if any(x in n for x in EXCLUDE):
        return False
    return any(x in n for x in INCLUDE)


# ---------------------------------------------------------------------
# Mindbody scrape — parse the FRESH ICS (the workflow regenerates this
# every run before the audit, so it's always current)
# ---------------------------------------------------------------------
ICS_MAX_AGE_HOURS = 6  # refuse to use older data — better to error


def scrape_week(days):
    """Read scraper output. Refuses to use stale data — better to fail
    loudly than alert on month-old ICS contents.

    Returns a list of dicts: {date, start_time, name, location}.
    """
    ics = Path(__file__).parent / "docs" / "beth-calendar.ics"
    if not ics.exists():
        raise RuntimeError(
            "docs/beth-calendar.ics not found. Run scraper.py first "
            "(the weekly-audit workflow does this automatically).")

    age_h = (datetime.now().timestamp()
             - ics.stat().st_mtime) / 3600.0
    log.info("ICS file age: {:.1f}h (max allowed: {}h)".format(
        age_h, ICS_MAX_AGE_HOURS))
    if age_h > ICS_MAX_AGE_HOURS:
        raise RuntimeError(
            "docs/beth-calendar.ics is {:.1f}h old — refusing to "
            "audit against stale data. The workflow should run "
            "scraper.py before the audit. Fix the workflow ordering."
            .format(age_h))

    return _parse_ics_for_classes()


def _parse_ics_for_classes():
    ics = Path(__file__).parent / "docs" / "beth-calendar.ics"
    if not ics.exists():
        return []
    text = ics.read_text()
    classes = []
    for block in re.split(r"BEGIN:VEVENT", text)[1:]:
        end = block.find("END:VEVENT")
        if end < 0:
            continue
        block = block[:end]
        m_dt = re.search(r"DTSTART[^:]*:(\d{8})T(\d{6})", block)
        m_sum = re.search(r"SUMMARY:(.+)", block)
        if not m_dt or not m_sum:
            continue
        date = "{}-{}-{}".format(
            m_dt.group(1)[:4], m_dt.group(1)[4:6], m_dt.group(1)[6:8])
        time_s = "{}:{}".format(m_dt.group(2)[:2], m_dt.group(2)[2:4])
        classes.append({
            "date": date,
            "start_time": time_s,
            "name": m_sum.group(1).strip(),
            "location": "",
        })
    return classes


# ---------------------------------------------------------------------
# Google Calendar — what's actually scheduled for Beth
# ---------------------------------------------------------------------
def fetch_calendar_events(days):
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_KEY"]),
        scopes=["https://www.googleapis.com/auth/calendar.readonly"])
    svc = build("calendar", "v3", credentials=creds, cache_discovery=False)

    now = datetime.utcnow()
    start = (now - timedelta(hours=24)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    resp = svc.events().list(
        calendarId=os.environ["GOOGLE_CALENDAR_ID"],
        timeMin=start.isoformat() + "Z",
        timeMax=(now + timedelta(days=days)).isoformat() + "Z",
        maxResults=500,
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    out = []
    for e in resp.get("items", []):
        dt = (e.get("start", {}).get("dateTime")
              or e.get("start", {}).get("date") or "")
        out.append({
            "date": dt[:10],
            "start_time": dt[11:16] if "T" in dt else "",
            "name": e.get("summary", ""),
        })
    return out


def cal_has_match(cal_events, mb_class):
    """Match a Mindbody class against calendar events.

    True if the calendar has any event on the same date whose summary
    contains a token from the class name (or vice versa). We're loose
    here because emoji prefixes and "(waitlist)" suffixes vary.
    """
    target_date = mb_class["date"]
    target_name = mb_class["name"].lower()
    target_keywords = [w for w in re.findall(r"[a-z]+", target_name)
                       if len(w) >= 4]
    for ev in cal_events:
        if ev["date"] != target_date:
            continue
        ev_name = ev["name"].lower()
        # Match if any 4+ char keyword appears in both
        for kw in target_keywords:
            if kw in ev_name:
                return True
    return False


# ---------------------------------------------------------------------
# Build report
# ---------------------------------------------------------------------
def find_gaps(mb_classes, cal_events):
    """Return classes that are at Tice Creek matching Beth's prefs
    but NOT on her calendar."""
    gaps = []
    for c in mb_classes:
        # Filter to Beth-preference classes after earliest_hour
        try:
            hh = int(c["start_time"].split(":")[0])
        except Exception:
            continue
        if hh < EARLIEST_HOUR:
            continue
        if not matches_beth(c["name"]):
            continue
        if not cal_has_match(cal_events, c):
            gaps.append(c)
    return gaps


def format_report(gaps):
    if not gaps:
        return None
    by_date = {}
    for g in gaps:
        by_date.setdefault(g["date"], []).append(g)
    lines = ["Beth's calendar weekly audit — found gaps:", ""]
    for d in sorted(by_date):
        weekday = datetime.strptime(d, "%Y-%m-%d").strftime("%A")
        lines.append("{} ({}):".format(weekday, d))
        for g in sorted(by_date[d], key=lambda x: x["start_time"]):
            lines.append("  {} — {}".format(
                g["start_time"], g["name"]))
        lines.append("")
    lines.append(
        "These classes are on the Tice Creek schedule and match "
        "Beth's preferences but were not found on her Google Calendar.")
    lines.append("")
    lines.append(
        "IMPORTANT: This audit already ran scraper.py + auto_book.py "
        "BEFORE producing this report. If you're seeing gaps here, "
        "automated self-heal couldn't close them — manual "
        "investigation is needed.")
    lines.append("")
    lines.append("Likely causes worth checking:")
    lines.append("  - Mindbody UI changed (look at the auto-book log "
                 "for 'No matching button' / page errors)")
    lines.append("  - The class is full and Beth's already on the "
                 "waitlist (waitlist may not be writing to calendar)")
    lines.append("  - A keyword in include_classes matches the Tice "
                 "Creek class name but auto_book.py's TARGET_CLASSES "
                 "doesn't (or vice versa)")
    return "\n".join(lines)


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------
def main():
    log.info("=" * 60)
    log.info("Beth's Calendar — Weekly Audit ({} days)".format(DAYS))
    log.info("=" * 60)

    mb_classes = scrape_week(DAYS)
    log.info("Mindbody classes found: {}".format(len(mb_classes)))

    cal_events = fetch_calendar_events(DAYS)
    log.info("Calendar events found:  {}".format(len(cal_events)))

    gaps = find_gaps(mb_classes, cal_events)
    log.info("Gaps (preference matches not on calendar): {}".format(
        len(gaps)))

    report = format_report(gaps)
    if report:
        print("\n" + report + "\n")
        if not NO_EMAIL:
            try:
                from notify import send_alert
                send_alert(
                    "Weekly Calendar Audit",
                    "Found {} preference-matching classes not on "
                    "Beth's calendar".format(len(gaps)),
                    extra_context=report,
                )
                log.info("Sent gap report to connordy@gmail.com")
            except Exception as e:
                log.warning("Could not send alert: {}".format(e))
    else:
        log.info("No gaps. Calendar is in sync. (No email sent.)")


if __name__ == "__main__":
    main()
