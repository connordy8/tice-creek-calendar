"""Sign Beth up for her favorite classes, without hoarding spots.

Connor's rules (Oct 2026), configured under `autobook` in config.yaml:

  * Classes, in priority order (Connor's ranking for conflicts): Zumba,
    Aquacise (Bob Huff's only; she doesn't like the other instructors),
    UJAM, Posture/Balance/Core and Strength, Mat Yoga.
  * Book a class only the day before, or sooner once it's down to its
    last couple of spots. The Tice Creek staff don't like people
    signing up for lots of future classes.
  * Never a class that starts before 11 AM.
  * At most one of each class a day (the latest session when several
    qualify) and at most two classes a day in total. Never two that
    overlap, and never over anything else on Beth's calendar. Classes
    Beth booked herself (any class, not just these) count.
  * Never pay, never join a waitlist, never cancel anything.

Modes:

  python autobook.py --check       # what's due; no browser, no booking
  python autobook.py               # check, then book what's due
  python autobook.py --watch       # between 3:00 and 8:40 AM Pacific,
                                   # keep checking until 8:40 (every 20s
                                   # from 7:40: sign-ups open at 8 AM and
                                   # popular classes fill in minutes);
                                   # otherwise the same as no flag
  python autobook.py --dry-run     # sign in and go as far as Mindbody's
                                   # "Confirm booking" for the first due
                                   # class, without pressing it
  python autobook.py --dry-run --class-id cinst_...
                                   # same, for a class that passes every
                                   # rule except "due"

Booking drives Mindbody's branded-web schedule widget (the page Tice
Creek embeds). Per the widget's own code: Sign Up opens a sign-in
popup if needed, then a cart page; only the cart's "Confirm booking"
books anything. The bot presses it only for a free booking: a price
above $0 (other than the cancellation-policy fees), a card form, a
waiver, a pass to choose, or a purchase/waitlist button stops it.

Failure handling: every attempt is recorded in autobook_state.json
(committed by the workflow). A failure after "Confirm booking" was
pressed is never retried and still counts toward that day's limits (it
may have gone through); a failure before it is retried a few times, and
a cart that wants something the bot won't do isn't retried. Problems
email Connor (screenshots attached), at most once a day per problem.
The repo is public, so the Actions log shows signed-in pages only as
Mindbody's own UI words (anything else, like Beth's name, is masked).
After Mindbody rejects the password, sign-in pauses until the
MINDBODY_PASSWORD secret changes, so a stale password can't lock her
account.
"""

import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import tice_schedule

log = logging.getLogger("autobook")

PACIFIC = ZoneInfo("America/Los_Angeles")
STATE_FILE = Path("autobook_state.json")
DEBUG_DIR = Path("debug")
CONFIG_FILE = Path("config.yaml")

DEFAULT_RULES = {
    "classes": [],
    "earliest_hour": 11,
    "book_days_ahead": 1,
    "max_spots_left": 2,
    "max_per_day": 2,
    "buffer_minutes": 15,
    "min_lead_minutes": 120,
}

# How long one problem stays quiet after Connor has been emailed
ALERT_EVERY_HOURS = 24
# A class that filled up before we got to it is retried after this
# long, up to this many attempts
UNAVAILABLE_RETRY_MINUTES = 60
UNAVAILABLE_MAX_ATTEMPTS = 4
# A failure before "Confirm booking" was pressed (page didn't load, row
# not found, cart never appeared) is retried after this long, up to
# this many attempts. Before that button nothing can have been booked:
# Sign Up only opens a cart.
ERROR_RETRY_MINUTES = 15
ERROR_MAX_ATTEMPTS = 3
# After a sign-in failure that isn't a wrong password (verification
# code, unexpected page), wait this long before signing in again
SIGNIN_PAUSE_HOURS = {"otp": 24, "stuck": 6, "locked": 24}
# A repeat of the same sign-in problem pauses longer each time; after
# this many in a row it stays paused until Connor resets it
SIGNIN_MAX_REPEATS = 3
# Two calendar entries this close together are the same class
SAME_CLASS_MINUTES = 20
# Runs that start in this Pacific-time window keep watching until the
# end of it: sign-ups open at 8:00 AM, 7 days ahead, and popular classes
# fill within minutes. GitHub's scheduler drops most runs around then,
# so any run from 3 AM on stays up for it.
WATCH_FROM = (3, 0)
WATCH_RUSH = (7, 40)      # poll every 20s from here...
WATCH_UNTIL = (8, 40)     # ...to here
WATCH_SLOW_SECONDS = 600  # before the rush, check every 10 min
WATCH_FAST_SECONDS = 20
# Alert if no 8 AM watch has run for this many days
WATCH_MISSED_DAYS = 3
# Alert if the calendar sync workflow hasn't run for this long (it
# checks this workflow the same way)
PEER_STALE_HOURS = 24

# --- Mindbody's own wording (from the widget's text catalog, Oct 2026) ---
# Sign Up never books by itself: it opens a cart page (/book/cartv2/...)
# or shows one of these messages on the schedule.
# Exact sentences only: the family-account attendee step shows "Already
# booked" next to *other* people.
ALREADY_RE = re.compile(r"\b(you are|all attendees are) already booked for "
                        r"this class", re.I)
# Mindbody shows these when she may already hold the class (it can't
# tell us which), so they count as "maybe booked", never as "full"
GONE_RE = re.compile(r"can no longer be booked", re.I)
ALREADY_WAITLISTED_RE = re.compile(r"already on the waitlist", re.I)
CART_OPEN_ERROR_RE = re.compile(r"couldn'?t open your cart", re.I)
# The cart's success screen
SUCCESS_RE = re.compile(r"you'?re booked!?|booking confirmed", re.I)
WAITLISTED_RE = re.compile(r"added to the waitlist|on the waitlist", re.I)
# Anything on the cart that means "don't go on"
# (pattern, why, kind): "retry" = a passing Mindbody error (nothing was
# booked; try again later), "stop" = it wants something the bot won't do
CART_STOP_PATTERNS = [
    (r"booking failed|cannot be made|couldn'?t complete your booking|"
     r"something went wrong|error occurred|session has expired",
     "Mindbody reported an error", "retry"),
    (r"unable to complete this booking|suspended",
     "Mindbody won't let her book online", "stop"),
    (r"requires payment|card was declined|payment could not|"
     r"payment couldn'?t|complete your payment|scan qr", "it wants a payment",
     "stop"),
    (r"pay after your service|pricing will be determined|due after service",
     "it would book her as pay-later (unpaid)", "stop"),
    (r"membership (is )?required|do not have the membership|"
     r"requires an active membership", "it says she needs a membership",
     "stop"),
    (r"prerequisite|complete something else first", "a prerequisite", "stop"),
    (r"liability waiver|type your full name", "a liability waiver to sign",
     "stop"),
    (r"select a booking option|is required for this booking|"
     r"select an option below", "it wants a pass or pricing option chosen",
     "stop"),
    (r"who are you booking for|select the person who will attend",
     "it asks who the booking is for (family account)", "stop"),
    (r"request (has been )?submitted|request booking", "request-only class",
     "stop"),
]
# The one button that books: "Confirm booking", optionally with a $0
# total ("Confirm booking · $0.00"). Never "Confirm purchase", "Pay",
# "Join waitlist" or "Request booking".
CONFIRM_BUTTON_RE = re.compile(
    r"^\s*(confirm booking|book|book now|confirm)"
    r"(\s*·\s*\$\s*0(\.00)?)?\s*$", re.I)
FORBIDDEN_BUTTON_RE = re.compile(
    r"purchase|\bpay\b|\bbuy\b|waitlist|request|add card|checkout", re.I)
MONEY_RE = re.compile(r"\$\s*([0-9]+(?:,[0-9]{3})*(?:\.[0-9]{2})?)")
# The cart's cancellation-policy banner quotes fees ("Late cancellations
# will be charged $5.00"): those sentences aren't a price for booking.
POLICY_SENTENCE_RE = re.compile(
    r"[^.\n]*\b(late cancel\w*|no-?shows?|cancellation fees?|cancellations?"
    r" or no-show)\b[^.\n]*[.\n]?", re.I)
CARD_SELECTOR = ("input[autocomplete^=cc-], iframe[src*=pay], "
                 "iframe[title*=card i], iframe[name*=card i]")
