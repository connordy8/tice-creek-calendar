#!/usr/bin/env python3
"""
Booking-window probe.

For target classes 7-8 days out, log the (Reserved, Open) counts and
whether a Reserve button is even present. Run frequently across a 24h
window — by comparing timestamps of state changes, we can determine
EXACTLY when Tice Creek opens each class's booking window.

Output is appended to booking_probe.log (one JSON line per check).
Upload that as a workflow artifact for review.

Usage:
  python3 booking_probe.py
"""

import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

LOG_FILE = Path("booking_probe.log")


def main():
    from playwright.sync_api import sync_playwright

    # Probe day 7 and day 8 (most likely to be at the booking edge)
    targets = []
    for offset in (7, 8):
        d = (datetime.now() + timedelta(days=offset)).date()
        targets.append(d)

    rows = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()

        # Login (reuse the auto_book module so we don't duplicate)
        sys.path.insert(0, str(Path(__file__).parent))
        from auto_book import (  # noqa: E402
            _attempt_login, navigate_to_schedule,
            find_reservable_classes, SCHEDULE_LOCATIONS,
        )

        try:
            _attempt_login(page)
        except Exception as e:
            log.error("Login failed: {}".format(e))
            return

        for target_date in targets:
            for loc in SCHEDULE_LOCATIONS:
                try:
                    navigate_to_schedule(page, target_date, loc=loc)
                    buttons = find_reservable_classes(page)
                except Exception as e:
                    log.warning("scrape failed for {} loc={}: {}".format(
                        target_date, loc, e))
                    continue

                for b in buttons:
                    text = b.get("text", "") if isinstance(b, dict) else str(b)
                    # Extract counts: (X Reserved, Y Open)
                    m = re.search(
                        r"\((\d+)\s*Reserved,\s*(\d+)\s*Open\)", text)
                    reserved = int(m.group(1)) if m else None
                    opens = int(m.group(2)) if m else None
                    rows.append({
                        "ts": datetime.utcnow().isoformat() + "Z",
                        "date": target_date.isoformat(),
                        "loc": loc,
                        "row_text": text[:300],
                        "reserved": reserved,
                        "open": opens,
                    })

        browser.close()

    # Append rows to log file
    with open(LOG_FILE, "a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

    log.info("Logged {} probe rows".format(len(rows)))


if __name__ == "__main__":
    main()
