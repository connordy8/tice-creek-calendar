"""Google Calendar API sync for Beth's calendar.

Pushes Tice Creek class listings, Rossmoor movies and concerts, and
email-forwarded appointments onto Beth's Google Calendar.

Safety rules (the calendar is Beth's real calendar, so a bug here
deletes things she relies on):

  1. We only ever touch events whose IDs carry one of our prefixes.
     Nothing Beth or anyone else created is ever modified or deleted.
  2. Each category (classes, movies, concerts, appointments) is
     reconciled only if its source scraped successfully. A source
     passed as None means "unknown", never "everything was cancelled".
  3. Events that have already started are never deleted.
  4. Class events are only removed inside the date range the Tice Creek
     widget actually covered on this run.
  5. Circuit breaker: if a run would delete more than half of a
     category's upcoming events (and more than a handful), it deletes
     nothing for that category and reports a failure instead.

Google Calendar color IDs:
  1  Lavender       5  Banana      9  Blueberry
  2  Sage           6  Tangerine  10  Basil
  3  Grape          7  Peacock    11  Tomato
  4  Flamingo       8  Graphite
"""

import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

log = logging.getLogger("gcal_sync")

PACIFIC = ZoneInfo("America/Los_Angeles")

# Color IDs for different event types
COLOR_FITNESS = "2"      # Sage (green)
COLOR_MOVIE = "9"        # Blueberry (blue/purple)
COLOR_CONCERT = "6"      # Tangerine (orange)

SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Prefixes for event IDs we manage. Google Calendar IDs must use only
# lowercase a-v and digits 0-9.
EVENT_ID_PREFIX = "be0ca1"        # movies + concerts (and legacy fitness)
CLASS_EVENT_PREFIX = "be0cc3"     # Tice Creek class listings
MANUAL_EVENT_PREFIX = "be0cd4"    # appointments forwarded by email
# One-off movie events added by add_movies.py (Oct 2026), superseded by
# this sync. Treated as movies so the first good run replaces them.
LEGACY_MOVIE_PREFIX = "be0cb2"
ALL_PREFIXES = (EVENT_ID_PREFIX, CLASS_EVENT_PREFIX, MANUAL_EVENT_PREFIX,
                LEGACY_MOVIE_PREFIX)

TICE_CREEK_ADDRESS = (
    "Tice Creek Fitness Center, 1751 Tice Creek Dr, Walnut Creek, CA 94595")
SIGNUP_URL = "https://www.ticefitnesscenter.com/schedule/"
AQUATICS_SIGNUP_URL = "https://www.ticefitnesscenter.com/aquatic-schedule/"

MASS_DELETE_MIN = 5          # always allow deleting up to this many
MASS_DELETE_FRACTION = 0.5   # beyond that, refuse if > this fraction


class SyncError(RuntimeError):
    pass


def get_calendar_service():
    """Authenticate and return a Google Calendar API service."""
    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_KEY")
    if not creds_json:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_KEY env var not set")

    creds_info = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(
        creds_info, scopes=SCOPES)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def gcal_api_call(fn, max_retries=4, **kwargs):
    """Execute a Google Calendar API call with retry on transient errors.

    Retries on 429 (rate limit), 500, 502, 503 and network errors with
    exponential backoff. Other HTTP errors (403, 404, 409...) raise
    immediately so callers can handle them.
    """
    for attempt in range(1, max_retries + 1):
        try:
            return fn(**kwargs).execute()
        except HttpError as e:
            status = e.resp.status if hasattr(e, "resp") else 0
            if status in (429, 500, 502, 503) and attempt < max_retries:
                wait = 2 ** attempt
                log.warning("  API error {} (attempt {}/{}), retrying in {}s"
                            .format(status, attempt, max_retries, wait))
                time.sleep(wait)
            else:
                raise
        except (OSError, TimeoutError) as e:
            if attempt < max_retries:
                log.warning("  Network error ({}), retrying".format(e))
                time.sleep(2 ** attempt)
            else:
                raise


