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

    if alerts:
        msg = "\n\n".join(alerts)
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
