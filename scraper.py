#!/usr/bin/env python3
"""
Tice Creek Fitness Center Schedule -> Google Calendar Sync

Scrapes class schedules from the Tice Creek Fitness Center website
(Mindbody/Healcode Branded Web widgets) and generates an ICS calendar file.

Usage:
    python3 scraper.py                  # Normal run (headless)
    python3 scraper.py --discover       # Discovery mode: captures debug info
    python3 scraper.py --no-headless    # Watch the browser work
"""

import json
import hashlib
import re
import sys
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict
from zoneinfo import ZoneInfo

import yaml
from playwright.sync_api import sync_playwright

# --- Configuration -------------------------------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

PACIFIC = ZoneInfo("America/Los_Angeles")
STUDIO_ID = 72039
LOCATION = "Tice Creek Fitness Center, 1751 Tice Creek Dr, Walnut Creek, CA 94595"

SCHEDULE_PAGES = {
    "group_fitness": "https://www.ticefitnesscenter.com/schedule/",
    "aquatics": "https://www.ticefitnesscenter.com/aquatic-schedule/",
}

DISCOVER_MODE = "--discover" in sys.argv
HEADLESS = "--no-headless" not in sys.argv


def load_config(path="config.yaml"):
    p = Path(path)
    if not p.exists():
        return {}
    with open(p) as f:
        return yaml.safe_load(f) or {}


# =========================================================================
# HTML Parsing - Branded Web (bw) Widget
# =========================================================================
# The Tice Creek website embeds Mindbody's "Branded Web" widget in an iframe.
# Each class is a <div class="bw-session"> with:
#   - data-bw-widget-mbo-class-name="..." (machine-readable name)
#   - <time class="hc_starttime" datetime="2026-02-16T10:00">
#   - <time class="hc_endtime" datetime="2026-02-16T10:45">
#   - <div class="bw-session__name">Water Aerobics</div>
#   - <div class="bw-session__staff">CATHY STEEN</div>

def parse_bw_widget_html(html, label=""):
    """Parse classes from Branded Web widget HTML."""
    classes = []

    # Match each bw-session div and its content
    session_pattern = (
        r'(<div[^>]*class="bw-session"[^>]*>)'
        r'(.*?)'
        r'(?=<div[^>]*class="bw-session"|<div class="bw-widget__day|</body>)'
    )
    matches = re.findall(session_pattern, html, re.DOTALL)

    for opening_tag, content in matches:
        # Machine-readable class name from data attribute
        raw_name_match = re.search(
            r'data-bw-widget-mbo-class-name="([^"]+)"', opening_tag)
        raw_name = raw_name_match.group(1) if raw_name_match else ""

        # Start time (ISO datetime)
        start_match = re.search(r'hc_starttime[^>]*datetime="([^"]+)"', content)
        if not start_match:
            continue
        start_iso = start_match.group(1)  # e.g., "2026-02-16T10:00"

        # End time (ISO datetime)
        end_match = re.search(r'hc_endtime[^>]*datetime="([^"]+)"', content)
        end_iso = end_match.group(1) if end_match else ""

        # Display name (text inside .bw-session__name, minus the type span)
        name_block = re.search(
            r'class="bw-session__name">(.*?)</div>', content, re.DOTALL)
        display_name = ""
        if name_block:
            text = re.sub(r'<span[^>]*>.*?</span>', '', name_block.group(1))
            display_name = re.sub(r'<[^>]+>', '', text).strip()

        if not display_name:
            display_name = raw_name.replace("_", " ").title()

        # Instructor
        staff_block = re.search(
            r'class="bw-session__staff"[^>]*>(.*?)</div>', content, re.DOTALL)
        instructor = ""
        if staff_block:
            instructor = re.sub(r'<[^>]+>', '', staff_block.group(1)).strip()

        # Parse the start datetime
        try:
            start_dt = datetime.fromisoformat(start_iso)
        except ValueError:
            continue

        cls = {
            "name": display_name,
            "raw_name": raw_name,
            "start_iso": start_iso,
            "end_iso": end_iso,
            "date": start_dt.strftime("%Y-%m-%d"),
            "day": start_dt.strftime("%A"),
            "time": start_dt.strftime("%I:%M %p").lstrip("0"),
            "start_hour": start_dt.hour,
            "instructor": instructor,
            "source": label,
        }

        # Parse end time for duration
        if end_iso:
            try:
                end_dt = datetime.fromisoformat(end_iso)
                cls["end_time"] = end_dt.strftime("%I:%M %p").lstrip("0")
                cls["duration_minutes"] = int(
                    (end_dt - start_dt).total_seconds() / 60)
            except ValueError:
                pass

        classes.append(cls)

    return classes


