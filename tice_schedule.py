"""Fetch the Tice Creek class schedule from Mindbody's branded-web widget.

In May 2026 Tice Creek's schedule pages switched from the old healcode
DOM widget to Mindbody's Next.js "branded web" widget (an iframe on
go.mindbodyonline.com). That page is server-rendered and embeds the
next 7 days of classes as JSON in its Next.js flight payload, so a plain
HTTP GET gives us structured data: no Playwright, no login.

Each class carries capacity, numberRegistered, cancelled, waitlistable,
bookingWindowStart/End, staff, roomName and description, which is
everything Beth needs to decide whether to sign up.

Widget IDs are resolved at runtime from the healcode IDs embedded on
ticefitnesscenter.com (healcode → branded-web mapping JSON), with the
last-known IDs as a fallback, so a widget swap on their side doesn't
silently break us.
"""

import html as html_lib
import json
import logging
import re
import time
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger("tice_schedule")

PACIFIC = ZoneInfo("America/Los_Angeles")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36")

SCHEDULE_PAGES = {
    "group_fitness": "https://www.ticefitnesscenter.com/schedule/",
    "aquatics": "https://www.ticefitnesscenter.com/aquatic-schedule/",
}

# Last-known branded-web widget IDs (Oct 2026). Used only if runtime
# discovery fails.
FALLBACK_WIDGET_IDS = {
    "group_fitness": "3355016f2c5",
    "aquatics": "3355017f2c5",
}

HEALCODE_MAP_URL = (
    "https://widgets.mindbodyonline.com/widgets/schedules/{}.json"
    "?mobile=false&version=1")
WIDGET_URL = (
    "https://go.mindbodyonline.com/book/widgets/schedules/view/{}/schedule")

# A healthy week at Tice Creek has 100+ group fitness sessions. Far
# fewer means the payload shape changed, not that the gym is empty.
MIN_EXPECTED_SESSIONS = {"group_fitness": 30, "aquatics": 3}


class ScheduleError(RuntimeError):
    pass


def _get(url, timeout=30, retries=3):
    last = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            last = e
            if attempt < retries:
                time.sleep(2 * attempt)
    raise ScheduleError("GET {} failed: {}".format(url, last))


def resolve_widget_id(label):
    """Find the current branded-web widget ID for a Tice Creek page."""
    try:
        page = _get(SCHEDULE_PAGES[label])
        hc = re.search(r'<healcode-widget[^>]*data-widget-id="([^"]+)"', page)
        if hc:
            mapping = json.loads(_get(HEALCODE_MAP_URL.format(hc.group(1))))
            m = re.search(r'data-widget-id="([^"]+)"',
                          mapping.get("contents", ""))
            if m:
                return m.group(1)
        # Page might embed the branded widget directly some day
        m = re.search(r'schedules/view/([0-9a-z]+)/schedule', page)
        if m:
            return m.group(1)
        log.warning("  Couldn't discover widget id for %s; using fallback",
                    label)
    except Exception as e:
        log.warning("  Widget discovery for %s failed (%s); using fallback",
                    label, e)
    return FALLBACK_WIDGET_IDS[label]


def parse_widget_html(page):
    """Extract the raw class list from a branded-web widget page."""
    chunks = re.findall(
        r'self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>', page, re.S)
    if not chunks:
        raise ScheduleError("No Next.js flight payload in widget page")
    payload = "".join(json.loads('"{}"'.format(c)) for c in chunks)
    key = '"initialClasses":'
    i = payload.find(key)
    if i < 0:
        raise ScheduleError("initialClasses missing from widget payload")
    decoder = json.JSONDecoder()
    obj, _ = decoder.raw_decode(payload, i + len(key))
    classes = obj.get("classes") if isinstance(obj, dict) else None
    if not isinstance(classes, list):
        raise ScheduleError("initialClasses has unexpected shape")

    # The flight format de-duplicates repeated objects into separate
    # rows ("2f:{...}") and references them as "$2f". Resolve those so
    # e.g. staff entries come back as dicts, not "$2f" strings.
    rows = {}

    def _row(ref_id):
        if ref_id not in rows:
            m = re.search(r'(?:^|\n){}:(?=[\[{{])'.format(re.escape(ref_id)),
                          payload)
            rows[ref_id] = (decoder.raw_decode(payload, m.end())[0]
                            if m else None)
        return rows[ref_id]

    def _resolve(v, depth=0):
        if depth > 10:
            return v
        if isinstance(v, str) and re.fullmatch(r"\$[0-9a-f]+", v):
            target = _row(v[1:])
            return v if target is None else _resolve(target, depth + 1)
        if isinstance(v, list):
            return [_resolve(x, depth + 1) for x in v]
        if isinstance(v, dict):
            return {k: _resolve(x, depth + 1) for k, x in v.items()}
        return v

    return [_resolve(c) for c in classes]