# Sign-in pages
OTP_RE = re.compile(r"enter the code|verification code|one-time|"
                    r"we sent (you )?a code|check your email", re.I)
REJECTED_RE = re.compile(r"incorrect password|wrong password|"
                         r"password (is )?incorrect|invalid (email|password|"
                         r"credentials)|doesn'?t match", re.I)
# Lockouts and rate limits: a timed pause, not "wrong password"
LOCKED_RE = re.compile(r"too many|try again later|temporarily locked|"
                       r"account (is )?locked", re.I)
# Row-button labels the widget uses for a class that can't be booked
# (its i18n catalog); the book button itself is the widget's
# bookButtonText ("Sign Up" at Tice Creek). Anything else is a change
# worth an email.
UNBOOKABLE_LABELS = {"full", "closed", "not yet open", "call to book",
                     "unavailable", "cancelled", "join waitlist",
                     "members only"}
EMAIL_INPUT = ("#username, input[type=email], input[autocomplete=username], "
               "input[name=email]")

# Attempt results that mean Beth has (or may have) the class: they count
# toward the day's limits and are never retried. Only "booked" (Mindbody
# said "You're booked!" to us) gets the ✅ on her calendar.
COMMITTED = ("booked", "booked-by-beth", "maybe-booked", "failed")


class BookingError(RuntimeError):
    """kind: "error" (retry later), "blocked" (Mindbody needs something
    the bot won't do, e.g. a payment or waiver: don't retry this class).
    detail: screen text for Connor's email, never stored or logged raw."""

    def __init__(self, message, kind="error", detail=""):
        super().__init__(message)
        self.kind = kind
        self.detail = detail


class SignInError(BookingError):
    def __init__(self, message, kind, detail=""):
        # "rejected" | "otp" | "locked" | "stuck"
        super().__init__(message, kind, detail)


def _now():
    return datetime.now(PACIFIC).replace(tzinfo=None)


def _dt(s):
    return datetime.fromisoformat(s)


# =========================================================================
# Config + state
# =========================================================================

def load_rules(path=CONFIG_FILE, text=None):
    """Rules from config.yaml (or YAML `text`). Raises ValueError on a
    malformed `autobook` section, so a bad edit stops booking with a
    clear email instead of booking by surprise."""
    import yaml
    config = yaml.safe_load(text if text is not None
                            else Path(path).read_text()) or {}
    rules = dict(DEFAULT_RULES)
    rules.update(config.get("autobook") or {})
    classes = []
    for entry in rules.get("classes") or []:
        if isinstance(entry, str):
            entry = {"name": entry}
        name = (entry.get("name") or "").strip().lower()
        if name:
            classes.append({
                "name": name,
                "instructor": (entry.get("instructor") or "").strip().lower(),
            })
    rules["classes"] = classes
    rules["exclude"] = [e.lower() for e in config.get("exclude_classes") or []
                        if e]
    # Every class Beth likes (for recognising ones she booked herself)
    rules["include"] = [
        (e.get("name") if isinstance(e, dict) else e).strip().lower()
        for e in config.get("include_classes") or []
        if (e.get("name") if isinstance(e, dict) else e)]
    _check_rules(rules)
    return rules


def _check_rules(rules):
    ints = {"earliest_hour": (0, 23), "book_days_ahead": (0, 7),
            "max_spots_left": (0, 100), "max_per_day": (0, 10),
            "buffer_minutes": (0, 240), "min_lead_minutes": (0, 1440)}
    for k, (lo, hi) in ints.items():
        v = rules.get(k)
        if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
            raise ValueError("config.yaml autobook.{} must be a whole number "
                             "from {} to {} (got {!r})".format(k, lo, hi, v))
    if not isinstance(rules.get("enabled", True), bool):
        raise ValueError("config.yaml autobook.enabled must be true or false")
    names = [r["name"] for r in rules["classes"]]
    if len(set(names)) != len(names):
        raise ValueError("config.yaml autobook.classes has a duplicate name")


def load_state(path=STATE_FILE):
    state = {}
    if path.exists():
        state = json.loads(path.read_text())
    state.setdefault("bookings", {})   # class id -> attempt record
    state.setdefault("alerts", {})     # problem key -> last emailed
    state.setdefault("signin_pause", {})
    state.setdefault("last_watch", "")
    return state


def save_state(state, path=STATE_FILE, now=None):
    now = now or _now()
    cutoff = (now - timedelta(days=21)).strftime("%Y-%m-%d")
    state["bookings"] = {k: v for k, v in state["bookings"].items()
                         if v.get("date", "9999") >= cutoff}
    alert_cutoff = (now - timedelta(days=7)).isoformat()
    state["alerts"] = {k: v for k, v in state["alerts"].items()
                       if v >= alert_cutoff}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def merge_states(ours, theirs):
    """Combine two versions of the state file (used when a push races
    another run). Per class, the record that commits Beth wins, then the
    newer one; alerts keep the latest time; the newer sign-in pause
    wins."""
    rank = {r: i for i, r in enumerate(
        ("error", "unavailable", "blocked", "failed", "maybe-booked",
         "booked-by-beth", "booked"))}
    key = lambda r: (rank.get(r.get("result"), -1), r.get("attempts", 0),
                     r.get("at", ""))
    out = {"bookings": dict(theirs["bookings"]), "alerts": dict(
        theirs["alerts"]), "signin_pause": theirs.get("signin_pause") or {}}
    for cid, rec in ours["bookings"].items():
        old = out["bookings"].get(cid)
        if old is None or key(rec) > key(old):
            out["bookings"][cid] = rec
    for k, v in ours["alerts"].items():
        out["alerts"][k] = max(v, out["alerts"].get(k, ""))
    mine = ours.get("signin_pause") or {}
    if mine.get("since", "") > out["signin_pause"].get("since", ""):
        out["signin_pause"] = mine
    out["last_watch"] = max(ours.get("last_watch", ""),
                            theirs.get("last_watch", ""))
    return out


def booked_class_ids(path=STATE_FILE):
    """Mindbody class ids the bot booked and Mindbody confirmed ("You're
    booked!"); gcal_sync marks them ✅. Classes Beth booked herself, or
    may hold, aren't claimed."""
    try:
        return {k for k, v in load_state(path)["bookings"].items()
                if v.get("result") == "booked"}
    except Exception as e:
        log.warning("Couldn't read %s: %s", path, e)
        return set()


# =========================================================================
# Choosing what to book (pure functions: see test_autobook.py)
# =========================================================================

_GENERIC_WORDS = {"class", "club", "with", "and", "the", "for", "aqua",
                  "new", "apt"}


def _sig_words(text):
    return {w for w in re.findall(r"[a-z]+", (text or "").lower())
            if len(w) >= 3 and w not in _GENERIC_WORDS}


def class_type(cls, rules):
    """The autobook rule name this Mindbody class falls under, or None."""
    if cls.get("is_club") or cls.get("cancelled"):
        return None
    name = re.sub(r"\s+", " ", cls.get("name") or "").lower()
    if any(x in name for x in rules["exclude"]):
        return None
    instructor = re.sub(r"\s+", " ", cls.get("instructor") or "").lower()
    for rule in rules["classes"]:
        if rule["name"] not in name:
            continue
        if rule["instructor"] and not re.search(
                r"\b{}\b".format(re.escape(rule["instructor"])), instructor):
            continue
        return rule["name"]
    return None


def summary_type(summary, rules):
    """Class type of one of Beth's own calendar entries (e.g. the
    " Aqua: Aquacise" the Mindbody app adds when she books)."""
    text = re.sub(r"\s+", " ", summary or "").lower()
    for rule in rules["classes"]:
        if rule["name"] in text:
            return rule["name"]
    return None


def is_class_event(ev, rules, day_classes):
    """Is this entry on Beth's calendar a fitness class (any kind)? Either
    it names a class she likes, or it lines up with a Tice Creek class
    on the schedule (same time, a shared name word)."""
    text = re.sub(r"\s+", " ", ev["summary"]).lower()
    if summary_type(text, rules) or any(k in text for k in rules["include"]):
        return True
    words = _sig_words(text)
    for c in day_classes:
        if abs((_dt(c["start_iso"]) - ev["start"]).total_seconds()) <= \
                SAME_CLASS_MINUTES * 60 and words & _sig_words(c["name"]):
            return True
    return False


def spots_left(cls):
    return cls["capacity"] - cls["registered"]