def make_event_id(prefix, unique_str):
    """Create a deterministic Google Calendar event ID.

    Google requires event IDs to be 5-1024 chars, lowercase a-v and 0-9.
    We use a hex hash (0-9, a-f) which is a valid subset.
    """
    raw = "{}-{}".format(prefix, unique_str)
    h = hashlib.md5(raw.encode()).hexdigest()
    return "{}{}".format(EVENT_ID_PREFIX, h)


def _hash_id(prefix, unique_str):
    return prefix + hashlib.md5(unique_str.encode()).hexdigest()


def _now():
    return datetime.now(PACIFIC).replace(tzinfo=None)


def _event_start(item):
    """Naive Pacific datetime for an event returned by the API."""
    s = item.get("start", {})
    raw = s.get("dateTime") or s.get("date")
    if not raw:
        return None
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo:
        dt = dt.astimezone(PACIFIC).replace(tzinfo=None)
    return dt


def _timed(start, end):
    return {
        "start": {"dateTime": start.strftime("%Y-%m-%dT%H:%M:%S"),
                  "timeZone": "America/Los_Angeles"},
        "end": {"dateTime": end.strftime("%Y-%m-%dT%H:%M:%S"),
                "timeZone": "America/Los_Angeles"},
    }


def _fmt_time(dt):
    return dt.strftime("%I:%M %p").lstrip("0")


def _minutes(text, default):
    m = re.search(r"\d+", str(text or ""))
    return int(m.group()) if m and int(m.group()) > 0 else default


# =========================================================================
# Event builders
# =========================================================================

def _class_display_name(cls, config):
    name = cls.get("name", "Class")
    instr = cls.get("instructor", "").lower()
    for rule in config.get("custom_titles", []) or []:
        if (rule.get("match_name", "").lower() in name.lower()
                and rule.get("match_instructor", "").lower() in instr):
            return rule["title"]
    return cls.get("display_name") or name


def class_status(cls, now):
    """(short title tag, description line) for a class's sign-up state."""
    cap = cls.get("capacity", 0)
    left = cap - cls.get("registered", 0)
    opens = cls.get("booking_opens")
    opens_dt = datetime.fromisoformat(opens) if opens else None

    if cls.get("autobooked"):
        return ("\u2705 booked",
                "BOOKED: The calendar bot signed Beth up for this class. "
                "If she can't make it, she should cancel in the Mindbody "
                "app so someone else gets the spot.")
    if cls.get("is_club") or cap <= 0:
        return ("drop-in",
                "No sign-up needed: this is a drop-in class, just show up.")
    if opens_dt and now < opens_dt:
        when = "{} at {}".format(opens_dt.strftime("%a %b %-d"),
                                 _fmt_time(opens_dt))
        return ("sign-up opens {}".format(opens_dt.strftime("%a")),
                "SIGN-UP REQUIRED. Sign-up opens {} ({} spots).".format(
                    when, cap))
    if cls.get("bookable"):
        if 0 < left <= cap:
            return ("sign up · {} left".format(left),
                    "SIGN-UP REQUIRED. Spots available: {} of {} left."
                    .format(left, cap))
        return ("sign up",
                "SIGN-UP REQUIRED. Spots are available.")
    if cls.get("waitlistable"):
        return ("FULL · waitlist",
                "SIGN-UP REQUIRED. Class is full, but the waitlist is open.")
    return ("FULL", "SIGN-UP REQUIRED. Class is full (no waitlist).")