def scrape_page(page, url, label):
    """Load a schedule page and extract classes from the bw-widget."""
    log.info("Loading: {}".format(url))
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        log.warning("  Page load issue: {}".format(e))

    # Wait for the widget to render
    page.wait_for_timeout(15000)

    all_classes = []

    # Try main page HTML
    html = page.content()
    classes = parse_bw_widget_html(html, label)
    if classes:
        log.info("  Found {} classes in main frame".format(len(classes)))
        all_classes.extend(classes)

    # Also check iframes (the widget usually lives in an iframe)
    for i, frame in enumerate(page.frames):
        if frame == page.main_frame:
            continue
        try:
            frame_html = frame.content()
            classes = parse_bw_widget_html(frame_html, label)
            if classes:
                log.info("  Found {} classes in frame {} ({})".format(
                    len(classes), i, frame.url[:80]))
                all_classes.extend(classes)
        except Exception as e:
            log.debug("  Frame {} error: {}".format(i, e))

    if not all_classes:
        log.info("  No classes found on this page")

    return all_classes


# =========================================================================
# Filtering
# =========================================================================

def filter_classes(classes, config):
    include = [c.lower().strip() for c in config.get("include_classes", []) if c]
    exclude = [c.lower().strip() for c in config.get("exclude_classes", []) if c]
    earliest = config.get("earliest_hour")
    latest = config.get("latest_hour")

    if not include and not exclude and earliest is None and latest is None:
        return classes

    filtered = []
    for cls in classes:
        nm = cls.get("name", "").lower()
        raw = cls.get("raw_name", "").lower()
        combined = nm + " " + raw

        if include and not any(p in combined for p in include):
            continue
        if exclude and any(p in combined for p in exclude):
            continue

        hour = cls.get("start_hour")
        if hour is not None:
            if earliest is not None and hour < earliest:
                continue
            if latest is not None and hour >= latest:
                continue

        filtered.append(cls)

    log.info("Filtered {} -> {} classes".format(len(classes), len(filtered)))
    return filtered


# =========================================================================
# ICS generation
# =========================================================================