def bookable_now(cls, rules, now):
    """Open for sign-up with a real free spot, at/after 11 AM, and far
    enough ahead for Beth to notice. Says nothing about *when* to book."""
    if not cls.get("bookable") or cls.get("cancelled"):
        return False
    if cls["capacity"] <= 0 or not 1 <= spots_left(cls) <= cls["capacity"]:
        return False
    start = _dt(cls["start_iso"])
    if start.hour < rules["earliest_hour"]:
        return False
    return start - now >= timedelta(minutes=rules["min_lead_minutes"])


def due_reason(cls, rules, now):
    """Why this class should be booked now, or None. Doesn't look at
    Beth's calendar: plan() does that."""
    if not bookable_now(cls, rules, now):
        return None
    start = _dt(cls["start_iso"])
    last_day = now.date() + timedelta(days=rules["book_days_ahead"])
    if start.date() <= last_day:
        return "day before" if start.date() > now.date() else "today"
    left = spots_left(cls)
    if left <= rules["max_spots_left"]:
        return "{} left".format(left)
    return None


def _overlaps(start, end, busy, buffer):
    pad = timedelta(minutes=buffer)
    for b_start, b_end, _ in busy:
        if start < b_end + pad and b_start < end + pad:
            return True
    return False


def day_commitments(date, state, events, rules, day_classes=()):
    """What Beth already has on `date`: (taken, busy).

    taken: [(type or None, start)] every class she has that day: ours
           (including failed attempts, which may have gone through) and
           her own (any class, typed when it's one of the auto-book
           types), de-duplicated (the Mindbody app may add its own entry
           for a class we booked). len(taken) is the day's class count.
    busy:  [(start, end, label)] everything she's committed to that
           touches the day, including multi-day events.
    day_classes: the schedule for that date, to recognise her own class
           entries and drop bookings the gym has since cancelled.
    """
    taken, busy = [], []
    day_start = _dt(date)
    day_end = day_start + timedelta(days=1)
    cancelled = {c["mindbody_id"] for c in day_classes if c.get("cancelled")}

    def _add_taken(ctype, start):
        for i, (t, s) in enumerate(taken):
            if abs((s - start).total_seconds()) <= SAME_CLASS_MINUTES * 60 \
                    and (t == ctype or t is None or ctype is None):
                if t is None:
                    taken[i] = (ctype, s)
                return
        taken.append((ctype, start))

    for cid, rec in state["bookings"].items():
        if rec.get("date") != date or rec.get("result") not in COMMITTED:
            continue
        if cid in cancelled:
            continue  # the gym cancelled it: frees the slot
        start, end = _dt(rec["start"]), _dt(rec["end"])
        _add_taken(rec.get("type"), start)
        busy.append((start, end, rec.get("name", rec.get("type"))))
    for ev in events:
        if not (ev["start"] < day_end and ev["end"] > day_start):
            continue
        busy.append((ev["start"], ev["end"], ev["summary"]))
        if ev.get("all_day") or ev["start"] < day_start:
            continue
        if is_class_event(ev, rules, day_classes):
            _add_taken(summary_type(ev["summary"], rules), ev["start"])
    return taken, busy


def _retry_ok(rec, now):
    """Can a class with this attempt record be tried again now?"""
    if rec is None:
        return True
    at, attempts = rec.get("at", ""), rec.get("attempts", 1)
    if rec.get("result") == "unavailable":
        return attempts < UNAVAILABLE_MAX_ATTEMPTS and at <= (now - timedelta(
            minutes=UNAVAILABLE_RETRY_MINUTES)).isoformat()
    if rec.get("result") == "error":
        return attempts < ERROR_MAX_ATTEMPTS and at <= (now - timedelta(
            minutes=ERROR_RETRY_MINUTES)).isoformat()
    return False  # booked / failed / blocked: never again


def plan(classes, rules, now, state, events, only_id=None):
    """Classes to book now, in booking order: [(class, type, reason)].

    events: Beth's own calendar entries (and forwarded appointments) as
    {"start", "end", "summary", "all_day"} naive Pacific datetimes.
    only_id: rehearsal target; it must pass every rule except "due".
    """
    priority = {r["name"]: i for i, r in enumerate(rules["classes"])}
    due, by_date = {}, {}
    for c in classes:
        by_date.setdefault(c["date"], []).append(c)
        ctype = class_type(c, rules)
        if not ctype:
            continue
        if only_id:
            if c["mindbody_id"] != only_id:
                continue
            reason = due_reason(c, rules, now) or (
                "requested" if bookable_now(c, rules, now) else None)
        else:
            reason = due_reason(c, rules, now)
        if not reason:
            continue
        if not _retry_ok(state["bookings"].get(c["mindbody_id"]), now):
            continue
        due.setdefault(c["date"], []).append((ctype, c, reason))

    targets = []
    for date in sorted(due):
        taken, busy = day_commitments(date, state, events, rules,
                                      by_date.get(date, []))
        types_taken = {t for t, _ in taken if t}
        count = len(taken)
        if count >= rules["max_per_day"]:
            log.info("  %s: already has %d classes; booking nothing more",
                     date, count)
            continue
        for rule in rules["classes"]:
            ctype = rule["name"]
            options = sorted((x for x in due[date] if x[0] == ctype),
                             key=lambda x: x[1]["start_iso"], reverse=True)
            if not options:
                continue
            if ctype in types_taken:
                log.info("  %s: already has %s", date, ctype)
                continue
            if count >= rules["max_per_day"]:
                log.info("  %s: %s is due but the day is full", date, ctype)
                continue
            for _, c, reason in options:  # latest session first
                start, end = _dt(c["start_iso"]), _dt(c["end_iso"])
                if _overlaps(start, end, busy, rules["buffer_minutes"]):
                    log.info("  %s %s %s: clashes with something on her "
                             "calendar", date, c["time"], c["name"])
                    continue
                targets.append((c, ctype, reason))
                busy.append((start, end, c["name"]))
                types_taken.add(ctype)
                count += 1
                break
    targets.sort(key=lambda t: (t[0]["date"], priority[t[1]]))
    return targets


# =========================================================================
# Inputs: schedule + Beth's calendar
# =========================================================================

def fetch_classes():
    """All Tice Creek classes. Returns (classes, errors)."""
    classes, _, errors = tice_schedule.fetch_all()
    return classes, errors


def calendar_events(items):
    """Beth's commitments from Google Calendar API items: her own events
    and forwarded appointments, not our class/movie listings. All-day
    events count only when marked busy (Google's default for all-day
    events is "free", which keeps birthdays and reminders out)."""
    from gcal_sync import ALL_PREFIXES, MANUAL_EVENT_PREFIX
    events = []
    to_local = lambda v: datetime.fromisoformat(v).astimezone(
        PACIFIC).replace(tzinfo=None)
    for item in items:
        eid = item.get("id", "")
        if eid.startswith(ALL_PREFIXES) and \
                not eid.startswith(MANUAL_EVENT_PREFIX):
            continue
        if item.get("status") == "cancelled":
            continue
        if item.get("eventType") in ("birthday", "workingLocation"):
            continue
        s, e = item.get("start", {}), item.get("end", {})
        if s.get("dateTime") and e.get("dateTime"):
            events.append({"start": to_local(s["dateTime"]),
                           "end": to_local(e["dateTime"]),
                           "summary": item.get("summary") or "",
                           "all_day": False})
        elif s.get("date") and e.get("date") and \
                item.get("transparency") != "transparent":
            events.append({"start": _dt(s["date"]), "end": _dt(e["date"]),
                           "summary": item.get("summary") or "",
                           "all_day": True})
    return events