def build_class_event(cls, config, now):
    start = datetime.fromisoformat(cls["start_iso"])
    dur = cls.get("duration_minutes") or config.get(
        "default_class_duration_minutes", 45)
    end = start + timedelta(minutes=dur)
    early = config.get("early_start_minutes", 0)
    tag, status_line = class_status(cls, now)
    aquatic = cls.get("is_aquatics")
    emoji = "\U0001f3ca" if aquatic else "\U0001f3cb️"
    name = _class_display_name(cls, config)

    lines = [status_line]
    if tag not in ("drop-in", "\u2705 booked"):
        lines.append("Beth signs up herself on the Mindbody app or at {}"
                     .format(AQUATICS_SIGNUP_URL if aquatic else SIGNUP_URL))
        lines.append("(This listing can't see Beth's own bookings. If "
                     "she's already signed up, she's all set.)")
    lines.append("")
    lines.append("Class time: {} - {} ({} min). Calendar starts {} min "
                 "early for travel.".format(_fmt_time(start),
                                            _fmt_time(end), dur, early))
    if cls.get("instructor"):
        lines.append("Instructor: {}".format(cls["instructor"]))
    if cls.get("room"):
        lines.append("Room: {}".format(cls["room"]))
    if cls.get("email_notes"):
        lines.append("Note: {}".format(cls["email_notes"]))
    if cls.get("description"):
        lines.append("")
        lines.append(cls["description"])
    lines.append("")
    lines.append("Availability as of {}.".format(
        now.strftime("%a %b %-d, %-I:%M %p")))

    location = TICE_CREEK_ADDRESS
    if cls.get("room"):
        location = "{} - {}".format(cls["room"], TICE_CREEK_ADDRESS)

    key = cls.get("mindbody_id") or "{}-{}".format(
        cls.get("name"), cls["start_iso"])
    body = {
        "summary": "{} {} · {}".format(emoji, name, tag),
        "description": "\n".join(lines),
        "location": location,
        "colorId": COLOR_FITNESS,
        "extendedProperties": {"private": {
            "bethbot": "class", "source": cls.get("source", "")}},
    }
    body.update(_timed(start - timedelta(minutes=early), end))
    return _hash_id(CLASS_EVENT_PREFIX, key), body


def build_movie_event(mov, config):
    from scraper import MOVIE_LOCATION
    start = datetime.fromisoformat(mov["start_iso"])
    runtime = _minutes(mov.get("runtime"),
                       config.get("movie_duration_minutes", 135))
    end = start + timedelta(minutes=runtime)
    early = config.get("early_start_minutes", 0)
    title = mov["title"]
    score = mov.get("rt_score")

    lines = []
    if mov.get("description"):
        lines += [mov["description"], ""]
    lines.append("Showtime: {} at {}".format(
        _fmt_time(start), mov.get("venue") or "Peacock Hall"))
    details = [d for d in (mov.get("movie_year"),
                           "{} min".format(runtime) if mov.get("runtime")
                           else "", mov.get("rating")) if d]
    if details:
        lines.append(" · ".join(details))
    if mov.get("series"):
        lines.append("Series: {}".format(mov["series"]))
    if score:
        lines.append("Rotten Tomatoes: {}{}".format(
            score, " ({})".format(mov["rt_url"]) if mov.get("rt_url")
            else ""))
    lines.append("Free admission" if (mov.get("cost") or "Free") == "Free"
                 else "Cost: {}".format(mov["cost"]))
    lines.append("Source: myrossmoor.com/events-calendar")

    eid = make_event_id("movie", "{}-{}-{}".format(
        title, mov.get("date", ""), mov["start_iso"]))
    body = {
        "summary": "\U0001f3ac {}{}".format(
            title, " \U0001f345 {}".format(score) if score else ""),
        "description": "\n".join(lines),
        "location": MOVIE_LOCATION,
        "colorId": COLOR_MOVIE,
    }
    body.update(_timed(start - timedelta(minutes=early), end))
    return eid, body