def _clean_text(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html_lib.unescape(s).replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def _utc_to_pacific(s):
    # "2026-10-03T17:00:00.0000000Z" -> aware Pacific datetime
    s = re.sub(r"\.\d+Z$", "Z", s).replace("Z", "+00:00")
    return datetime.fromisoformat(s).astimezone(PACIFIC)


def normalize(raw, label):
    """Turn a raw Mindbody class into the dict shape the rest of the
    pipeline uses (naive Pacific ISO strings, like the old scraper)."""
    start = _utc_to_pacific(raw["startDateTime"])
    end = _utc_to_pacific(raw["endDateTime"])
    staff = [s for s in (raw.get("staff") or []) if isinstance(s, dict)]
    instructor = (staff[0].get("displayLabel") or "").strip() if staff else ""
    name = re.sub(r"\s+", " ", raw.get("name") or "").strip()
    is_club = (instructor.upper() == "CLUB CLASS"
               or name.upper().startswith("CLUB"))
    if instructor.upper() == "CLUB CLASS":
        instructor = ""

    def _ts(key):
        v = raw.get(key)
        return _utc_to_pacific(v).replace(tzinfo=None).isoformat() if v else ""

    return {
        "mindbody_id": raw.get("id", ""),
        "source": label,
        "name": name,
        "raw_name": name,
        "instructor": instructor,
        "room": re.sub(r"\s+", " ", raw.get("roomName") or "").strip(),
        "description": _clean_text(raw.get("description")),
        "start_iso": start.replace(tzinfo=None).isoformat(timespec="minutes"),
        "end_iso": end.replace(tzinfo=None).isoformat(timespec="minutes"),
        "date": start.strftime("%Y-%m-%d"),
        "day": start.strftime("%A"),
        "time": start.strftime("%I:%M %p").lstrip("0"),
        "start_hour": start.hour,
        "duration_minutes": int(raw.get("duration") or
                                (end - start).total_seconds() // 60),
        "capacity": int(raw.get("capacity") or 0),
        "registered": int(raw.get("numberRegistered") or 0),
        "cancelled": bool(raw.get("cancelled")),
        "waitlistable": bool(raw.get("waitlistable")),
        "bookable": bool(raw.get("bookable")),
        "bookable_online": bool(raw.get("isBookableOnline")),
        "booking_opens": _ts("bookingWindowStart"),
        "booking_closes": _ts("bookingWindowEnd"),
        "is_club": is_club,
        "is_aquatics": label == "aquatics" or any(
            w in name.lower() for w in ("aqua", "water", "swim")),
    }


def fetch_schedule(label):
    """Fetch and normalize one schedule (group_fitness or aquatics).

    Returns (classes, (first_date, last_date)) where the date range is
    the span the widget actually covered, so callers only reconcile
    calendar events inside it. Raises ScheduleError when the data looks
    broken.
    """
    widget_id = resolve_widget_id(label)
    log.info("  %s: widget %s", label, widget_id)
    raw = parse_widget_html(_get(WIDGET_URL.format(widget_id)))
    classes = []
    for r in raw:
        try:
            classes.append(normalize(r, label))
        except Exception as e:
            log.warning("  Skipping unparseable class %s: %s",
                        r.get("name", "?"), e)
    if len(classes) < MIN_EXPECTED_SESSIONS[label]:
        raise ScheduleError(
            "{}: only {} sessions parsed (expected {}+); widget format "
            "may have changed".format(
                label, len(classes), MIN_EXPECTED_SESSIONS[label]))
    dates = sorted(c["date"] for c in classes)
    log.info("  %s: %d sessions, %s to %s", label, len(classes),
             dates[0], dates[-1])
    return classes, (dates[0], dates[-1])


def fetch_all():
    """Fetch every schedule. Returns (classes, coverage, errors) where
    coverage maps label -> (first_date, last_date) for schedules that
    succeeded and errors maps label -> message for those that didn't."""
    classes, coverage, errors = [], {}, {}
    for label in SCHEDULE_PAGES:
        try:
            got, span = fetch_schedule(label)
            classes.extend(got)
            coverage[label] = span
        except Exception as e:
            log.error("  %s schedule failed: %s", label, e)
            errors[label] = str(e)
    return classes, coverage, errors


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    cls, cov, err = fetch_all()
    for c in cls:
        print(c["date"], c["time"], c["name"], "|", c["instructor"], "|",
              c["room"], "| cap", c["capacity"], "reg", c["registered"],
              "| club" if c["is_club"] else "",
              "| CANCELLED" if c["cancelled"] else "")
    print(cov, err)
