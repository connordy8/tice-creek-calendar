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
import os
import re
import sys
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict
from zoneinfo import ZoneInfo

import yaml

# --- Configuration -------------------------------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

PACIFIC = ZoneInfo("America/Los_Angeles")
STUDIO_ID = 72039
LOCATION = "Tice Creek Fitness Center, 1751 Tice Creek Dr, Walnut Creek, CA 94595"

ROSSMOOR_MOVIE_PDF_URL = (
    "https://rossmoor.com/residents/recreation/movies-and-special-events/"
)
# As of May 2026 Rossmoor restructured their site. The movie/event
# calendar is now a JS-rendered widget on myrossmoor.com whose data
# is embedded as a base64-encoded inline <script>. We parse the JSON
# out of that script instead of downloading a PDF.
MYROSSMOOR_EVENTS_URL = "https://myrossmoor.com/events-calendar/"
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

def filter_classes(classes, config):
    raw_include = config.get("include_classes", [])
    exclude = [c.lower().strip() for c in config.get("exclude_classes", []) if c]
    earliest = config.get("earliest_hour")
    latest = config.get("latest_hour")
    # e.g. {"zumba": 10}: Zumba is worth getting up for at 10 AM
    hour_overrides = {k.lower(): v for k, v in
                      (config.get("earliest_hour_overrides") or {}).items()}

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


    filtered = []
    for cls in classes:
        if cls.get("cancelled"):
            continue
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
            floor = next((h for k, h in hour_overrides.items()
                          if k in combined), earliest)
            if floor is not None and hour < floor:
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

# Venue → full address mapping for events scraped from the widget
MYROSSMOOR_VENUE_LOCATIONS = {
    "Peacock Hall":
        "Peacock Hall, Gateway Complex, 1001 Golden Rain Rd, "
        "Walnut Creek, CA 94595",
    "Event Center":
        "Event Center, 1021 Stanley Dollar Dr, Walnut Creek, CA 94595",
    "Fireside Room":
        "Fireside Room, Gateway Complex, 1001 Golden Rain Rd, "
        "Walnut Creek, CA 94595",
    "Peacock Plaza":
        "Peacock Plaza, Gateway Complex, 1001 Golden Rain Rd, "
        "Walnut Creek, CA 94595",
}


# Known candidate URLs for the events calendar. We try each in order
# until one returns a parseable page. If Rossmoor reorganizes again,
# we just add a new candidate here — no code change to the parsing
# logic needed.
MYROSSMOOR_EVENT_URL_CANDIDATES = [
    "https://myrossmoor.com/events-calendar/",
    "https://myrossmoor.com/recreation-events/",
    "https://myrossmoor.com/events/",
    "https://myrossmoor.com/calendar/",
]

# Sanity thresholds — anything below these in a successful scrape
# suggests the page format changed even though we got SOME data back.
# Tuned conservatively: Rossmoor publishes ~2 months at a time with
# ~20+ items each.
MIN_EXPECTED_TOTAL_EVENTS = 10     # raise if scrape returns fewer
MIN_EXPECTED_MONTHS = 1            # at minimum we should get current month
EXPECTED_HEALTHY_TOTAL = 30        # below this → warn (not fatal)