def generate_ics(classes, config):
    cal_name = config.get("calendar_name",
                          "Tice Creek Fitness \u2013 Mom's Classes")
    default_dur = config.get("default_class_duration_minutes", 45)
    now_utc = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0",
        "PRODID:-//Tice Creek Calendar Sync//EN",
        "X-WR-CALNAME:{}".format(cal_name),
        "X-WR-TIMEZONE:America/Los_Angeles",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        "BEGIN:VTIMEZONE", "TZID:America/Los_Angeles",
        "BEGIN:DAYLIGHT", "TZOFFSETFROM:-0800", "TZOFFSETTO:-0700",
        "TZNAME:PDT",
        "DTSTART:19700308T020000",
        "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU", "END:DAYLIGHT",
        "BEGIN:STANDARD", "TZOFFSETFROM:-0700", "TZOFFSETTO:-0800",
        "TZNAME:PST",
        "DTSTART:19701101T020000",
        "RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU", "END:STANDARD",
        "END:VTIMEZONE",
    ]

    count = 0
    for cls in classes:
        start_iso = cls.get("start_iso", "")
        if not start_iso:
            continue
        try:
            start = datetime.fromisoformat(start_iso)
        except ValueError:
            continue

        dur = cls.get("duration_minutes", default_dur)
        if dur <= 0:
            dur = default_dur
        end = start + timedelta(minutes=dur)

        name = cls["name"]
        instructor = cls.get("instructor", "")
        source = cls.get("source", "")

        is_water = any(
            w in name.lower() for w in ["aqua", "water", "swim", "pool"])
        emoji = "\U0001f3ca" if is_water else "\U0001f3cb\ufe0f"

        desc_parts = []
        if instructor:
            desc_parts.append("Instructor: {}".format(instructor))
        if source:
            desc_parts.append("Schedule: {}".format(
                source.replace("_", " ").title()))
        desc_parts.append("Auto-synced from ticefitnesscenter.com")
        newline = "\\n"
        description = newline.join(desc_parts)

        uid_str = "{}-{}-{}".format(name, cls.get("date", ""), start_iso)
        uid = hashlib.md5(uid_str.encode()).hexdigest()[:16]

        lines.extend([
            "BEGIN:VEVENT",
            "UID:{}@tice-creek-sync".format(uid),
            "DTSTAMP:{}".format(now_utc),
            "DTSTART;TZID=America/Los_Angeles:{}".format(
                start.strftime("%Y%m%dT%H%M%S")),
            "DTEND;TZID=America/Los_Angeles:{}".format(
                end.strftime("%Y%m%dT%H%M%S")),
            "SUMMARY:{} {}".format(emoji, name),
            "DESCRIPTION:{}".format(description),
            "LOCATION:{}".format(LOCATION),
            "STATUS:CONFIRMED", "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ])
        count += 1

    lines.append("END:VCALENDAR")
    log.info("Generated {} calendar events".format(count))
    return "\r\n".join(lines)


# =========================================================================
# Discovery mode
# =========================================================================

def run_discovery(page, url, label):
    debug = Path("debug")
    debug.mkdir(exist_ok=True)
    network = []

    def on_response(response):
        entry = {
            "url": response.url,
            "status": response.status,
            "content_type": response.headers.get("content-type", ""),
        }
        try:
            ct = entry["content_type"]
            if "json" in ct or "javascript" in ct:
                entry["body_preview"] = response.text()[:5000]
        except Exception:
            pass
        network.append(entry)

    page.on("response", on_response)
    log.info("[DISCOVER] Loading: {}".format(url))
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        log.warning("  Load: {}".format(e))
    page.wait_for_timeout(15000)

    # Save HTML
    html = page.content()
    (debug / "{}.html".format(label)).write_text(html)
    log.info("  HTML: {:,} chars -> debug/{}.html".format(len(html), label))

    # Save network log
    with open(debug / "{}_network.json".format(label), "w") as f:
        json.dump(network, f, indent=2, default=str)
    log.info("  Network: {} requests -> debug/{}_network.json".format(
        len(network), label))

    # Log frame info
    log.info("  Frames: {}".format(len(page.frames)))
    for i, frame in enumerate(page.frames):
        log.info("    [{}] {}".format(i, frame.url[:120]))

    # Log widget element counts
    for sel in ["iframe", ".bw-widget", ".bw-session", "table"]:
        try:
            n = len(page.query_selector_all(sel))
            if n:
                log.info("    '{}': {} elements".format(sel, n))
        except Exception:
            pass

    # Screenshot
    try:
        page.screenshot(path=str(debug / "{}.png".format(label)))
        log.info("  Screenshot -> debug/{}.png".format(label))
    except Exception:
        pass

    # Extract classes from HTML
    classes = parse_bw_widget_html(html, label)

    # Also check iframes
    for i, frame in enumerate(page.frames):
        if frame == page.main_frame:
            continue
        try:
            frame_html = frame.content()
            frame_classes = parse_bw_widget_html(frame_html, label)
            if frame_classes:
                log.info("  Found {} classes in frame {}".format(
                    len(frame_classes), i))
                classes.extend(frame_classes)
        except Exception:
            pass

    return classes


# =========================================================================
# Main
# =========================================================================

def main():
    config = load_config()
    output_dir = Path(config.get("output_dir", "docs"))
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / config.get(
        "output_filename", "tice-creek-classes.ics")

    log.info("=" * 60)
    log.info("Tice Creek Fitness Center \u2013 Calendar Sync")
    log.info("Mode: {} | Headless: {}".format(
        "DISCOVER" if DISCOVER_MODE else "normal", HEADLESS))
    log.info("=" * 60)

    all_classes = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/121.0.0.0 Safari/537.36"
            ),
        )

        for label, url in SCHEDULE_PAGES.items():
            page = ctx.new_page()

            if DISCOVER_MODE:
                classes = run_discovery(page, url, label)
            else:
                classes = scrape_page(page, url, label)

            all_classes.extend(classes)
            page.close()

        browser.close()

    log.info("")
    log.info("Total scraped: {}".format(len(all_classes)))

    # Deduplicate by start_iso + name
    seen = set()
    unique = []
    for cls in all_classes:
        key = (cls.get("start_iso", ""), cls.get("name", ""))
        if key not in seen:
            seen.add(key)
            unique.append(cls)
    if len(unique) < len(all_classes):
        log.info("Deduplicated: {} -> {} unique".format(
            len(all_classes), len(unique)))
    all_classes = unique

    # Save raw data
    Path("debug").mkdir(exist_ok=True)
    with open("debug/all_classes.json", "w") as f:
        json.dump(all_classes, f, indent=2, default=str)

    if DISCOVER_MODE:
        log.info("")
        log.info("\U0001f50d Discovery complete! Check the debug/ folder.")
        log.info("   Files: *.html, *.png, *_network.json, all_classes.json")
        if all_classes:
            log.info("")
            log.info("\u2705 Auto-extracted {} classes!".format(
                len(all_classes)))
            for cls in all_classes[:10]:
                log.info("   {} {} - {} ({})".format(
                    cls.get("date", "?"), cls.get("time", "?"),
                    cls.get("name", "?"), cls.get("instructor", "?")))
            if len(all_classes) > 10:
                log.info("   ... and {} more".format(len(all_classes) - 10))
            log.info("")
            log.info("   Run without --discover to generate the calendar.")
        else:
            log.info("")
            log.info("\u26a0\ufe0f  No classes auto-extracted.")
            log.info("   Share the debug/ folder and I'll tune the scraper.")
        return

    if not all_classes:
        log.error(
            "\n\u274c No classes scraped!\n"
            "   Run: python3 scraper.py --discover --no-headless\n"
            "   Then share the debug/ folder so we can tune the parser."
        )
        output_file.write_text(generate_ics([], config))
        sys.exit(1)

    # Show what we found
    for cls in all_classes[:15]:
        log.info("  {} {:>8} - {} ({})".format(
            cls.get("date", "?"), cls.get("time", "?"),
            cls.get("name", "?"), cls.get("instructor", "?")))
    if len(all_classes) > 15:
        log.info("  ... and {} more".format(len(all_classes) - 15))

    # Filter to Beth's preferences
    filtered = filter_classes(all_classes, config)

    if filtered:
        log.info("")
        log.info("Beth's classes this period:")
        for cls in filtered:
            log.info("  {} {} {} - {} ({})".format(
                cls.get("day", ""), cls.get("date", ""),
                cls.get("time", ""), cls.get("name", ""),
                cls.get("instructor", "")))

    # Generate ICS
    ics = generate_ics(filtered, config)
    output_file.write_text(ics)
    log.info("")
    log.info("\u2705 Calendar -> {} ({:,} bytes, {} events)".format(
        output_file, output_file.stat().st_size, len(filtered)))


if __name__ == "__main__":
    main()