def build_concert_event(evt, config):
    from scraper import MYROSSMOOR_VENUE_LOCATIONS, ROSSMOOR_LOCATIONS
    start = datetime.fromisoformat(evt["start_iso"])
    end = start + timedelta(minutes=config.get("concert_duration_minutes",
                                               120))
    early = config.get("early_start_minutes", 0)
    title = evt["title"]
    venue = evt.get("venue") or evt.get("location_code") or "EC"
    location = (MYROSSMOOR_VENUE_LOCATIONS.get(venue)
                or ROSSMOOR_LOCATIONS.get(venue)
                or "{}, Rossmoor, Walnut Creek, CA 94595".format(venue))
    if "Spotlight" in (evt.get("event_type", "") + title):
        emoji, display = "\U0001f3b5", title
    else:
        emoji, display = "\U0001f3b6", title

    cost = evt.get("cost", "")
    lines = ["{} at {}".format(_fmt_time(start), venue)]
    if cost and cost != "Free":
        lines.append("Tickets: {}".format(cost))
        lines.append("Tickets at Recreation Dept, Gateway, "
                     "Mon-Fri 8am-4:30pm")
    else:
        lines.append("Free admission")
    lines.append("Source: myrossmoor.com/events-calendar")

    eid = make_event_id("concert", "{}-{}-{}".format(
        title, evt.get("date", ""), evt["start_iso"]))
    body = {
        "summary": "{} {}".format(emoji, display),
        "description": "\n".join(lines),
        "location": location,
        "colorId": COLOR_CONCERT,
    }
    body.update(_timed(start - timedelta(minutes=early), end))
    return eid, body


def build_manual_event(evt):
    start = datetime.fromisoformat("{}T{}".format(evt["date"],
                                                  evt["start_time"]))
    end = start + timedelta(hours=1)
    if evt.get("end_time"):
        try:
            end = datetime.fromisoformat("{}T{}".format(evt["date"],
                                                        evt["end_time"]))
        except ValueError:
            pass
    if end <= start:
        end = start + timedelta(hours=1)
    lines = []
    if evt.get("notes"):
        lines.append(evt["notes"])
    if evt.get("source"):
        lines.append("From: {}".format(evt["source"]))
    lines.append("Added from an email to bethcalendarupdate@gmail.com")
    body = {
        "summary": evt.get("title") or "Appointment",
        "description": "\n".join(lines),
        "location": evt.get("location", ""),
    }
    body.update(_timed(start, end))
    key = evt.get("uid") or "{}-{}-{}".format(
        evt.get("title"), evt["date"], evt["start_time"])
    return _hash_id(MANUAL_EVENT_PREFIX, key), body


# =========================================================================
# Sync
# =========================================================================

def _category(eid, item):
    if eid.startswith(CLASS_EVENT_PREFIX):
        return "classes"
    if eid.startswith(MANUAL_EVENT_PREFIX):
        return "appointments"
    if eid.startswith(LEGACY_MOVIE_PREFIX):
        return "movies"
    if eid.startswith(EVENT_ID_PREFIX):
        color = item.get("colorId")
        if color == COLOR_MOVIE:
            return "movies"
        if color == COLOR_CONCERT:
            return "concerts"
        return "legacy"   # old scraper-made fitness events
    return None


_GENERIC_WORDS = {"class", "club", "with", "and", "the", "for", "caar",
                  "aqua", "new", "apt"}


def _sig_words(text):
    # 3+ letters so "Tai Chi" / "Mat Yoga" count
    return {w for w in re.findall(r"[a-z]+", (text or "").lower())
            if len(w) >= 3 and w not in _GENERIC_WORDS}


def _beth_already_has(cls, others):
    """True if Beth's own calendar (events we don't manage, e.g. ones the
    Mindbody app adds when she books) already has this class: same
    name word, starting within 20 minutes."""
    start = datetime.fromisoformat(cls["start_iso"])
    words = _sig_words(cls.get("name"))
    for item in others:
        other_start = _event_start(item)
        if other_start is None:
            continue
        if abs((other_start - start).total_seconds()) <= 20 * 60 and (
                words & _sig_words(item.get("summary"))):
            return True
    return False