def beths_events(first_date, last_date):
    """Beth's calendar commitments touching the two dates (inclusive)."""
    from gcal_sync import gcal_api_call, get_calendar_service
    service = get_calendar_service()
    start = _dt(first_date).replace(tzinfo=PACIFIC)
    end = _dt(last_date).replace(tzinfo=PACIFIC) + timedelta(days=1)
    items, page_token = [], None
    while True:
        resp = gcal_api_call(
            service.events().list,
            calendarId=os.environ.get("GOOGLE_CALENDAR_ID", "primary"),
            timeMin=start.isoformat(), timeMax=end.isoformat(),
            maxResults=2500, singleEvents=True, pageToken=page_token)
        items.extend(resp.get("items", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            return calendar_events(items)


# =========================================================================
# Booking (Playwright)
# =========================================================================

_UI_WORDS = None
# Public schedule words (class and instructor names) allowed in the log
_SCHEDULE_WORDS = set()


def _ui_words():
    global _UI_WORDS
    if _UI_WORDS is None:
        try:
            _UI_WORDS = {w.strip() for w in Path(
                __file__).with_name("autobook_ui_words.txt").read_text()
                .splitlines() if w.strip() and not w.startswith("#")}
        except Exception:
            _UI_WORDS = set()
    return _UI_WORDS | _SCHEDULE_WORDS


def _mask(text):
    """Make text from Mindbody's signed-in pages safe for the public
    log: only words from Mindbody's own UI vocabulary, numbers and
    prices survive; anything else (Beth's name, say) becomes "•"."""
    text = text or ""
    password = os.environ.get("MINDBODY_PASSWORD")
    if password:
        text = text.replace(password, "<password>")
    text = re.sub(r"\S+@\S+", "<email>", text)
    words = _ui_words()

    def _word(m):
        w = m.group(0)
        return w if w.lower() in words else "•"
    text = re.sub(r"[A-Za-z][A-Za-z']*", _word, text)
    return re.sub(r"\s+", " ", text).strip()


def _scrub(text):
    """Remove the password from text bound for Connor's email."""
    password = os.environ.get("MINDBODY_PASSWORD")
    return (text or "").replace(password, "<password>") if password \
        else (text or "")


def _quiet(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def cart_verdict(text, buttons):
    """What to do on the booking cart, from its visible text and button
    labels. Returns (action, detail):

      ("booked", "")      Mindbody's "You're booked!"
      ("already", "")     she's already in the class
      ("maybe", why)      Mindbody says it can't be booked, possibly
                          because she already holds it
      ("stop", why)       something the bot must not go past
      ("retry", why)      a passing Mindbody error; nothing was booked
      ("confirm", label)  safe to press this button (free booking)
      ("wait", "")        still loading (or the page couldn't be read)
    """
    if not (text or "").strip():
        return "wait", ""  # never decide from a page we couldn't read
    if SUCCESS_RE.search(text):
        return "booked", ""
    if ALREADY_RE.search(text):
        return "already", ""
    if GONE_RE.search(text) or ALREADY_WAITLISTED_RE.search(text):
        return "maybe", "Mindbody says it can no longer be booked"
    if WAITLISTED_RE.search(text):
        return "stop", "the cart put her on a waitlist"
    everything = text + "\n" + "\n".join(buttons)
    for pattern, why, kind in CART_STOP_PATTERNS:
        if re.search(pattern, everything, re.I):
            return kind, why
    priced = POLICY_SENTENCE_RE.sub(" ", text)
    for label in buttons:
        priced += " " + label
    amounts = [float(a.replace(",", "")) for a in MONEY_RE.findall(priced)]
    if any(a > 0 for a in amounts):
        return "stop", "it shows a price ({})".format(", ".join(
            "${:.2f}".format(a) for a in amounts if a > 0))
    for label in buttons:
        if CONFIRM_BUTTON_RE.search(label):
            if any(FORBIDDEN_BUTTON_RE.search(b) for b in buttons) and \
                    not re.search(r"confirm booking", label, re.I):
                return "stop", ("it offers a purchase or waitlist button "
                                "and no plain 'Confirm booking'")
            return "confirm", label
    if any(FORBIDDEN_BUTTON_RE.search(b) for b in buttons):
        return "stop", "it only offers a purchase or waitlist button"
    return "wait", ""


class Booker:
    """One browser session; books classes one at a time.

    Mindbody's flow (from its widget code): Sign Up on a schedule row
    either shows "already booked" / "can no longer be booked" on the
    schedule, or opens a cart page. Signed out, it first opens a sign-in
    popup and, once signed in, carries on to the cart by itself. Only
    the cart's "Confirm booking" button books anything.
    """

    def __init__(self, dry_run=False, min_lead_minutes=0):
        self.dry_run = dry_run
        self.min_lead = timedelta(minutes=min_lead_minutes)
        self.shots = []
        self.signed_in = False
        self.confirm_pressed = False
        self._popups = []
        self._pw = self._browser = self.ctx = self.page = None
        self._widget_urls = {}
        self._book_label = "Sign Up"

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch()
            self.ctx = self._browser.new_context(
                viewport={"width": 1200, "height": 1000},
                user_agent=tice_schedule.UA,
                timezone_id="America/Los_Angeles", locale="en-US")
            self.ctx.set_default_timeout(30000)
            self.page = self.ctx.new_page()
            self.ctx.on("page", lambda p: self._popups.append(p)
                        if p is not self.page else None)
        except BaseException:
            # Otherwise Playwright stays half-started and every later
            # session in this process fails
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc):
        _quiet(lambda: self._browser.close())
        _quiet(lambda: self._pw.stop())
        self._browser = self._pw = None

    # --- helpers ---------------------------------------------------------

    def snap(self, page, name):
        """Screenshot + text for Connor's email; a masked one-liner for
        the public log."""
        try:
            DEBUG_DIR.mkdir(exist_ok=True)
            path = DEBUG_DIR / "{:02d}-{}.png".format(len(self.shots) + 1,
                                                      name)
            page.screenshot(path=str(path), full_page=True)
            text = page.locator("body").inner_text(timeout=5000)
            self.shots.append((path, page.url, _scrub(text[:3000])))
        except Exception as e:
            log.warning("    (screenshot failed: %s)", type(e).__name__)
        log.info("    [%s] %s", name, self.describe(page))

    @staticmethod
    def describe(page):
        """One public-safe line: URL path (ids dropped), headings and
        buttons, masked to Mindbody's own vocabulary."""
        try:
            path = re.sub(r"[?#].*", "", page.url)
            path = re.sub(r"/[0-9a-f-]{16,}|/[A-Za-z0-9_-]*\d[A-Za-z0-9_-]{8,}",
                          "/…", path)
            heads = [h.strip() for h in page.locator(
                "h1, h2, h3, h4, h5, h6, [role=heading]").all_inner_texts()
                if h.strip()][:6]
            buttons = [b.strip() for b in page.locator("button:visible")
                       .all_inner_texts() if b.strip()][:10]
            return "{} | headings: {} | buttons: {}".format(
                path, _mask(" / ".join(heads))[:300],
                _mask(" / ".join(buttons))[:300])
        except Exception as e:
            return "(couldn't describe screen: {})".format(type(e).__name__)

    def widget_url(self, source):
        if source not in self._widget_urls:
            self._widget_urls[source] = tice_schedule.WIDGET_URL.format(
                tice_schedule.resolve_widget_id(source))
        return self._widget_urls[source]

    def open_class_row(self, cls):
        """Load the schedule, select the class's day, return its row."""
        page = self.page
        page.goto(self.widget_url(cls["source"]),
                  wait_until="domcontentloaded")
        # The gym can rename its book button ("Sign Up" today)
        m = re.search(r'\\?"bookButtonText\\?":\\?"([^"\\]{1,40})',
                      _quiet(page.content, ""))
        if m:
            self._book_label = m.group(1).strip()
        page.locator("div[role=button]").first.wait_for(timeout=30000)
        # Signed in, the widget restores the session right after loading;
        # a click before that opens the sign-in popup again
        page.wait_for_timeout(4000 if self.signed_in else 1500)
        day = _dt(cls["start_iso"])
        label = re.compile(r"^\s*(Today|{})\s*{}\s*$".format(
            day.strftime("%a"), day.day))
        for _ in range(6):
            btn = page.locator("div[role=button]").filter(has_text=label)
            if btn.count():
                btn.first.click()
                break
            nxt = page.get_by_role("button", name="Next")
            if not nxt.count() or not nxt.first.is_enabled():
                raise BookingError("{} isn't in the widget's date strip"
                                   .format(cls["date"]))
            nxt.first.click()
            page.wait_for_timeout(1500)
        else:
            raise BookingError("Couldn't find {} in the widget".format(
                cls["date"]))
        # The list is headed by the selected day ("Tuesday, Oct 20"):
        # make sure it's this class's day before trusting any row.
        heading = "{}, {} {}".format(day.strftime("%A"), day.strftime("%b"),
                                     day.day)
        try:
            page.get_by_text(heading, exact=True).first.wait_for(
                timeout=15000)
        except Exception:
            raise BookingError("Widget didn't switch to {}".format(heading))
        return self._class_row(cls)

    def _class_row(self, cls):
        page = self.page
        words = lambda s: r"^\s*{}\s*$".format(
            r"\s+".join(re.escape(w) for w in s.split()))
        rows = (page.locator("div.MuiGrid-container")
                .filter(has=page.locator("h6", has_text=re.compile(
                    words(cls["time"]), re.I)))
                .filter(has=page.locator("h6", has_text=re.compile(
                    words(cls["name"]), re.I)))
                # Rows can nest; the innermost holds one class
                .filter(has_not=page.locator("div.MuiGrid-container")
                        .filter(has=page.locator("h6"))))
        for _ in range(20):  # the list renders a moment after the click
            if rows.count():
                break
            page.wait_for_timeout(500)
        if rows.count() > 1 and cls.get("instructor"):
            rows = rows.filter(has_text=re.compile(
                r"\s+".join(map(re.escape, cls["instructor"].split())),
                re.I))
        if rows.count() != 1:
            raise BookingError("Expected 1 row for {} {} {}, found {}".format(
                cls["date"], cls["time"], cls["name"], rows.count()))
        return rows.first

    @staticmethod
    def _row_label(row):
        buttons = row.locator("button")
        if not buttons.count():
            return ""
        return buttons.last.inner_text(timeout=10000).strip()

    def _row_state(self, label):
        """"book", "unavailable", or raise for a label we don't know."""
        norm = re.sub(r"\s+", " ", label).strip().lower()
        if norm == self._book_label.lower():
            return "book"
        if norm in UNBOOKABLE_LABELS:
            return "unavailable"
        raise BookingError("The schedule row's button says '{}', not '{}' or "
                           "a known full/closed label: the widget may have "
                           "changed".format(_mask(label)[:40],
                                            self._book_label))

    def _in_cart(self):
        return "/cart" in _quiet(lambda: self.page.url, "")

    def _body(self, page=None):
        return _quiet(lambda: (page or self.page).locator("body").inner_text(
            timeout=3000), "")

    # --- sign in ---------------------------------------------------------

    def _sign_in(self, popup):
        """Sign in through the popup. Any surprise becomes a SignInError
        (pauses sign-in) so a changed page can't burn every due class."""
        try:
            self._sign_in_steps(popup)
        except SignInError:
            raise
        except Exception as e:
            if not _quiet(popup.is_closed, True):
                self.snap(popup, "sign-in-error")
            raise SignInError("Mindbody's sign-in page didn't behave as "
                              "expected ({})".format(type(e).__name__),
                              "stuck")

    def _closed_by_itself(self, popup, seconds):
        """Wait for the popup to close (Mindbody's sign-in cookie can
        finish the sign-in with no typing)."""
        for _ in range(seconds):
            if _quiet(popup.is_closed, True):
                return True
            try:
                popup.wait_for_event("close", timeout=1000)
                return True
            except Exception:
                pass
        return _quiet(popup.is_closed, True)

    def _sign_in_steps(self, popup):
        email = os.environ.get("MINDBODY_EMAIL", "")
        password = os.environ.get("MINDBODY_PASSWORD", "")
        if not email or not password:
            raise SignInError("MINDBODY_EMAIL / MINDBODY_PASSWORD not set",
                              "rejected")
        _quiet(lambda: popup.wait_for_load_state("domcontentloaded"))
        email_box = popup.locator(EMAIL_INPUT).first
        # Either a sign-in form appears, or the popup finishes by itself
        for _ in range(30):
            if _quiet(popup.is_closed, True):
                self.signed_in = True
                return
            if _quiet(email_box.is_visible, False):
                break
            if _quiet(lambda: popup.url, "").startswith(
                    "https://go.mindbodyonline.com/book/api/"):
                if self._closed_by_itself(popup, 15):
                    self.signed_in = True
                    return
            popup.wait_for_timeout(1000)
        host = re.sub(r"^https://([^/]+).*", r"\1", _quiet(lambda: popup.url,
                                                           ""))
        if host not in ("signin.mindbodyonline.com", "go.mindbodyonline.com"):
            self.snap(popup, "unknown-sign-in-page")
            raise SignInError("Sign Up opened an unexpected page ({})".format(
                host), "stuck")
        # The visible submit button: an earlier step's may still be in
        # the page, hidden
        submit = lambda: popup.locator(
            "button[type=submit]:visible, "
            "button.MuiLoadingButton-root:visible").first
        try:
            email_box.fill(email, timeout=10000)
        except Exception:
            self.snap(popup, "no-email-field")
            raise SignInError("Mindbody's sign-in page has no email field",
                              "stuck")
        submit().click()

        pw = popup.locator("input[type=password]")
        try:
            pw.first.wait_for(timeout=30000)
        except Exception:
            if _quiet(popup.is_closed, True):
                self.signed_in = True  # remembered sign-in
                return
            text = self._body(popup)
            # Code-first sign-in: take the password route if offered
            alt = popup.get_by_text(re.compile(r"enter my password instead",
                                               re.I))
            if _quiet(alt.count, 0):
                alt.first.click()
                _quiet(lambda: pw.first.wait_for(timeout=15000))
            if not _quiet(pw.count, 0):
                self.snap(popup, "no-password-field")
                if LOCKED_RE.search(text):
                    raise SignInError("Mindbody says too many attempts / "
                                      "locked", "locked")
                if OTP_RE.search(text):
                    raise SignInError("Mindbody asked for a code it emailed "
                                      "to Beth", "otp")
                raise SignInError("Mindbody didn't ask for a password after "
                                  "the email", "stuck")
        try:
            pw.first.fill(password, timeout=10000)
        except Exception:
            self.snap(popup, "password-field-stuck")
            raise SignInError("Mindbody's password field wouldn't take "
                              "input", "stuck")
        submit().click()

        # Success: Mindbody posts back to go.mindbodyonline.com and the
        # popup closes itself (give it up to 15 s there before closing it)
        on_go = 0
        for _ in range(45):
            if self._closed_by_itself(popup, 1):
                self.signed_in = True
                return
            if _quiet(lambda: popup.url, "").startswith(
                    "https://go.mindbodyonline.com"):
                on_go += 1
                if on_go >= 15:
                    _quiet(popup.close)
                    self.signed_in = True
                    return
                continue
            text = self._body(popup)
            if REJECTED_RE.search(text):
                self.snap(popup, "sign-in-rejected")
                raise SignInError("Mindbody says the password is wrong",
                                  "rejected")
            if LOCKED_RE.search(text):
                self.snap(popup, "sign-in-locked")
                raise SignInError("Mindbody says too many attempts / locked",
                                  "locked")
            if OTP_RE.search(text) and not _quiet(lambda: popup.locator(
                    "input[type=password]:visible").count(), 0):
                self.snap(popup, "verification-code")
                raise SignInError("Mindbody asked for a code it emailed to "
                                  "Beth", "otp")
        self.snap(popup, "sign-in-stuck")
        raise SignInError("Sign-in didn't finish within 45s", "stuck")

    # --- booking ---------------------------------------------------------

    def _click_sign_up(self, row, wait_s=20):
        """Click the row's book button (matched by its label, so a
        re-render can't redirect the click) and see what Mindbody does.
        Returns "cart", "already", "maybe" or "nothing"."""
        btn = row.get_by_role("button", name=re.compile(
            r"^\s*{}\s*$".format(re.escape(self._book_label)), re.I))
        if btn.count() != 1:
            raise BookingError("Row has {} '{}' buttons".format(
                btn.count(), self._book_label))
        self._popups.clear()
        btn.click()
        deadline = time.monotonic() + wait_s
        sign_ins = 0
        while time.monotonic() < deadline:
            if self._popups:
                popup = self._popups.pop(0)
                sign_ins += 1
                if sign_ins > 2:
                    _quiet(popup.close)
                    raise BookingError("Mindbody kept asking to sign in")
                self._sign_in(popup)
                log.info("    Signed in")
                # The widget carries on to the cart by itself
                deadline = time.monotonic() + 25
                continue
            if self._in_cart():
                return "cart"
            body = self._body()
            if ALREADY_RE.search(body):
                return "already"
            if GONE_RE.search(body) or ALREADY_WAITLISTED_RE.search(body):
                return "maybe"
            if CART_OPEN_ERROR_RE.search(body):
                raise BookingError("Mindbody couldn't open the cart")
            self.page.wait_for_timeout(300)
        return "nothing"

    def book(self, cls):
        """Book one class. Returns "booked", "already" (she has it),
        "maybe" (Mindbody refused in a way that may mean she has it),
        "unavailable" (filled up) or "dry-run". Raises BookingError."""
        self.confirm_pressed = False
        self._class_start = _dt(cls["start_iso"])
        _SCHEDULE_WORDS.update(w.lower() for w in re.findall(
            r"[A-Za-z][A-Za-z']*", "{} {}".format(
                cls.get("name", ""), cls.get("instructor", ""))))
        if self._too_soon():
            return "unavailable"
        row = self.open_class_row(cls)
        label = self._row_label(row)
        log.info("    Row button: %s", label or "(none)")
        if self._row_state(label) == "unavailable":
            return "unavailable"
        was_signed_in = self.signed_in
        outcome = self._click_sign_up(row)
        if outcome == "nothing" and not was_signed_in and self.signed_in:
            # Signed in, but the widget didn't carry on: click again
            row = self.open_class_row(cls)
            if self._row_state(self._row_label(row)) == "unavailable":
                return "unavailable"
            outcome = self._click_sign_up(row)
        log.info("    After Sign Up: %s", outcome)
        if outcome in ("already", "maybe"):
            return outcome
        if outcome != "cart":
            self.snap(self.page, "no-cart")
            raise BookingError("Sign Up didn't lead to a booking cart")
        return self._checkout()

    def _too_soon(self):
        if self.min_lead and self._class_start - _now() < self.min_lead:
            log.info("    Now too close to class time; skipping")
            return True
        return False

    def _read_cart(self):
        """(verdict, detail, text, buttons) for the cart as it is now;
        ("wait", ...) when it can't be read reliably."""
        page = self.page
        card = _quiet(lambda: page.locator(CARD_SELECTOR).count(), None)
        if card is None:
            return "wait", "", "", []
        if card:
            return "stop", "it asks for a payment card", "", []
        text = self._body()
        buttons = _quiet(lambda: [b.strip() for b in page.locator(
            "button:visible").all_inner_texts() if b.strip()], None)
        if buttons is None:
            return "wait", "", text, []
        action, detail = cart_verdict(text, buttons)
        return action, detail, text, buttons

    def _checkout(self):
        """On the cart: press "Confirm booking" if (and only if) it's a
        free booking, then wait for "You're booked!"."""
        page = self.page
        last = None
        confirms = 0
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            page.wait_for_timeout(1500)
            action, detail, text, buttons = self._read_cart()
            if (action, detail) != last:
                self.snap(page, "cart-{}".format(action))
                last = (action, detail)
            if action == "booked":
                return "booked"
            if action in ("already", "maybe"):
                return action
            if action in ("stop", "retry"):
                raise BookingError(
                    "Stopped: {}".format(detail),
                    "blocked" if action == "stop" else "error",
                    detail=_scrub(text[:1500]))
            if action != "confirm" or self.confirm_pressed:
                confirms = 0
                continue
            # Press only on a steady, enabled button: the same "confirm"
            # verdict twice in a row, re-checked right before the click
            confirms += 1
            btn = page.get_by_role("button", name=re.compile(
                r"^\s*{}\s*$".format(re.escape(detail)), re.I)).first
            if confirms < 2 or not _quiet(btn.is_enabled, False):
                continue
            if self.dry_run:
                log.info("    DRY RUN: would press '%s'; stopping", detail)
                return "dry-run"
            again = self._read_cart()
            if again[:2] != (action, detail) or self._too_soon():
                confirms = 0
                continue
            log.info("    Pressing '%s'", detail)
            self.confirm_pressed = True
            try:
                btn.click(timeout=3000)
            except Exception:
                self.confirm_pressed = False  # the click never landed
                confirms = 0
                continue
            deadline = time.monotonic() + 90  # time for "You're booked!"
        self.snap(page, "cart-timeout")
        raise BookingError("No 'You're booked!' after pressing Confirm "
                           "booking" if self.confirm_pressed else
                           "The cart never offered 'Confirm booking'")


# =========================================================================
# Alerts (throttled: one email per problem per day)
# =========================================================================

def alert(state, key, subject, body, shots=(), every_hours=ALERT_EVERY_HOURS):
    """Email Connor unless this problem was emailed recently. Returns
    False only when an email was due and couldn't be sent."""
    last = state["alerts"].get(key)
    if last and _dt(last) > _now() - timedelta(hours=every_hours):
        log.info("  (already emailed Connor about this recently)")
        return True
    if send_email(subject, body, shots):
        state["alerts"][key] = _now().isoformat()
        return True
    return False


def send_email(subject, body, shots=()):
    """Email Connor, attaching screenshots (kept out of the public
    Actions log because they show Beth's account)."""
    import smtplib
    from email.mime.image import MIMEImage
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    import notify
    if not notify.SENDER_PASSWORD:
        log.error("CALENDAR_EMAIL_PASSWORD not set; can't email Connor")
        return False
    msg = MIMEMultipart()
    msg["Subject"] = "[Beth Calendar] {}".format(subject)
    msg["From"] = "Beth Calendar Bot <{}>".format(notify.SENDER_EMAIL)
    msg["To"] = notify.ALERT_RECIPIENT
    parts = [body]
    if notify.GITHUB_RUN_URL:
        parts.append("\nRun: {}".format(notify.GITHUB_RUN_URL))
    for path, url, text in shots:
        parts.append("\n--- {} ({})\n{}".format(path.name, url, text))
    msg.attach(MIMEText(_scrub("\n".join(parts)), "plain"))
    for path, _, _ in list(shots)[-12:]:
        try:
            img = MIMEImage(path.read_bytes())
            img.add_header("Content-Disposition", "attachment",
                           filename=path.name)
            msg.attach(img)
        except Exception:
            pass
    for attempt in range(3):
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
                s.login(notify.SENDER_EMAIL, notify.SENDER_PASSWORD)
                s.send_message(msg)
            log.info("  Emailed Connor: %s", subject)
            return True
        except Exception as e:
            log.warning("  Email attempt %d failed: %s", attempt + 1,
                        type(e).__name__)
            time.sleep(5 * (attempt + 1))
    log.error("Couldn't email Connor")
    return False


# =========================================================================
# Sign-in pause (protects Beth's account from repeated bad logins)
# =========================================================================

def credentials_fingerprint(reset=0):
    """Changes whenever MINDBODY_EMAIL/PASSWORD (or config.yaml's
    autobook.signin_reset) change, without revealing them: an HMAC keyed
    by the (secret) service-account key, so the public state file can't
    be used to guess the password."""
    key = os.environ.get("GOOGLE_SERVICE_ACCOUNT_KEY", "")
    msg = "{}\0{}\0{}".format(os.environ.get("MINDBODY_EMAIL", ""),
                              os.environ.get("MINDBODY_PASSWORD", ""), reset)
    return hmac.new(key.encode(), msg.encode(), hashlib.sha256) \
        .hexdigest()[:16]


def signin_paused(state, now, reset=0):
    """Reason sign-in is paused, or None."""
    p = state.get("signin_pause") or {}
    if not p:
        return None
    if p.get("fingerprint") != credentials_fingerprint(reset):
        return None  # credentials (or the reset knob) changed: try again
    if p.get("until") and _dt(p["until"]) <= now:
        return None
    return p.get("reason", "sign-in problem")


def pause_signin(state, err, now, reset=0):
    """Pause sign-in after a failure. A wrong password stays paused until
    the secret changes; other problems pause 6-24 h, three times longer
    on each repeat, and stay paused after SIGNIN_MAX_REPEATS."""
    prev = state.get("signin_pause") or {}
    fp = credentials_fingerprint(reset)
    count = prev.get("count", 1) + 1 if (
        prev.get("kind") == err.kind and prev.get("fingerprint") == fp) else 1
    hours = SIGNIN_PAUSE_HOURS.get(err.kind)
    until = ""
    if hours and count < SIGNIN_MAX_REPEATS:
        until = (now + timedelta(hours=hours * 3 ** (count - 1))).isoformat()
    state["signin_pause"] = {
        "fingerprint": fp, "reason": str(err), "kind": err.kind,
        "count": count, "since": now.isoformat(), "until": until}
    # One email per pause: the "paused" reminder starts a day later
    state["alerts"]["signin-paused"] = now.isoformat()


RESET_HELP = (
    "\nTo make it try again without changing the password, change "
    "`signin_reset` under `autobook` in config.yaml to any new number.\n")
SIGNIN_HELP = {
    "rejected": (
        "Mindbody rejected Beth's password, so auto-booking is paused "
        "until the GitHub secret changes (retrying could lock her "
        "account). Fix it with:\n\n"
        "  gh secret set MINDBODY_PASSWORD --repo "
        "connordy8/tice-creek-calendar\n" + RESET_HELP),
    "otp": (
        "Mindbody asked for a verification code it emailed to Beth. The "
        "bot can't get past that, so sign-in is paused (a day the first "
        "time, longer if it keeps happening, then until reset). Each "
        "attempt emails Beth a code she didn't ask for.\n" + RESET_HELP),
    "locked": (
        "Mindbody said there were too many attempts or the account is "
        "locked. Sign-in is paused for a day (longer if it repeats). Beth "
        "may want to check she can sign in to the Mindbody app.\n"
        + RESET_HELP),
    "stuck": (
        "Mindbody's sign-in page didn't behave as expected (it may have "
        "changed). Sign-in is paused for 6 hours (longer if it repeats); "
        "screenshots attached.\n" + RESET_HELP),
}


# =========================================================================
# Main
# =========================================================================

def _describe(cls):
    return "{} {} {} {} w/ {} ({}/{} registered)".format(
        cls["day"][:3], cls["date"], cls["time"], cls["name"],
        cls.get("instructor") or "?", cls["registered"], cls["capacity"])


def _record(state, cls, ctype, result, error=""):
    """Note an attempt. The state file is public: `error` must be our
    own short message, never page text."""
    old = state["bookings"].get(cls["mindbody_id"]) or {}
    state["bookings"][cls["mindbody_id"]] = {
        "date": cls["date"], "type": ctype, "name": cls["name"],
        "time": cls["time"], "start": cls["start_iso"],
        "end": cls["end_iso"], "result": result, "at": _now().isoformat(),
        "attempts": old.get("attempts", 0) + 1,
        **({"error": _mask(error)[:160]} if error else {})}


def find_targets(state, rules, now, only_id=None):
    classes, errors = fetch_classes()
    ok = True
    for label, err in errors.items():
        log.error("Schedule %s failed: %s", label, err)
        ok &= alert(state, "schedule:" + label,
                    "Auto-book: can't read the {} schedule".format(label),
                    "The Tice Creek {} schedule couldn't be read, so no "
                    "{} classes can be auto-booked until it's fixed.\n\n"
                    "Error: {}".format(label, label, err))
    mine = [c for c in classes if class_type(c, rules)]
    log.info("Beth's auto-book classes on the schedule:")
    for c in sorted(mine, key=lambda c: c["start_iso"]):
        log.info("  %s  %s", _describe(c), due_reason(c, rules, now) or "")
    candidates = [c for c in mine if due_reason(c, rules, now)
                  or c["mindbody_id"] == only_id]
    if not candidates:
        return [], ok
    dates = sorted(c["date"] for c in candidates)
    events = beths_events(dates[0], dates[-1])
    return plan(classes, rules, now, state, events, only_id=only_id), ok


def _failure_email(cls, e, shots, committed):
    detail = getattr(e, "detail", "")
    if committed:
        what = ("It pressed 'Confirm booking' but didn't see Mindbody's "
                "'You're booked!'. It won't retry this class, and the class "
                "counts toward that day's limits in case it went through. "
                "Beth may want to check the Mindbody app.")
    elif getattr(e, "kind", "") == "blocked":
        what = ("Mindbody wanted something the bot won't do on its own, so "
                "nothing was booked and it won't retry this class. Beth can "
                "sign up herself in the Mindbody app.")
    else:
        what = ("Nothing was booked (it never reached 'Confirm booking'). It "
                "will retry a couple of times.")
    return ("Tried to sign Beth up for {}.\n\nWhat happened: {}\n\n{}{}"
            .format(_describe(cls), _scrub(str(e)), what,
                    "\n\nScreen text:\n" + _scrub(detail) if detail else ""))


def book_all(state, targets, dry_run, rules):
    """Book each target in one browser session. Returns False if an
    alert couldn't be delivered."""
    ok = True
    reset = rules.get("signin_reset", 0)
    paused = signin_paused(state, _now(), reset)
    if paused:
        log.warning("Sign-in is paused (%s); not booking", paused)
        return alert(state, "signin-paused", "Auto-book paused",
                     "Classes are due but sign-in is paused: {}\n\n{}".format(
                         paused, SIGNIN_HELP.get(
                             state["signin_pause"].get("kind"), "")))
    with Booker(dry_run=dry_run,
                min_lead_minutes=rules["min_lead_minutes"]) as booker:
        for cls, ctype, reason in targets:
            log.info("Booking %s [%s]%s", _describe(cls), reason,
                     " (dry run)" if dry_run else "")
            booker.shots = []
            try:
                result = booker.book(cls)
            except SignInError as e:
                log.error("  Sign-in failed: %s", e)
                if not dry_run:
                    pause_signin(state, e, _now(), reset)
                ok &= alert(state, "signin:" + e.kind,
                            "Auto-book can't sign in to Mindbody",
                            "{}\n\n{}".format(e, SIGNIN_HELP[e.kind]),
                            booker.shots,
                            every_hours=0 if e.kind == "rejected"
                            else ALERT_EVERY_HOURS)
                save_state(state)
                return ok
            except Exception as e:
                committed = booker.confirm_pressed
                kind = getattr(e, "kind", "error")
                msg = str(e) if isinstance(e, BookingError) else \
                    "{} ({})".format(type(e).__name__,
                                     str(e).split("\n")[0][:120])
                result = ("failed" if committed else
                          "blocked" if kind == "blocked" else "error")
                log.error("  Booking %s: %s", result, _mask(msg)[:300])
                if not dry_run:
                    _record(state, cls, ctype, result,
                            msg if isinstance(e, BookingError)
                            else type(e).__name__)
                subject = "Auto-book {}: {} {} {}".format(
                    "FAILED" if committed else "couldn't book",
                    cls["name"], cls["date"], cls["time"])
                key = ("book:" + cls["mindbody_id"] if committed
                       else "blocked:" + msg if kind == "blocked"
                       else "error")
                ok &= alert(state, key, subject,
                            _failure_email(cls, e, booker.shots, committed),
                            booker.shots,
                            every_hours=0 if committed else ALERT_EVERY_HOURS)
                save_state(state)
                continue
            log.info("  Result: %s", result)
            if result in ("booked", "already"):
                _record(state, cls, ctype, "booked" if result == "booked"
                        else "booked-by-beth")
            elif result == "maybe":
                # Counts toward the day (she may hold it); never retried
                _record(state, cls, ctype, "maybe-booked")
                ok &= alert(
                    state, "maybe:" + cls["mindbody_id"],
                    "Auto-book: Mindbody wouldn't book {} {}".format(
                        cls["name"], cls["date"]),
                    "Mindbody said {} 'can no longer be booked' (it says "
                    "that when Beth may already have it, or it just filled "
                    "up). The bot is treating it as possibly booked: it "
                    "won't book another {} that day. Beth may want to check "
                    "the Mindbody app.".format(_describe(cls), ctype),
                    booker.shots, every_hours=0)
            elif result == "unavailable" and not dry_run:
                _record(state, cls, ctype, "unavailable")
            elif result == "dry-run":
                ok &= send_email(
                    "Auto-book dry run: {} {}".format(cls["name"],
                                                      cls["date"]),
                    "Dry run reached Mindbody's 'Confirm booking' step for "
                    "{} and stopped without pressing it. Nothing was "
                    "booked. Screenshots attached.".format(_describe(cls)),
                    booker.shots)
            save_state(state)
    return ok


def run_once(args, state, rules):
    """Check once and book what's due. Returns False if an alert
    couldn't be delivered."""
    now = _now()
    targets, ok = find_targets(state, rules, now, only_id=args.class_id)
    if args.class_id:
        targets = [t for t in targets if t[0]["mindbody_id"] == args.class_id]
        if not targets:
            log.error("Class %s isn't one the rules would book (not one of "
                      "her classes, before 11 AM, too soon, full, or it "
                      "clashes/exceeds the day's limits)", args.class_id)
            return ok
    if args.dry_run:
        targets = targets[:1]
    if not targets:
        log.info("Nothing to book.")
        return ok
    for cls, ctype, reason in targets:
        log.info("Due: %s [%s]", _describe(cls), reason)
    if args.check:
        out = os.environ.get("GITHUB_OUTPUT")
        if out:
            with open(out, "a") as f:
                f.write("targets={}\n".format(len(targets)))
        return ok
    return book_all(state, targets, args.dry_run, rules) and ok


def _at(now, hm):
    return now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)


def watching_now(now):
    """True from 3:00 to 8:40 AM Pacific: stay up for the 8 AM opening."""
    return _at(now, WATCH_FROM) <= now < _at(now, WATCH_UNTIL)


def push_state():
    """Commit and push autobook_state.json right away (CI only), so a
    booking made early in a long watch isn't lost if the job dies.
    Returns True when pushed (or not in CI)."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return True
    import subprocess
    try:
        return subprocess.run(["bash", "autobook_push_state.sh"],
                              check=False, timeout=240).returncode == 0
    except Exception as e:
        log.warning("Couldn't push state mid-run: %s", type(e).__name__)
        return False


def live_rules(current):
    """config.yaml as it is on GitHub now (CI only), so turning
    auto-booking off stops a morning watch that's already running.
    Falls back to `current` if it can't be read."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return current
    import subprocess
    try:
        subprocess.run(["git", "fetch", "-q", "origin", "main"], check=True,
                       timeout=60)
        text = subprocess.run(["git", "show", "origin/main:config.yaml"],
                              check=True, timeout=30, capture_output=True,
                              text=True).stdout
        rules = load_rules(text=text)
    except Exception as e:
        log.warning("Couldn't re-read config.yaml from GitHub (%s); keeping "
                    "the rules I have", type(e).__name__)
        return current
    if os.environ.get("GH_TOKEN") and os.environ.get("GITHUB_REPOSITORY"):
        try:
            state = subprocess.run(
                ["gh", "api", "repos/{}/actions/workflows/autobook.yml".format(
                    os.environ["GITHUB_REPOSITORY"]), "--jq", ".state"],
                check=True, timeout=30, capture_output=True,
                text=True).stdout.strip()
            if state == "disabled_manually":
                rules["enabled"] = False
        except Exception:
            pass
    return rules


def check_watch_health(state, now):
    """Alert (weekly at most) if GitHub hasn't let an 8 AM watch run for
    a few days: popular classes are only caught then."""
    last = state.get("last_watch", "")
    if not last:
        state["last_watch"] = now.date().isoformat()  # start counting
        return True
    if now < _at(now, WATCH_UNTIL):
        return True
    if last >= (now.date() - timedelta(days=WATCH_MISSED_DAYS)).isoformat():
        return True
    return alert(state, "watch-missed", "Auto-book: no 8 AM watch lately",
                 "GitHub hasn't started an early-morning auto-book run since "
                 "{}. Day-before bookings still happen, but classes that "
                 "fill up at the 8 AM opening (UJAM, Mat Yoga, Posture) "
                 "won't be caught until it does.".format(last),
                 every_hours=24 * 7)


def check_sync_alive(state, now):
    """Email Connor (daily at most) if the calendar sync workflow has
    stopped running. CI only; skipped if someone turned it off."""
    repo = os.environ.get("GITHUB_REPOSITORY")
    if os.environ.get("GITHUB_ACTIONS") != "true" or not repo or \
            not os.environ.get("GH_TOKEN"):
        return True
    import subprocess
    gh = lambda *a: subprocess.run(["gh"] + list(a), check=True, timeout=30,
                                   capture_output=True, text=True).stdout
    try:
        if gh("api", "repos/{}/actions/workflows/sync.yml".format(repo),
              "--jq", ".state").strip() != "active":
            return True
        last = gh("run", "list", "--repo", repo, "--workflow", "sync.yml",
                  "-L", "1", "--json", "createdAt", "--jq",
                  '.[0].createdAt // ""').strip()
    except Exception as e:
        log.warning("Couldn't check the sync workflow (%s)", type(e).__name__)
        return True
    if not last:
        return True
    last = datetime.fromisoformat(last.replace("Z", "+00:00")).astimezone(
        PACIFIC).replace(tzinfo=None)
    if now - last < timedelta(hours=PEER_STALE_HOURS):
        return True
    return alert(state, "stale:sync", "Calendar sync has stopped running",
                 "The 'Sync Tice Creek Schedule' workflow hasn't run since "
                 "{}, so Beth's calendar isn't being updated (classes, "
                 "movies, forwarded appointments). Check the Actions tab.\n"
                 .format(last.strftime("%a %b %-d %-I:%M %p")))


def watch(args, state, rules):
    """Keep checking until 8:40 AM: every 10 min, then every 20 s from
    7:40. Returns False if an alert couldn't be delivered."""
    ok = True
    log.info("Watching until %02d:%02d Pacific", *WATCH_UNTIL)
    state["last_watch"] = _now().date().isoformat()
    unpushed = False
    rules_checked = time.monotonic()
    while watching_now(_now()):
        if time.monotonic() - rules_checked >= 120:
            rules = live_rules(rules)
            rules_checked = time.monotonic()
        if not rules["classes"] or not rules.get("enabled", True):
            log.info("Auto-booking was turned off; stopping the watch")
            break
        before = json.dumps([state["bookings"], state["signin_pause"]],
                            sort_keys=True)
        try:
            ok &= run_once(args, state, rules)
        except Exception as e:
            ok &= _crash_alert(state, e, "check failed")
        save_state(state)
        if json.dumps([state["bookings"], state["signin_pause"]],
                      sort_keys=True) != before or unpushed:
            unpushed = not push_state()
        now = _now()
        pause = WATCH_FAST_SECONDS if now >= _at(now, WATCH_RUSH) \
            else WATCH_SLOW_SECONDS
        time.sleep(max(1, min(pause, (_at(now, WATCH_UNTIL) - now)
                              .total_seconds())))
    return ok


def _crash_alert(state, e, what):
    log.error("%s: %s", what, _mask("{}: {}".format(
        type(e).__name__, str(e).split("\n")[0][:200])))
    log.error(_mask(traceback.format_exc())[-1500:])
    return alert(state, "crash:" + type(e).__name__, "Auto-book: " + what,
                 _scrub("{}: {}: {}\n\nNo classes are being booked while "
                        "this keeps happening.\n\n{}".format(
                            what, type(e).__name__, e,
                            traceback.format_exc())))


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="only report what's due")
    ap.add_argument("--dry-run", action="store_true",
                    help="sign in and walk the flow, never confirm")
    ap.add_argument("--class-id", help="dry run against this class id")
    ap.add_argument("--watch", action="store_true",
                    help="if it's 3:00-8:40 AM Pacific, keep checking "
                         "until 8:40 (otherwise just run once)")
    ap.add_argument("--merge-state", metavar="OURS",
                    help="merge OURS into autobook_state.json and exit")
    args = ap.parse_args()
    if args.class_id:
        args.dry_run = True

    if args.merge_state:
        merged = merge_states(load_state(Path(args.merge_state)),
                              load_state())
        save_state(merged)
        return

    try:
        state = load_state()
    except Exception as e:
        # Only a hand edit can break it (the bot writes atomically).
        # Don't overwrite it, and don't email every run: the red run
        # (GitHub's own email) flags it.
        log.error("autobook_state.json is unreadable (%s); fix or delete it",
                  type(e).__name__)
        sys.exit(1)
    ok = True
    try:
        rules = load_rules()
        if not rules["classes"] or not rules.get("enabled", True):
            log.info("Auto-booking is turned off in config.yaml.")
            return
        if args.watch and watching_now(_now()) and not args.dry_run:
            ok = watch(args, state, rules)
        else:
            ok = run_once(args, state, rules)
            ok &= check_watch_health(state, _now())
            ok &= check_sync_alive(state, _now())
        save_state(state)
    except Exception as e:
        ok = _crash_alert(state, e, "crashed")
        _quiet(lambda: save_state(state))
    # Exit non-zero only when Connor couldn't be told about a problem:
    # GitHub's own failure email is then the backstop. Otherwise a
    # broken Mindbody page would mean a red run every 10 minutes.
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
