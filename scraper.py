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

ROSSMOOR_MOVIE_PDF_URL = (
    "https://rossmoor.com/residents/recreation/movies-and-special-events/"
)
MOVIE_LOCATION = (
    "Peacock Hall, Gateway Complex, 1001 Golden Rain Rd, Walnut Creek, CA 94595"
)

# Location codes from the PDF legend
ROSSMOOR_LOCATIONS = {
    "PH": "Peacock Hall, Gateway Complex, 1001 Golden Rain Rd, Walnut Creek, CA 94595",
    "EC": "Event Center, 1021 Stanley Dollar Dr, Walnut Creek, CA 94595",
    "FR": "Fireside Room, Gateway Complex, 1001 Golden Rain Rd, Walnut Creek, CA 94595",
    "G": "Gateway Complex, 1001 Golden Rain Rd, Walnut Creek, CA 94595",
    "CR": "Creekside, Rossmoor, Walnut Creek, CA 94595",
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
    raw_include = config.get("include_classes", [])
    exclude = [c.lower().strip() for c in config.get("exclude_classes", []) if c]
    earliest = config.get("earliest_hour")
    latest = config.get("latest_hour")

    # Normalise include rules: each becomes {name: str, instructor: str|None}
    include_rules = []
    for entry in raw_include:
        if isinstance(entry, dict):
            include_rules.append({
                "name": entry.get("name", "").lower().strip(),
                "instructor": entry.get("instructor", "").lower().strip() or None,
            })
        elif isinstance(entry, str) and entry.strip():
            include_rules.append({
                "name": entry.lower().strip(),
                "instructor": None,
            })

    if not include_rules and not exclude and earliest is None and latest is None:
        return classes

    filtered = []
    for cls in classes:
        nm = cls.get("name", "").lower()
        raw = cls.get("raw_name", "").lower()
        combined = nm + " " + raw
        instr = cls.get("instructor", "").lower()

        # Check include rules
        if include_rules:
            matched = False
            for rule in include_rules:
                if rule["name"] in combined:
                    if rule["instructor"] is None or rule["instructor"] in instr:
                        matched = True
                        break
            if not matched:
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


def resolve_conflicts(classes):
    """When Zumba overlaps with another class, keep only Zumba."""
    to_remove = set()
    for i, a in enumerate(classes):
        for j, b in enumerate(classes):
            if i >= j:
                continue
            a_start = a.get("start_iso", "")
            b_start = b.get("start_iso", "")
            a_end = a.get("end_iso", a_start)
            b_end = b.get("end_iso", b_start)
            if not (a_start and b_start):
                continue
            # Check overlap: two events overlap if one starts before the other ends
            if a_start < b_end and b_start < a_end:
                a_is_zumba = "zumba" in a.get("raw_name", "").lower() or "zumba" in a.get("name", "").lower()
                b_is_zumba = "zumba" in b.get("raw_name", "").lower() or "zumba" in b.get("name", "").lower()
                if a_is_zumba and not b_is_zumba:
                    to_remove.add(j)
                    log.info("Conflict: keeping '{}' over '{}'".format(
                        a.get("name"), b.get("name")))
                elif b_is_zumba and not a_is_zumba:
                    to_remove.add(i)
                    log.info("Conflict: keeping '{}' over '{}'".format(
                        b.get("name"), a.get("name")))
    if to_remove:
        classes = [c for idx, c in enumerate(classes) if idx not in to_remove]
        log.info("Resolved conflicts: removed {} overlapping classes".format(
            len(to_remove)))
    return classes


# =========================================================================
# Movie scraping (Rossmoor Peacock Hall)
# =========================================================================

def download_movie_pdf(url=ROSSMOOR_MOVIE_PDF_URL):
    """Download the Rossmoor Recreation Calendar PDF."""
    import urllib.request
    import tempfile

    log.info("Downloading Rossmoor movie calendar PDF...")
    req = urllib.request.Request(url, headers={
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/121.0.0.0 Safari/537.36"
        ),
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()

    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp.write(data)
    tmp.close()
    log.info("  Downloaded {:,} bytes -> {}".format(len(data), tmp.name))
    return tmp.name


def parse_recreation_pdf(pdf_path):
    """Extract movie AND concert/event listings from the Rossmoor PDF.

    Returns (movies, concerts) where each is a list of dicts.
    """
    import pdfplumber

    movies = []
    concerts = []

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if not text:
                continue

            # Find month/year header (e.g., "March 2026")
            month_match = re.search(
                r'(January|February|March|April|May|June|July|August|'
                r'September|October|November|December)\s+(\d{4})', text)
            if not month_match:
                continue
            month_name = month_match.group(1)
            year = int(month_match.group(2))
            month_num = datetime.strptime(month_name, "%B").month
            log.info("  Parsing recreation calendar for {} {}".format(
                month_name, year))

            lines = text.split('\n')
            current_day = None

            i = 0
            while i < len(lines):
                line = lines[i].strip()

                # Detect day numbers: standalone 1-31
                day_match = re.match(r'^(\d{1,2})$', line)
                if day_match:
                    d = int(day_match.group(1))
                    if 1 <= d <= 31:
                        current_day = d
                    i += 1
                    continue

                # --- Movies: Movie: "Title" (Year) ---
                movie_match = re.search(
                    r'Movie:\s*[\u201c"\u2018\'](.*?)[\u201d"\u2019\']\s*'
                    r'\((\d{4})\)', line)
                if movie_match and current_day:
                    title = movie_match.group(1)
                    movie_year = movie_match.group(2)

                    times_text = line
                    if i + 1 < len(lines):
                        next_line = lines[i + 1].strip()
                        if re.search(r'\d.*[ap]\.m\.', next_line, re.IGNORECASE):
                            times_text += " " + next_line
                            i += 1

                    showtimes = _parse_showtimes(
                        times_text, year, month_num, current_day)

                    for dt in showtimes:
                        date_str = "{}-{:02d}-{:02d}".format(
                            year, month_num, current_day)
                        movies.append({
                            "title": title,
                            "movie_year": movie_year,
                            "date": date_str,
                            "start_iso": dt.strftime("%Y-%m-%dT%H:%M"),
                            "start_hour": dt.hour,
                            "start_dt": dt,
                            "is_movie": True,
                        })
                    i += 1
                    continue

                # --- Concerts & Spotlight events ---
                # Match: Concert: "Name" or The Spotlight: Name
                concert_match = re.search(
                    r'(Concert|The Spotlight):\s*[\u201c"\u2018\']?'
                    r'(.*?)[\u201d"\u2019\']?\s*$', line)
                if concert_match and current_day:
                    event_type = concert_match.group(1).strip()
                    event_name = concert_match.group(2).strip()
                    # Clean up trailing quotes
                    event_name = event_name.strip('\u201c\u201d"\'')

                    # Gather time + location + cost from next line(s)
                    times_text = ""
                    cost = ""
                    location_code = ""
                    for look_ahead in range(1, 4):
                        if i + look_ahead >= len(lines):
                            break
                        next_line = lines[i + look_ahead].strip()
                        if re.search(r'[ap]\.m\.', next_line, re.IGNORECASE):
                            times_text += " " + next_line
                            # Extract cost like ($22) or ($18)
                            cost_match = re.search(
                                r'\(\$(\d+)\)', next_line)
                            if cost_match:
                                cost = "${}".format(cost_match.group(1))
                            # Extract location code
                            for code in ["EC", "FR", "PH", "CR", "G"]:
                                if code in next_line.split():
                                    location_code = code
                                    break
                            i += 1
                        else:
                            break

                    if not times_text:
                        i += 1
                        continue

                    showtimes = _parse_showtimes(
                        times_text, year, month_num, current_day)

                    for dt in showtimes:
                        date_str = "{}-{:02d}-{:02d}".format(
                            year, month_num, current_day)
                        concerts.append({
                            "title": event_name,
                            "event_type": event_type,
                            "date": date_str,
                            "start_iso": dt.strftime("%Y-%m-%dT%H:%M"),
                            "start_hour": dt.hour,
                            "start_dt": dt,
                            "cost": cost,
                            "location_code": location_code,
                            "is_concert": True,
                        })

                i += 1

    log.info("  Parsed {} movie showings, {} concerts/events from PDF".format(
        len(movies), len(concerts)))
    return movies, concerts


def _parse_showtimes(text, year, month, day):
    """Parse showtime strings like '1, 4, 7 p.m.' into datetime objects.

    Handles formats:
        '1, 4, 7 p.m. PH'
        '10 a.m., 1, 4, 7 p.m. PH'
        '10 a.m., 1, 4, 7, 9:15 p.m. PH'
        '4 p.m. PH'
    """
    times = []

    # Extract just the time portion (before PH/EC/FR etc.)
    time_section = re.search(
        r'([\d,:\s.]+(?:a\.m\.|p\.m\.)(?:\s*,?\s*[\d,:\s.]*'
        r'(?:a\.m\.|p\.m\.)?)*)',
        text, re.IGNORECASE)
    if not time_section:
        return times

    time_str = time_section.group(1).strip()

    # Split by comma and process right-to-left to inherit AM/PM
    segments = [s.strip() for s in time_str.split(',')]
    parsed = []
    current_period = None

    for seg in reversed(segments):
        seg = seg.strip()
        if not seg:
            continue

        period_match = re.search(r'(a\.m\.|p\.m\.)', seg, re.IGNORECASE)
        if period_match:
            current_period = (
                "AM" if "a.m." in period_match.group().lower() else "PM")

        time_val = re.search(r'(\d{1,2})(?::(\d{2}))?', seg)
        if time_val and current_period:
            hour = int(time_val.group(1))
            minute = int(time_val.group(2)) if time_val.group(2) else 0

            if current_period == "PM" and hour != 12:
                hour += 12
            elif current_period == "AM" and hour == 12:
                hour = 0

            try:
                dt = datetime(year, month, day, hour, minute)
                parsed.append(dt)
            except ValueError:
                pass

    parsed.reverse()
    return parsed


def fetch_movie_description(title, year):
    """Fetch a 1-3 sentence movie description from Wikipedia."""
    import urllib.request
    import urllib.parse

    search_terms = [
        "{} ({} film)".format(title, year),
        "{} (film)".format(title),
        title,
    ]

    for term in search_terms:
        encoded = urllib.parse.quote(term.replace(' ', '_'))
        url = (
            "https://en.wikipedia.org/api/rest_v1/page/summary/{}"
            .format(encoded))
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "TiceCreekCalendar/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                extract = data.get("extract", "")
                if extract and len(extract) > 20:
                    sentences = re.split(r'(?<=[.!?])\s+', extract)
                    desc = ' '.join(sentences[:3])
                    if len(desc) > 500:
                        desc = desc[:497] + "..."
                    return desc
        except Exception:
            continue

    return ""


def scrape_entertainment(config):
    """Scrape Rossmoor movie + concert listings and return evening events."""
    include_movies = config.get("include_movies", True)
    include_concerts = config.get("include_concerts", True)

    if not include_movies and not include_concerts:
        log.info("Movies and concerts disabled in config, skipping")
        return [], []

    min_hour = config.get("movie_earliest_hour", 18)  # 6 PM default

    try:
        pdf_path = download_movie_pdf()
    except Exception as e:
        log.warning("Failed to download recreation PDF: {}".format(e))
        return [], []

    try:
        all_movies, all_concerts = parse_recreation_pdf(pdf_path)
    except Exception as e:
        log.warning("Failed to parse recreation PDF: {}".format(e))
        return [], []
    finally:
        import os
        try:
            os.unlink(pdf_path)
        except OSError:
            pass

    # --- Filter movies to evening showings ---
    evening_movies = []
    if include_movies:
        evening_movies = [m for m in all_movies if m["start_hour"] >= min_hour]
        log.info("  Evening movies ({}:00+): {}".format(
            min_hour, len(evening_movies)))

        # Fetch Wikipedia descriptions
        unique_titles = {}
        for m in evening_movies:
            key = (m["title"], m["movie_year"])
            if key not in unique_titles:
                unique_titles[key] = None

        log.info("  Fetching descriptions for {} unique movies...".format(
            len(unique_titles)))
        for (title, movie_year) in unique_titles:
            desc = fetch_movie_description(title, movie_year)
            unique_titles[(title, movie_year)] = desc
            if desc:
                log.info("    {} ({}) - got description".format(
                    title, movie_year))
            else:
                log.info("    {} ({}) - no description found".format(
                    title, movie_year))

        for m in evening_movies:
            m["description"] = unique_titles.get(
                (m["title"], m["movie_year"]), "")

    # --- Filter concerts to evening ---
    evening_concerts = []
    if include_concerts:
        evening_concerts = [
            c for c in all_concerts if c["start_hour"] >= min_hour]
        log.info("  Evening concerts ({}:00+): {}".format(
            min_hour, len(evening_concerts)))

    return evening_movies, evening_concerts


# =========================================================================
# ICS generation
# =========================================================================

def _ics_header(cal_name):
    """Return the standard VCALENDAR header lines."""
    return [
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


def generate_fitness_ics(classes, config):
    """Generate ICS for fitness classes only."""
    cal_name = config.get("calendar_name",
                          "Tice Creek \u2013 Beth's Classes")
    default_dur = config.get("default_class_duration_minutes", 45)
    early_start = config.get("early_start_minutes", 0)
    now_utc = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    lines = _ics_header(cal_name)
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

        # Apply custom titles from config
        display_name = name
        for rule in config.get("custom_titles", []):
            match_name = rule.get("match_name", "").lower()
            match_instr = rule.get("match_instructor", "").lower()
            if match_name and match_name in name.lower():
                if match_instr and match_instr in instructor.lower():
                    display_name = rule["title"]
                    break
                elif not match_instr:
                    display_name = rule["title"]
                    break

        is_water = any(
            w in name.lower() for w in ["aqua", "water", "swim", "pool"])
        emoji = "\U0001f3ca" if is_water else "\U0001f3cb\ufe0f"

        desc_parts = []
        if early_start > 0:
            real_time = cls.get("time", "")
            end_time = cls.get("end_time", "")
            if real_time and end_time:
                desc_parts.append("Class time: {} - {}".format(
                    real_time, end_time))
            elif real_time:
                desc_parts.append("Class time: {}".format(real_time))
        if instructor:
            desc_parts.append("Instructor: {}".format(instructor))
        if source:
            desc_parts.append("Schedule: {}".format(
                source.replace("_", " ").title()))
        desc_parts.append("Auto-synced from ticefitnesscenter.com")
        newline = "\\n"
        description = newline.join(desc_parts)

        cal_start = start - timedelta(minutes=early_start)

        uid_str = "{}-{}-{}".format(name, cls.get("date", ""), start_iso)
        uid = hashlib.md5(uid_str.encode()).hexdigest()[:16]

        lines.extend([
            "BEGIN:VEVENT",
            "UID:{}@tice-creek-sync".format(uid),
            "DTSTAMP:{}".format(now_utc),
            "DTSTART;TZID=America/Los_Angeles:{}".format(
                cal_start.strftime("%Y%m%dT%H%M%S")),
            "DTEND;TZID=America/Los_Angeles:{}".format(
                end.strftime("%Y%m%dT%H%M%S")),
            "SUMMARY:{} {}".format(emoji, display_name),
            "DESCRIPTION:{}".format(description),
            "LOCATION:{}".format(LOCATION),
            "STATUS:CONFIRMED", "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ])
        count += 1

    lines.append("END:VCALENDAR")
    log.info("Generated {} fitness events".format(count))
    return "\r\n".join(lines), count


def generate_entertainment_ics(movies, concerts, config):
    """Generate ICS for movies + concerts."""
    cal_name = "Rossmoor \u2013 Movies & Events"
    movie_dur = config.get("movie_duration_minutes", 135)
    concert_dur = config.get("concert_duration_minutes", 120)
    early_start = config.get("early_start_minutes", 0)
    now_utc = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    lines = _ics_header(cal_name)
    count = 0

    # --- Movies ---
    for mov in (movies or []):
        start_iso = mov.get("start_iso", "")
        if not start_iso:
            continue
        try:
            start = datetime.fromisoformat(start_iso)
        except ValueError:
            continue

        end = start + timedelta(minutes=movie_dur)
        title = mov["title"]
        movie_year = mov.get("movie_year", "")
        display_name = "{} ({})".format(title, movie_year)

        desc_parts = []
        movie_desc = mov.get("description", "")
        if movie_desc:
            desc_parts.append(movie_desc)
        show_time = start.strftime("%I:%M %p").lstrip("0")
        desc_parts.append("Showtime: {} at Peacock Hall".format(show_time))
        desc_parts.append("Free admission")
        desc_parts.append("Auto-synced from rossmoor.com recreation calendar")
        newline = "\\n"
        description = newline.join(desc_parts)

        cal_start = start - timedelta(minutes=early_start)
        uid_str = "movie-{}-{}-{}".format(title, mov.get("date", ""),
                                          start_iso)
        uid = hashlib.md5(uid_str.encode()).hexdigest()[:16]

        lines.extend([
            "BEGIN:VEVENT",
            "UID:{}@tice-creek-sync".format(uid),
            "DTSTAMP:{}".format(now_utc),
            "DTSTART;TZID=America/Los_Angeles:{}".format(
                cal_start.strftime("%Y%m%dT%H%M%S")),
            "DTEND;TZID=America/Los_Angeles:{}".format(
                end.strftime("%Y%m%dT%H%M%S")),
            "SUMMARY:\U0001f3ac {}".format(display_name),
            "DESCRIPTION:{}".format(description),
            "LOCATION:{}".format(MOVIE_LOCATION),
            "STATUS:CONFIRMED", "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ])
        count += 1

    # --- Concerts / Spotlight events ---
    for evt in (concerts or []):
        start_iso = evt.get("start_iso", "")
        if not start_iso:
            continue
        try:
            start = datetime.fromisoformat(start_iso)
        except ValueError:
            continue

        end = start + timedelta(minutes=concert_dur)
        title = evt["title"]
        event_type = evt.get("event_type", "Concert")
        cost = evt.get("cost", "")
        loc_code = evt.get("location_code", "EC")
        location = ROSSMOOR_LOCATIONS.get(loc_code, ROSSMOOR_LOCATIONS["EC"])

        if "Spotlight" in event_type:
            emoji = "\U0001f3b5"  # music note
            display_name = "Spotlight: {}".format(title)
        else:
            emoji = "\U0001f3b6"  # music notes
            display_name = title

        desc_parts = []
        show_time = start.strftime("%I:%M %p").lstrip("0")
        desc_parts.append("{} at {}".format(show_time, loc_code))
        if cost:
            desc_parts.append("Tickets: {}".format(cost))
        else:
            desc_parts.append("Free admission")
        desc_parts.append(
            "Tickets at Recreation Dept, Gateway, Mon-Fri 8am-4:30pm")
        desc_parts.append("Auto-synced from rossmoor.com recreation calendar")
        newline = "\\n"
        description = newline.join(desc_parts)

        cal_start = start - timedelta(minutes=early_start)
        uid_str = "concert-{}-{}-{}".format(title, evt.get("date", ""),
                                            start_iso)
        uid = hashlib.md5(uid_str.encode()).hexdigest()[:16]

        lines.extend([
            "BEGIN:VEVENT",
            "UID:{}@tice-creek-sync".format(uid),
            "DTSTAMP:{}".format(now_utc),
            "DTSTART;TZID=America/Los_Angeles:{}".format(
                cal_start.strftime("%Y%m%dT%H%M%S")),
            "DTEND;TZID=America/Los_Angeles:{}".format(
                end.strftime("%Y%m%dT%H%M%S")),
            "SUMMARY:{} {}".format(emoji, display_name),
            "DESCRIPTION:{}".format(description),
            "LOCATION:{}".format(location),
            "STATUS:CONFIRMED", "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ])
        count += 1

    lines.append("END:VCALENDAR")
    log.info("Generated {} entertainment events ({} movies, {} concerts)".format(
        count, len(movies or []), len(concerts or [])))
    return "\r\n".join(lines), count


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
    fitness_file = output_dir / config.get(
        "fitness_filename", "tice-creek-fitness.ics")
    entertainment_file = output_dir / config.get(
        "entertainment_filename", "tice-creek-entertainment.ics")

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
        fitness_file.write_text(
            generate_fitness_ics([], config)[0])
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

    # Resolve conflicts (Zumba wins over overlapping classes)
    filtered = resolve_conflicts(filtered)

    if filtered:
        log.info("")
        log.info("Beth's classes this period:")
        for cls in filtered:
            log.info("  {} {} {} - {} ({})".format(
                cls.get("day", ""), cls.get("date", ""),
                cls.get("time", ""), cls.get("name", ""),
                cls.get("instructor", "")))

    # Scrape Rossmoor entertainment (movies + concerts)
    log.info("")
    log.info("=" * 60)
    log.info("Rossmoor \u2013 Movies & Entertainment Sync")
    log.info("=" * 60)
    movies, concerts = scrape_entertainment(config)

    if movies:
        log.info("")
        log.info("Evening movies this month:")
        for mov in movies:
            log.info("  {} {} - {} ({})".format(
                mov.get("date", ""),
                datetime.fromisoformat(mov["start_iso"]).strftime(
                    "%I:%M %p").lstrip("0"),
                mov["title"], mov["movie_year"]))

    if concerts:
        log.info("")
        log.info("Evening concerts/events this month:")
        for evt in concerts:
            log.info("  {} {} - {} {}".format(
                evt.get("date", ""),
                datetime.fromisoformat(evt["start_iso"]).strftime(
                    "%I:%M %p").lstrip("0"),
                evt["title"],
                "({})".format(evt["cost"]) if evt.get("cost") else "(Free)"))

    # Generate separate ICS files
    fitness_ics, fitness_count = generate_fitness_ics(filtered, config)
    fitness_file.write_text(fitness_ics)

    entertainment_ics, ent_count = generate_entertainment_ics(
        movies, concerts, config)
    entertainment_file.write_text(entertainment_ics)

    log.info("")
    log.info("\u2705 Fitness    -> {} ({:,} bytes, {} events)".format(
        fitness_file, fitness_file.stat().st_size, fitness_count))
    log.info("\u2705 Entertainment -> {} ({:,} bytes, {} events)".format(
        entertainment_file, entertainment_file.stat().st_size, ent_count))


if __name__ == "__main__":
    main()