def _needs_update(old, new):
    if old.get("status") == "cancelled":
        return True
    for k in ("summary", "location", "colorId"):
        if (old.get(k) or "") != (new.get(k) or ""):
            return True
    # Ignore the "as of" timestamp so classes aren't rewritten every run
    strip = lambda d: re.sub(r"Availability as of .*", "", d or "")
    if strip(old.get("description")) != strip(new.get("description")):
        return True
    for k in ("start", "end"):
        a = old.get(k, {}).get("dateTime")
        b = new.get(k, {}).get("dateTime")
        if not a or not b:
            return True
        da = datetime.fromisoformat(a)
        db = datetime.fromisoformat(b).replace(tzinfo=PACIFIC)
        if da.tzinfo is None:
            da = da.replace(tzinfo=PACIFIC)
        if da != db:
            return True
    return False


def sync_to_google_calendar(classes, movies, concerts, config,
                            class_coverage=None, appointments=None):
    """Reconcile Beth's calendar with freshly scraped data.

    Pass None for any source that failed to scrape: its events are left
    exactly as they are. class_coverage maps schedule label (e.g.
    "group_fitness") -> (first_date, last_date) actually fetched.

    Returns (created, updated, deleted). Raises SyncError after doing
    everything it safely can if any category hit a problem.
    """
    calendar_id = os.environ.get("GOOGLE_CALENDAR_ID", "primary")
    service = get_calendar_service()
    now = _now()
    problems = []

    # --- Find existing managed events ---
    time_min = (datetime.utcnow() - timedelta(days=7)).isoformat() + "Z"
    time_max = (datetime.utcnow() + timedelta(days=90)).isoformat() + "Z"
    existing = {}
    others = []   # Beth's own events: read-only, used to avoid duplicates
    page_token = None
    while True:
        resp = gcal_api_call(
            service.events().list,
            calendarId=calendar_id, timeMin=time_min, timeMax=time_max,
            maxResults=2500, singleEvents=True, showDeleted=True,
            pageToken=page_token)
        for item in resp.get("items", []):
            eid = item.get("id", "")
            if eid.startswith(ALL_PREFIXES):
                existing[eid] = item
            elif (item.get("status") != "cancelled"
                  and item.get("start", {}).get("dateTime")):
                others.append(item)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    log.info("  Found {} existing managed events".format(len(existing)))

    # --- Build desired events per category ---
    desired = {}          # eid -> body
    desired_upcoming = {}  # category -> count of future desired events
    reconcile = set()     # categories whose source succeeded

    builders = [
        ("classes", classes, lambda c: build_class_event(c, config, now)),
        ("movies", movies, lambda m: build_movie_event(m, config)),
        ("concerts", concerts, lambda c: build_concert_event(c, config)),
        ("appointments", appointments, build_manual_event),
    ]
    if classes is not None:
        from zumba_autobook import booked_class_ids
        ours = booked_class_ids()
        for c in classes:
            c["autobooked"] = c.get("mindbody_id") in ours
    for cat, items, build_fn in builders:
        if cat == "classes" and items is not None:
            mine = [c for c in items if _beth_already_has(c, others)]
            for c in mine:
                log.info("  Beth already has {} on {} {}; not listing it"
                         .format(c.get("name"), c.get("date"), c.get("time")))
            items = [c for c in items if c not in mine]
        if items is None:
            log.warning("  {}: source unavailable, leaving existing events "
                        "untouched".format(cat))
            continue
        reconcile.add(cat)
        n = 0
        for item in items:
            try:
                eid, body = build_fn(item)
            except Exception as e:
                log.warning("  Skipping bad {} entry {}: {}".format(
                    cat, item.get("title") or item.get("name"), e))
                continue
            end = datetime.fromisoformat(body["end"]["dateTime"])
            if end < now:
                continue  # already over; don't clutter the past
            desired[eid] = body
            n += 1
        desired_upcoming[cat] = n
        log.info("  Desired {}: {}".format(cat, n))
    if classes is not None and not class_coverage:
        # No coverage info means we can't scope deletions safely
        reconcile.discard("classes")

    # --- Create / update ---
    created = updated = 0
    for eid, body in desired.items():
        old = existing.get(eid)
        try:
            if old is None:
                try:
                    gcal_api_call(service.events().insert,
                                  calendarId=calendar_id,
                                  body=dict(body, id=eid))
                    created += 1
                    continue
                except HttpError as e:
                    if e.resp.status != 409:
                        raise
                    # Exists outside our query window (or was deleted):
                    # fall through to update, which also resurrects it.
            elif not _needs_update(old, body):
                continue
            gcal_api_call(service.events().update, calendarId=calendar_id,
                          eventId=eid, body=dict(body, status="confirmed"))
            updated += 1
        except HttpError as e:
            if e.resp.status in (403, 429):
                raise SyncError("Calendar API refused writes ({}): {}"
                                .format(e.resp.status, e))
            log.warning("  Failed to write {} ({}): {}".format(
                eid, body.get("summary"), e))
            problems.append("write failed: {}".format(body.get("summary")))

    # --- Delete what's no longer wanted (with guards) ---
    to_delete = {}
    upcoming = {}
    for eid, item in existing.items():
        if item.get("status") == "cancelled":
            continue
        cat = _category(eid, item)
        start = _event_start(item)
        if cat is None or start is None or start <= now:
            continue  # rule 3: never delete started/past events
        upcoming.setdefault(cat, 0)
        upcoming[cat] += 1
        if eid in desired:
            continue
        if cat == "legacy":
            to_delete.setdefault(cat, []).append(eid)
            continue
        if cat not in reconcile:
            continue  # rule 2
        if cat == "classes":
            props = item.get("extendedProperties", {}).get("private", {})
            span = class_coverage.get(props.get("source", ""))
            day = start.strftime("%Y-%m-%d")
            if not span or not (span[0] <= day <= span[1]):
                continue  # rule 4
        to_delete.setdefault(cat, []).append(eid)

    deleted = 0
    for cat, eids in to_delete.items():
        # Swapping events for replacements is fine; losing most of a
        # category with nothing to replace it is what a broken scrape
        # looks like.
        total = upcoming.get(cat, 0)
        if (cat != "legacy" and len(eids) > MASS_DELETE_MIN
                and desired_upcoming.get(cat, 0)
                < MASS_DELETE_FRACTION * total):
            msg = ("{}: refusing to delete {} of {} upcoming events when "
                   "only {} replacements were scraped (circuit breaker). "
                   "Check the scrape.".format(
                       cat, len(eids), total, desired_upcoming.get(cat, 0)))
            log.error("  " + msg)
            problems.append(msg)
            continue
        for eid in eids:
            try:
                gcal_api_call(service.events().delete,
                              calendarId=calendar_id, eventId=eid)
                deleted += 1
                log.info("  Removed ({}): {}".format(
                    cat, existing[eid].get("summary", "")))
            except HttpError as e:
                if e.resp.status in (404, 410):
                    continue
                log.warning("  Failed to delete {}: {}".format(eid, e))

    log.info("Sync complete: {} created, {} updated, {} deleted".format(
        created, updated, deleted))
    if problems:
        raise SyncError("; ".join(problems))
    return created, updated, deleted


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    # Quick test: just authenticate and list upcoming events
    service = get_calendar_service()
    calendar_id = os.environ.get("GOOGLE_CALENDAR_ID", "primary")
    log.info("Testing connection to calendar: {}".format(calendar_id))

    resp = service.events().list(
        calendarId=calendar_id,
        maxResults=5,
        singleEvents=True,
        orderBy="startTime",
        timeMin=datetime.utcnow().isoformat() + "Z",
    ).execute()

    items = resp.get("items", [])
    log.info("Found {} upcoming events".format(len(items)))
    for item in items:
        log.info("  {} - {}".format(
            item.get("start", {}).get("dateTime", "all-day"),
            item.get("summary", "(no title)")))