def _fetch_events_page(url, timeout=30):
    """Download one events page candidate. Returns HTML or raises."""
    import urllib.request
    req = urllib.request.Request(url, headers={
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/121.0.0.0 Safari/537.36"
        ),
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        # Follow redirects implicitly; capture final URL so we log it
        final_url = resp.geturl()
        html = resp.read().decode("utf-8", errors="replace")
    return html, final_url


def _b64(s):
    import base64
    return base64.b64decode(s + "=" * (-len(s) % 4)).decode(
        "utf-8", errors="replace")


def _is_events_js(js):
    """Does this script hold the events data, in either known format?"""
    return ("movies" in js and (
        "monthName" in js or re.search(r"\bmovies\s*=\s*\[", js)))


def _parse_months(events_js):
    """Return [{monthName, year, monthIdx, movies, events}, ...].

    Two page formats seen so far:
      May 2026:  months=[{monthName, year, monthIdx, movies, events}, ...]
      Oct 2026:  movies=[...],events=[...] for one month, with the month
                 only in the calendar builder: new Date(2026,9,1)
    """
    import json as _json

    m = re.search(
        r"months\s*=\s*(\[\s*\{.*?\}\s*\])\s*;\s*(?:let|var|const)\s+af",
        events_js, re.DOTALL) or re.search(
        r"months\s*=\s*(\[\s*\{.*?\}\s*\])\s*;", events_js, re.DOTALL)
    if m:
        return _json.loads(m.group(1))

    decoder = _json.JSONDecoder()

    def _array(name):
        mm = re.search(r"\b{}\s*=\s*(?=\[)".format(name), events_js)
        if not mm:
            return None
        return decoder.raw_decode(events_js, mm.end())[0]

    movies = _array("movies")
    if movies is None:
        raise RuntimeError("Couldn't find months=[...] or movies=[...] "
                           "in events script")
    events = _array("events") or []

    # The grid is drawn from new Date(YEAR, MONTH_IDX, 1). Fall back to
    # the "October 2026 Special Events" style heading text if needed.
    dm = re.search(r"new Date\(\s*(\d{4})\s*,\s*(\d{1,2})\s*,\s*1\s*\)",
                   events_js)
    if dm:
        year, month_idx = int(dm.group(1)), int(dm.group(2))
    else:
        hm = re.search(
            r"'(January|February|March|April|May|June|July|August|"
            r"September|October|November|December) '\s*\+?\s*(\d{4})|"
            r"(January|February|March|April|May|June|July|August|"
            r"September|October|November|December) (\d{4})", events_js)
        if not hm:
            raise RuntimeError("Couldn't determine which month the "
                               "events data is for")
        name = hm.group(1) or hm.group(3)
        year = int(hm.group(2) or hm.group(4))
        month_idx = datetime.strptime(name, "%B").month - 1
    return [{
        "monthName": datetime(year, month_idx + 1, 1).strftime("%B"),
        "year": year,
        "monthIdx": month_idx,
        "movies": movies,
        "events": events,
    }]


def scrape_myrossmoor_events(url=MYROSSMOOR_EVENTS_URL):
    """Scrape the MyRossmoor Recreation Department events page.

    The page renders a filterable calendar widget. Events are encoded
    as a JSON object inside a base64-encoded inline <script> tag —
    we extract that JSON directly (no Playwright, no PDF parsing).

    Returns (movies, concerts) in the same shape as parse_recreation_pdf.
    Raises RuntimeError if the data looks empty or malformed, so the
    caller can fall back to a different source.
    """
    import base64
    import json as _json

    # Try the primary URL, then alternates if it returns nothing
    # parseable. Each candidate must contain the event data script,
    # otherwise we move on.
    candidates = [url]
    if url == MYROSSMOOR_EVENTS_URL:
        # Only use fallbacks when the caller didn't explicitly pin a URL
        for c in MYROSSMOOR_EVENT_URL_CANDIDATES:
            if c not in candidates:
                candidates.append(c)

    def _page_has_event_data(html_blob):
        """Quick sniff: does this page contain the events JSON
        anywhere, either in plain HTML or inside a base64 script?"""
        if not html_blob:
            return False
        if _is_events_js(html_blob):
            return True
        for s in re.findall(
                r'data:text/javascript;base64,([A-Za-z0-9+/=]+)',
                html_blob):
            try:
                decoded = _b64(s)
            except Exception:
                continue
            if _is_events_js(decoded):
                return True
        return False

    html = None
    final_url = None
    last_err = None
    for candidate in candidates:
        try:
            log.info("Fetching MyRossmoor events page: {}".format(
                candidate))
            html_candidate, final_url = _fetch_events_page(candidate)
            log.info("  Downloaded {:,} bytes from {}".format(
                len(html_candidate), final_url))
            if _page_has_event_data(html_candidate):
                html = html_candidate
                break
            log.warning(
                "  Page from {} doesn't appear to contain event "
                "data — trying next candidate".format(candidate))
        except Exception as e:
            last_err = e
            log.warning(
                "  Fetch failed for {}: {}".format(candidate, e))

    if not html:
        raise RuntimeError(
            "All MyRossmoor URL candidates failed. Last error: {}. "
            "Tried: {}. The recreation events page may have been "
            "moved again — update MYROSSMOOR_EVENT_URL_CANDIDATES."
            .format(last_err, candidates))

    # Find every inline base64-encoded script and decode each. The
    # one we want contains both 'monthName' and 'movies'.
    b64_scripts = re.findall(
        r'<script[^>]*src="data:text/javascript;base64,'
        r'([A-Za-z0-9+/=]+)"', html)
    log.info("  Inspecting {} inline scripts".format(len(b64_scripts)))

    events_js = None
    for b64 in b64_scripts:
        try:
            decoded = _b64(b64)
        except Exception:
            continue
        if _is_events_js(decoded):
            events_js = decoded
            break

    if not events_js:
        raise RuntimeError(
            "Couldn't find the events data script on {}. The page "
            "structure may have changed.".format(url))

    months = _parse_months(events_js)
    log.info("  Parsed {} month(s)".format(len(months)))

    movies = []
    concerts = []
    for mo in months:
        month_name = mo.get("monthName", "")
        year = int(mo.get("year", datetime.now().year))
        month_idx = int(mo.get("monthIdx", 0))  # 0-based
        month_num = month_idx + 1
        log.info("  {} {}: {} movies, {} events".format(
            month_name, year, len(mo.get("movies", [])),
            len(mo.get("events", []))))

        for mv in mo.get("movies", []):
            try:
                day = int(mv["date"])
            except (KeyError, ValueError, TypeError):
                continue
            showtimes = _parse_showtimes(
                mv.get("times", ""), year, month_num, day)
            for dt in showtimes:
                movies.append({
                    "title": mv.get("title", "").strip(),
                    "movie_year": (mv.get("year") or "").strip(),
                    "date": "{}-{:02d}-{:02d}".format(
                        year, month_num, day),
                    "start_iso": dt.strftime("%Y-%m-%dT%H:%M"),
                    "start_hour": dt.hour,
                    "start_dt": dt,
                    "is_movie": True,
                    "venue": mv.get("venue", "Peacock Hall"),
                    "rating": mv.get("rating", ""),
                    "runtime": mv.get("runtime", ""),
                    "series": mv.get("series", ""),
                    "cost": mv.get("cost", "Free"),
                })

        for ev in mo.get("events", []):
            try:
                day = int(ev["date"])
            except (KeyError, ValueError, TypeError):
                continue
            showtimes = _parse_showtimes(
                ev.get("times", ""), year, month_num, day)
            for dt in showtimes:
                venue = ev.get("venue", "Event Center")
                concerts.append({
                    "title": ev.get("title", "").strip(),
                    "event_type": "Special Event",
                    "date": "{}-{:02d}-{:02d}".format(
                        year, month_num, day),
                    "start_iso": dt.strftime("%Y-%m-%dT%H:%M"),
                    "start_hour": dt.hour,
                    "start_dt": dt,
                    "cost": ev.get("cost", "Free"),
                    "location_code": venue,
                    "venue": venue,
                    "is_concert": True,
                })

    total = len(movies) + len(concerts)
    log.info("  Total: {} movie showings, {} special events".format(
        len(movies), len(concerts)))

    # === Validation gate ===
    # Raise if the result looks broken so the caller can fall back
    # to the legacy PDF source or alert. The goal: never silently
    # return empty data and let the calendar sit stale for weeks.
    if len(months) < MIN_EXPECTED_MONTHS:
        raise RuntimeError(
            "Parsed {} month(s); expected at least {}. The page "
            "structure may have changed."
            .format(len(months), MIN_EXPECTED_MONTHS))
    if total < MIN_EXPECTED_TOTAL_EVENTS:
        raise RuntimeError(
            "Parsed only {} total events (movies + special events); "
            "expected at least {}. The data shape may have changed "
            "or Rossmoor may be between calendars."
            .format(total, MIN_EXPECTED_TOTAL_EVENTS))
    if total < EXPECTED_HEALTHY_TOTAL:
        log.warning(
            "  Only {} total events — below healthy threshold of {}. "
            "Sync will proceed but worth a manual check."
            .format(total, EXPECTED_HEALTHY_TOTAL))
    # Sanity check date range: events must span the next 7 days at
    # minimum, otherwise we may be parsing a stale archived page.
    from datetime import date as _date
    today = _date.today()
    future_window_end = today + timedelta(days=7)
    has_future_event = any(
        e["start_dt"].date() >= today and e["start_dt"].date()
        <= future_window_end + timedelta(days=60)
        for e in (movies + concerts))
    if not has_future_event:
        raise RuntimeError(
            "No events in the next 60 days — page may be showing "
            "archived data. Check {}.".format(MYROSSMOOR_EVENTS_URL))

    return movies, concerts




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

    Uses grid-based extraction since the PDF is a calendar grid layout
    where each day is a cell positioned by x/y coordinates.

    Returns (movies, concerts) where each is a list of dicts.
    """
    import pdfplumber
    from collections import defaultdict

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

            # Extract words with positions to find day cells
            words = page.extract_words(
                keep_blank_chars=True, x_tolerance=2)
            log.info("  Page {}: {} words extracted".format(
                page.page_number, len(words)))

            # Find day number positions
            day_cells = []
            for w in words:
                t = w["text"].strip()
                if t.isdigit() and 1 <= int(t) <= 31:
                    day_cells.append({
                        "day": int(t), "x": w["x0"], "y": w["top"]})

            log.info("  Found {} day cells".format(len(day_cells)))
            if not day_cells:
                continue

            # Group days by row (same y, tolerance of 10)
            rows = defaultdict(list)
            for dc in day_cells:
                row_key = round(dc["y"] / 10) * 10
                rows[row_key].append(dc)

            sorted_rows = sorted(rows.items())
            if not sorted_rows:
                continue

            # Get column x positions from first full row
            first_row = sorted(sorted_rows[0][1], key=lambda d: d["x"])
            col_xs = [d["x"] for d in first_row]

            # Build column x-ranges
            col_ranges = []
            for ci, x in enumerate(col_xs):
                x_start = x - 5
                x_end = (col_xs[ci + 1] - 5
                         if ci + 1 < len(col_xs) else page.width)
                col_ranges.append((x_start, x_end))

            # Extract text for each day cell
            for row_idx, (row_y, day_list) in enumerate(sorted_rows):
                day_list.sort(key=lambda d: d["x"])
                y_start = row_y - 5
                y_end = (sorted_rows[row_idx + 1][0] - 5
                         if row_idx + 1 < len(sorted_rows)
                         else page.height)

                for dc in day_list:
                    col_idx = min(
                        range(len(col_xs)),
                        key=lambda i: abs(col_xs[i] - dc["x"]))
                    x_start, x_end = col_ranges[col_idx]

                    # Clamp bbox to page dimensions
                    bx0 = max(0, x_start)
                    by0 = max(0, y_start)
                    bx1 = min(page.width, x_end)
                    by1 = min(page.height, y_end)
                    try:
                        cell = page.crop((bx0, by0, bx1, by1))
                        cell_text = cell.extract_text()
                    except Exception:
                        continue
                    if not cell_text:
                        continue

                    current_day = dc["day"]
                    cell_lines = cell_text.split('\n')

                    ci = 0
                    while ci < len(cell_lines):
                        cline = cell_lines[ci].strip()

                        # --- Movies ---
                        movie_match = re.search(
                            r'Movie:\s*[\u201c"\u2018\'](.*?)[\u201d"\u2019\']\s*'
                            r'\((\d{4})\)', cline)
                        if not movie_match:
                            # Title may span two lines
                            if cline.startswith("Movie:") and ci + 1 < len(cell_lines):
                                combined = cline + " " + cell_lines[ci + 1].strip()
                                movie_match = re.search(
                                    r'Movie:\s*[\u201c"\u2018\'](.*?)[\u201d"\u2019\']\s*'
                                    r'\((\d{4})\)', combined)
                                if movie_match:
                                    ci += 1
                                    cline = combined

                        if movie_match:
                            title = movie_match.group(1)
                            movie_year = movie_match.group(2)

                            # Look for showtime on next line
                            times_text = cline
                            if ci + 1 < len(cell_lines):
                                nl = cell_lines[ci + 1].strip()
                                if re.search(
                                        r'(\d|Noon)', nl, re.IGNORECASE):
                                    times_text += " " + nl
                                    ci += 1

                            showtimes = _parse_showtimes(
                                times_text, year, month_num, current_day)
                            for dt in showtimes:
                                date_str = "{}-{:02d}-{:02d}".format(
                                    year, month_num, current_day)
                                movies.append({
                                    "title": title,
                                    "movie_year": movie_year,
                                    "date": date_str,
                                    "start_iso": dt.strftime(
                                        "%Y-%m-%dT%H:%M"),
                                    "start_hour": dt.hour,
                                    "start_dt": dt,
                                    "is_movie": True,
                                })
                            ci += 1
                            continue

                        # --- Concerts & Spotlight events ---
                        concert_match = re.search(
                            r'(Concert|The Spotlight):\s*[\u201c"\u2018\']?'
                            r'(.*?)[\u201d"\u2019\']?\s*$', cline)
                        if concert_match:
                            event_type = concert_match.group(1).strip()
                            event_name = concert_match.group(2).strip()
                            event_name = event_name.strip(
                                '\u201c\u201d"\'')

                            # Name may continue on next line(s)
                            while (ci + 1 < len(cell_lines)
                                   and not re.search(
                                       r'(\d|Noon).*([ap]\.m\.|EC|FR|PH)',
                                       cell_lines[ci + 1],
                                       re.IGNORECASE)):
                                ci += 1
                                extra = cell_lines[ci].strip()
                                if extra.startswith(("Movie:", "Concert:",
                                                     "The Spotlight:")):
                                    ci -= 1
                                    break
                                event_name += " " + extra

                            event_name = event_name.strip(
                                '\u201c\u201d"\'')

                            # Gather time/location/cost
                            times_text = ""
                            cost = ""
                            location_code = ""
                            for la in range(1, 4):
                                if ci + la >= len(cell_lines):
                                    break
                                nl = cell_lines[ci + la].strip()
                                if re.search(
                                        r'([ap]\.m\.|Noon)',
                                        nl, re.IGNORECASE):
                                    times_text += " " + nl
                                    cost_m = re.search(
                                        r'\(\$(\d+)\)', nl)
                                    if cost_m:
                                        cost = "${}".format(
                                            cost_m.group(1))
                                    for code in [
                                            "EC", "FR", "PH", "CR", "G"]:
                                        if code in nl.split():
                                            location_code = code
                                            break
                                    ci += 1
                                else:
                                    break

                            if not times_text:
                                ci += 1
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
                                    "start_iso": dt.strftime(
                                        "%Y-%m-%dT%H:%M"),
                                    "start_hour": dt.hour,
                                    "start_dt": dt,
                                    "cost": cost,
                                    "location_code": location_code,
                                    "is_concert": True,
                                })

                        ci += 1

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
        'Noon, EC'
    """
    times = []

    # Handle "Noon" explicitly
    if re.search(r'\bNoon\b', text, re.IGNORECASE):
        try:
            times.append(datetime(year, month, day, 12, 0))
        except ValueError:
            pass
        return times

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


def scrape_entertainment(config):
    """Scrape Rossmoor movie + concert listings and return evening events.

    Returns (movies, concerts). Either is None when that source could
    not be scraped, which tells the calendar sync to leave existing
    events alone rather than treat "no data" as "everything was
    cancelled" and delete them.
    """
    include_movies = config.get("include_movies", True)
    include_concerts = config.get("include_concerts", True)

    if not include_movies and not include_concerts:
        log.info("Movies and concerts disabled in config, skipping")
        return [], []

    movie_hour = config.get("movie_earliest_hour", 19)
    concert_hour = config.get("concert_earliest_hour", 18)

    # Primary source: MyRossmoor events page (JSON in inline script).
    # Falls back to the legacy PDF parser only if the new source fails
    # — useful if Rossmoor restructures their site again.
    try:
        all_movies, all_concerts = scrape_myrossmoor_events()
    except Exception as e:
        log.warning("MyRossmoor scrape failed ({}), trying legacy PDF"
                    .format(e))
        pdf_path = None
        try:
            pdf_path = download_movie_pdf()
            all_movies, all_concerts = parse_recreation_pdf(pdf_path)
        except Exception as e2:
            log.error("Legacy PDF source also failed: {}".format(e2))
            return None, None
        finally:
            if pdf_path:
                try:
                    os.unlink(pdf_path)
                except OSError:
                    pass
        # The legacy URL now redirects to a marketing page. Never trust
        # a thin result from it: that's how calendars get wiped.
        if len(all_movies) + len(all_concerts) < MIN_EXPECTED_TOTAL_EVENTS:
            log.error("Legacy PDF returned only {} events; ignoring".format(
                len(all_movies) + len(all_concerts)))
            return None, None

    # --- Filter movies to evening showings ---
    evening_movies = []
    if include_movies:
        evening_movies = [
            m for m in all_movies if m["start_hour"] >= movie_hour]
        log.info("  Evening movies ({}:00+): {}".format(
            movie_hour, len(evening_movies)))
        log.info("  Looking up Rotten Tomatoes scores + descriptions...")
        try:
            from movie_info import enrich
            today = datetime.now().strftime("%Y-%m-%d")
            enrich([m for m in evening_movies if m["date"] >= today])
        except Exception as e:
            # Scores are nice-to-have; never block the movies themselves
            log.warning("  Movie info lookup failed: {}".format(e))

    # --- Filter concerts to evening ---
    evening_concerts = []
    if include_concerts:
        evening_concerts = [
            c for c in all_concerts if c["start_hour"] >= concert_hour]
        log.info("  Evening concerts ({}:00+): {}".format(
            concert_hour, len(evening_concerts)))

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
        email_notes = cls.get("email_notes", "")
        if email_notes:
            desc_parts.append("Note: {}".format(email_notes))
        if cls.get("is_manual"):
            desc_parts.append("Added via email to bethcalendarupdate@gmail.com")
        else:
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
            "LOCATION:{}".format(
                cls.get("location") or LOCATION),
            "STATUS:CONFIRMED", "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ])
        count += 1

    lines.append("END:VCALENDAR")
    log.info("Generated {} fitness events".format(count))
    return "\r\n".join(lines), count


def generate_entertainment_ics(movies, concerts, config,
                               fitness_classes=None):
    """Generate ICS for movies + concerts.

    If fitness_classes is provided, skip entertainment events that
    overlap with any fitness class.
    """
    cal_name = "Rossmoor \u2013 Movies & Events"
    movie_dur = config.get("movie_duration_minutes", 135)
    concert_dur = config.get("concert_duration_minutes", 120)
    default_class_dur = config.get("default_class_duration_minutes", 45)
    early_start = config.get("early_start_minutes", 0)
    now_utc = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    # Build fitness time ranges for conflict detection
    fitness_ranges = []
    for cls in (fitness_classes or []):
        try:
            fs = datetime.fromisoformat(cls["start_iso"])
            fd = cls.get("duration_minutes", default_class_dur)
            if fd <= 0:
                fd = default_class_dur
            fe = fs + timedelta(minutes=fd)
            fitness_ranges.append((fs, fe))
        except (ValueError, KeyError):
            pass

    def conflicts_with_fitness(evt_start, evt_end):
        """Return True if the entertainment event overlaps any fitness class."""
        for fs, fe in fitness_ranges:
            if evt_start < fe and evt_end > fs:
                return True
        return False

    lines = _ics_header(cal_name)
    count = 0
    skipped = 0

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
        if conflicts_with_fitness(start, end):
            skipped += 1
            continue

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
        if conflicts_with_fitness(start, end):
            skipped += 1
            continue

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
    if skipped:
        log.info("Skipped {} entertainment events due to fitness conflicts".format(
            skipped))
    log.info("Generated {} entertainment events ({} movies, {} concerts)".format(
        count, len(movies or []), len(concerts or [])))
    return "\r\n".join(lines), count


def generate_combined_ics(classes, movies, concerts, config):
    """Generate a single ICS with fitness + entertainment events."""
    cal_name = "Beth's Calendar"
    default_dur = config.get("default_class_duration_minutes", 45)
    movie_dur = config.get("movie_duration_minutes", 135)
    concert_dur = config.get("concert_duration_minutes", 120)
    early_start = config.get("early_start_minutes", 0)
    now_utc = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    lines = _ics_header(cal_name)
    fitness_count = 0
    ent_count = 0
    skipped = 0

    # Build fitness time ranges for conflict detection
    fitness_ranges = []
    for cls in (classes or []):
        try:
            fs = datetime.fromisoformat(cls["start_iso"])
            fd = cls.get("duration_minutes", default_dur)
            if fd <= 0:
                fd = default_dur
            fe = fs + timedelta(minutes=fd)
            fitness_ranges.append((fs, fe))
        except (ValueError, KeyError):
            pass

    def conflicts_with_fitness(evt_start, evt_end):
        for fs, fe in fitness_ranges:
            if evt_start < fe and evt_end > fs:
                return True
        return False

    # --- Fitness classes ---
    for cls in (classes or []):
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
        email_notes = cls.get("email_notes", "")
        if email_notes:
            desc_parts.append("Note: {}".format(email_notes))
        if cls.get("is_manual"):
            desc_parts.append("Added via email to bethcalendarupdate@gmail.com")
        else:
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
            "LOCATION:{}".format(cls.get("location") or LOCATION),
            "STATUS:CONFIRMED", "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ])
        fitness_count += 1

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
        if conflicts_with_fitness(start, end):
            skipped += 1
            continue

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
        ent_count += 1

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
        if conflicts_with_fitness(start, end):
            skipped += 1
            continue

        title = evt["title"]
        event_type = evt.get("event_type", "Concert")
        cost = evt.get("cost", "")
        loc_code = evt.get("location_code", "EC")
        location = ROSSMOOR_LOCATIONS.get(
            loc_code, ROSSMOOR_LOCATIONS["EC"])

        if "Spotlight" in event_type:
            emoji = "\U0001f3b5"
            display_name = "Spotlight: {}".format(title)
        else:
            emoji = "\U0001f3b6"
            display_name = "Concert: {}".format(title)

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
        ent_count += 1

    lines.append("END:VCALENDAR")
    total = fitness_count + ent_count
    if skipped:
        log.info("Skipped {} entertainment events due to fitness conflicts".format(
            skipped))
    log.info("Generated {} total events ({} fitness, {} entertainment)".format(
        total, fitness_count, ent_count))
    return "\r\n".join(lines), total


# =========================================================================
# Manual events (from email handler)
# =========================================================================

MANUAL_EVENTS_FILE = Path("manual_events.json")


def load_manual_events():
    """Load manual events created by the email handler."""
    if not MANUAL_EVENTS_FILE.exists():
        return []
    try:
        with open(MANUAL_EVENTS_FILE) as f:
            events = json.load(f)
        if not isinstance(events, list):
            log.error("manual_events.json is not a list")
            return None
        log.info("Loaded {} manual event(s) from email handler".format(
            len(events)))
        return events
    except (json.JSONDecodeError, IOError) as e:
        # None (not []) so the sync leaves existing appointments alone
        log.error("Could not load manual events: {}".format(e))
        return None


def _names_match(target, cls):
    """Email targets say "zumba club"; Mindbody says "CLUB: Zumba"."""
    return any(target in (cls.get(k) or "").lower()
               for k in ("name", "display_name"))


def apply_manual_events(classes, manual_events):
    """Apply email-sourced changes to the scraped class list.

    Handles three action types:
      - cancel: remove matching class on that date
      - modify: replace matching class with new time/details
      - add: inject a new event into the class list
    """
    cancels = [e for e in manual_events if e.get("type") == "cancel"]
    modifies = [e for e in manual_events if e.get("type") == "modify"]
    adds = [e for e in manual_events if e.get("type") == "add"]

    # Apply cancellations
    for cancel in cancels:
        target = cancel.get("original_class", "").lower()
        target_date = cancel.get("date", "")
        if not target or not target_date:
            continue
        before = len(classes)
        classes = [
            c for c in classes
            if not (c.get("date") == target_date
                    and _names_match(target, c))
        ]
        removed = before - len(classes)
        if removed:
            log.info("  Cancelled {} event(s) matching '{}' on {}".format(
                removed, target, target_date))

    # Apply modifications
    for mod in modifies:
        target = mod.get("original_class", "").lower()
        target_date = mod.get("date", "")
        new_time = mod.get("start_time", "")
        if not target or not target_date:
            continue

        for cls in classes:
            if (cls.get("date") == target_date
                    and _names_match(target, cls)):
                if new_time:
                    # Update start time
                    new_iso = "{}T{}".format(target_date, new_time)
                    cls["start_iso"] = new_iso
                    try:
                        new_dt = datetime.fromisoformat(new_iso)
                        cls["time"] = new_dt.strftime(
                            "%I:%M %p").lstrip("0")
                        cls["start_hour"] = new_dt.hour
                    except ValueError:
                        pass
                if mod.get("end_time"):
                    end_iso = "{}T{}".format(target_date, mod["end_time"])
                    cls["end_iso"] = end_iso
                    try:
                        end_dt = datetime.fromisoformat(end_iso)
                        start_dt = datetime.fromisoformat(cls["start_iso"])
                        cls["end_time"] = end_dt.strftime(
                            "%I:%M %p").lstrip("0")
                        cls["duration_minutes"] = int(
                            (end_dt - start_dt).total_seconds() / 60)
                    except ValueError:
                        pass
                notes = mod.get("notes", "")
                if notes:
                    cls["email_notes"] = notes
                log.info("  Modified '{}' on {} -> {}".format(
                    target, target_date, new_time or "updated"))

    # Apply additions (new events from email)
    for add_evt in adds:
        date = add_evt.get("date", "")
        start_time = add_evt.get("start_time", "")
        if not date or not start_time:
            continue

        start_iso = "{}T{}".format(date, start_time)
        try:
            start_dt = datetime.fromisoformat(start_iso)
        except ValueError:
            continue

        end_time = add_evt.get("end_time", "")
        duration = 60  # default 1 hour for manual events
        if end_time:
            try:
                end_dt = datetime.fromisoformat(
                    "{}T{}".format(date, end_time))
                duration = int((end_dt - start_dt).total_seconds() / 60)
            except ValueError:
                pass

        new_cls = {
            "name": add_evt.get("title", "Event"),
            "raw_name": "email_event",
            "start_iso": start_iso,
            "end_iso": "{}T{}".format(date, end_time) if end_time else "",
            "date": date,
            "day": start_dt.strftime("%A"),
            "time": start_dt.strftime("%I:%M %p").lstrip("0"),
            "start_hour": start_dt.hour,
            "instructor": "",
            "source": "email",
            "duration_minutes": duration,
            "location": add_evt.get("location", ""),
            "email_notes": add_evt.get("notes", ""),
            "is_manual": True,
        }
        classes.append(new_cls)
        log.info("  Added email event: '{}' on {} at {}".format(
            new_cls["name"], date, start_time))

    return classes


# =========================================================================
# Discovery mode
# =========================================================================

def main():
    """Scrape every source and sync to Beth's calendar.

    Each source (Tice Creek classes, Rossmoor movies/concerts) succeeds
    or fails on its own: one broken website never blocks the others,
    and a failed source leaves its existing calendar events untouched.
    The process exits non-zero at the end if anything failed, so the
    GitHub Actions run goes red and the problem is visible.
    """
    config = load_config()
    output_dir = Path(config.get("output_dir", "docs"))
    output_dir.mkdir(parents=True, exist_ok=True)
    combined_file = output_dir / config.get(
        "combined_filename", "beth-calendar.ics")
    failures = []

    log.info("=" * 60)
    log.info("Tice Creek Fitness Center \u2013 Class Schedule")
    log.info("=" * 60)

    from tice_schedule import fetch_all
    all_classes, class_coverage, class_errors = fetch_all()
    for label, err in class_errors.items():
        failures.append("Tice Creek {}: {}".format(label, err))
    log.info("Total scraped: {}".format(len(all_classes)))

    Path("debug").mkdir(exist_ok=True)
    with open("debug/all_classes.json", "w") as f:
        json.dump(all_classes, f, indent=2, default=str)

    # Filter to Beth's preferences, then Zumba wins any overlap
    filtered = resolve_conflicts(filter_classes(all_classes, config))

    # Email-forwarded changes: cancel/modify adjust class listings, adds
    # become their own appointment events.
    manual = load_manual_events()
    if manual is None:
        appointments = None
        failures.append("manual_events.json unreadable")
    else:
        filtered = apply_manual_events(
            filtered, [m for m in manual
                       if m.get("type") in ("cancel", "modify")])
        appointments = [m for m in manual if m.get("type") == "add"
                        and m.get("date") and m.get("start_time")]
    if filtered:
        log.info("")
        log.info("Beth's classes this period:")
        for cls in filtered:
            log.info("  {} {} {} - {} ({})".format(
                cls.get("day", ""), cls.get("date", ""),
                cls.get("time", ""), cls.get("name", ""),
                cls.get("instructor", "")))

    log.info("")
    log.info("=" * 60)
    log.info("Rossmoor \u2013 Movies & Entertainment Sync")
    log.info("=" * 60)
    try:
        movies, concerts = scrape_entertainment(config)
    except Exception:
        log.exception("Entertainment scrape crashed")
        movies, concerts = None, None
    if movies is None or concerts is None:
        failures.append("Rossmoor movies/concerts: scrape failed")

    for mov in movies or []:
        log.info("  {} {} - {} ({}) {}".format(
            mov.get("date", ""),
            datetime.fromisoformat(mov["start_iso"]).strftime(
                "%I:%M %p").lstrip("0"),
            mov["title"], mov["movie_year"], mov.get("rt_score", "")))
    for evt in concerts or []:
        log.info("  {} {} - {} {}".format(
            evt.get("date", ""),
            datetime.fromisoformat(evt["start_iso"]).strftime(
                "%I:%M %p").lstrip("0"),
            evt["title"],
            "({})".format(evt["cost"]) if evt.get("cost") else "(Free)"))

    if os.environ.get("GOOGLE_SERVICE_ACCOUNT_KEY"):
        from gcal_sync import sync_to_google_calendar
        log.info("")
        log.info("=" * 60)
        log.info("Syncing to Google Calendar...")
        log.info("=" * 60)
        try:
            sync_to_google_calendar(filtered, movies, concerts, config,
                                    class_coverage=class_coverage,
                                    appointments=appointments)
        except Exception as e:
            log.exception("Google Calendar sync failed")
            failures.append("Google Calendar sync: {}".format(e))
    else:
        # Local/dev fallback: write an ICS file instead
        combined_ics, total_count = generate_combined_ics(
            filtered, movies or [], concerts or [], config)
        combined_file.write_text(combined_ics)
        log.info("")
        log.info("\u2705 Calendar -> {} ({:,} bytes, {} events)".format(
            combined_file, combined_file.stat().st_size, total_count))

    if failures:
        log.error("")
        log.error("Sync finished with problems:")
        for f in failures:
            log.error("  \u274c {}".format(f))
        sys.exit(1)
    log.info("")
    log.info("\u2705 All sources synced")


if __name__ == "__main__":
    main()
