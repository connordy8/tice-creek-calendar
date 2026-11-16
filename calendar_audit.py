#!/usr/bin/env python3
"""Read-only audit of Beth's Google Calendar.

Runs the normal sync logic as a dry run (read-only credentials, every
write intercepted) to see what it *would* change, then checks the
calendar against the booking rules. Emails Connor the report and prints it to the (public) Actions
log with the names of Beth's own events blanked out.

  python calendar_audit.py [--days 14] [--no-email]
"""

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from google.oauth2 import service_account
from googleapiclient.discovery import build

import gcal_sync
from gcal_sync import (ALL_PREFIXES, CLASS_EVENT_PREFIX, PACIFIC,
                       _event_start, _sig_words)

log = logging.getLogger("calendar_audit")


class _Write:
    def __init__(self, kind, kwargs, sink):
        self.kind, self.kwargs, self.sink = kind, kwargs, sink

    def execute(self):
        self.sink.append((self.kind, self.kwargs))
        return {}


class _Events:
    def __init__(self, real, sink):
        self.real, self.sink = real, sink

    def list(self, **kw):
        return self.real.list(**kw)

    def insert(self, **kw):
        return _Write("create", kw, self.sink)

    def update(self, **kw):
        return _Write("update", kw, self.sink)

    def delete(self, **kw):
        return _Write("delete", kw, self.sink)


class _Service:
    def __init__(self, real, sink):
        self.real, self.sink = real, sink

    def events(self):
        return _Events(self.real.events(), self.sink)


def readonly_service():
    creds = service_account.Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_KEY"]),
        scopes=["https://www.googleapis.com/auth/calendar.readonly"])
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def fetch_events(svc, days):
    cal = os.environ.get("GOOGLE_CALENDAR_ID", "primary")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    out, tok = [], None
    while True:
        r = svc.events().list(
            calendarId=cal, timeMin=now.isoformat() + "Z",
            timeMax=(now + timedelta(days=days)).isoformat() + "Z",
            singleEvents=True, orderBy="startTime", maxResults=2500,
            pageToken=tok).execute()
        out += r.get("items", [])
        tok = r.get("nextPageToken")
        if not tok:
            return out


def _end(item):
    e = item.get("end", {}).get("dateTime")
    if not e:
        return None
    d = datetime.fromisoformat(e)
    return d.astimezone(PACIFIC).replace(tzinfo=None) if d.tzinfo else d


def _when(item):
    s = _event_start(item)
    return s.strftime("%a %b %-d %-I:%M %p") if s else "(all day)"


def simulate_sync(svc):
    """What would the next sync create/update/delete? (no writes)"""
    import scraper
    from tice_schedule import fetch_all

    config = scraper.load_config()
    classes, coverage, errors = fetch_all()
    filtered = scraper.resolve_conflicts(
        scraper.filter_classes(classes, config))
    manual = scraper.load_manual_events() or []
    filtered = scraper.apply_manual_events(
        filtered, [m for m in manual if m.get("type") in ("cancel", "modify")])
    appts = [m for m in manual if m.get("type") == "add"
             and m.get("date") and m.get("start_time")]
    try:
        movies, concerts = scraper.scrape_entertainment(config)
    except Exception as e:
        log.warning("entertainment scrape failed: %s", e)
        movies = concerts = None

    sink = []
    gcal_sync.get_calendar_service = lambda: _Service(svc, sink)
    problem = None
    try:
        gcal_sync.sync_to_google_calendar(
            filtered, movies, concerts, config,
            class_coverage=coverage, appointments=appts)
    except Exception as e:
        problem = str(e)
    errs = ["Tice Creek {}: {}".format(k, v) for k, v in errors.items()]
    if movies is None:
        errs.append("Rossmoor movies/concerts scrape failed")
    if problem:
        errs.append("sync: " + problem)
    return sink, errs


def audit(days, send):
    import autobook

    svc = readonly_service()
    items = [i for i in fetch_events(svc, days)
             if i.get("status") != "cancelled"]
    now = gcal_sync._now()
    ours = [i for i in items if i.get("id", "").startswith(ALL_PREFIXES)]
    classes = [i for i in ours if i["id"].startswith(CLASS_EVENT_PREFIX)]
    beths = [i for i in items if not i.get("id", "").startswith(ALL_PREFIXES)
             and i.get("start", {}).get("dateTime")]
    state = autobook.load_state()["bookings"]
    rules = autobook.load_rules()

    issues, notes = [], []

    # 1. Would the sync change anything? (calendar drifted from source)
    writes, errs = simulate_sync(svc)
    for e in errs:
        issues.append("Source problem: " + e)
    by_id = {i["id"]: i for i in items}
    for kind, kw in writes:
        body = kw.get("body") or by_id.get(kw.get("eventId"), {})
        (notes if kind == "update" else issues).append(
            "Calendar out of date, next sync will {}: {} {}".format(
            kind, _when(body) if body.get("start") else "",
            body.get("summary", kw.get("eventId", ""))))

    # 2. Bookings vs calendar ✅
    booked = {k: v for k, v in state.items() if v.get("result") == "booked"
              and v.get("start", "") >= now.isoformat()[:16]}
    tagged = [c for c in classes if "✅" in c.get("summary", "")]
    for cid, rec in booked.items():
        start = datetime.fromisoformat(rec["start"])
        hit = [c for c in tagged if _event_start(c)
               and abs((_event_start(c) - start).total_seconds()) <= 3600
               and _sig_words(rec["name"]) & _sig_words(c["summary"])]
        if not hit:
            issues.append("Booked but not shown as booked: {} {} {}".format(
                rec["name"], rec["date"], rec["time"]))
    for c in tagged:
        s = _event_start(c)
        if not any(abs((datetime.fromisoformat(r["start"]) - s)
                       .total_seconds()) <= 3600
                   and _sig_words(r["name"]) & _sig_words(c["summary"])
                   for r in booked.values()):
            issues.append("Shows booked but bot has no booking: {} {}"
                          .format(_when(c), c["summary"]))
    pending = {k: v for k, v in state.items()
               if v.get("result") in ("maybe-booked", "failed", "error",
                                      "blocked", "unavailable")
               and v.get("start", "") >= now.isoformat()[:16]}
    for rec in pending.values():
        notes.append("Booking attempt '{}' ({}x): {} {} {}{}".format(
            rec["result"], rec.get("attempts", 1), rec["name"], rec["date"],
            rec["time"], " - " + rec["error"][:120] if rec.get("error") else ""))

    # 3. Descriptions: just the class + enrolled line
    for c in classes:
        lines = (c.get("description") or "").splitlines()
        ok = (len(lines) >= 2 and lines[1] in (
            "Enrolled", "Not enrolled", "Drop-in, no sign-up needed")
              and all(l.startswith("Note: ") for l in lines[2:]))
        if not ok:
            issues.append("Description not in the simple format: {} {}"
                          .format(_when(c), c.get("summary")))
        enrolled = len(lines) > 1 and lines[1] == "Enrolled"
        if enrolled != ("✅" in c.get("summary", "")):
            issues.append("Title and description disagree: {} {}"
                          .format(_when(c), c.get("summary")))

    # 4. Duplicates: two of our events at the same time, or a class we
    #    list that Beth also has on her own calendar
    seen = defaultdict(list)
    for c in ours:
        seen[(c.get("summary"), c["start"].get("dateTime",
                                               c["start"].get("date")))].append(c)
    for (summ, when), dup in seen.items():
        if len(dup) > 1:
            issues.append("Duplicate event x{}: {} {}".format(
                len(dup), when, summ))
    for c in classes:
        s = _event_start(c)
        for b in beths:
            bs = _event_start(b)
            if bs and abs((bs - s).total_seconds()) <= 20 * 60 and (
                    _sig_words(c["summary"]) & _sig_words(b.get("summary"))):
                issues.append("Listed twice (ours + her own entry): {} {}"
                              .format(_when(c), c["summary"]))

    # 5. Booking rules on what's booked
    per_day = defaultdict(list)
    for rec in booked.values():
        per_day[rec["date"]].append(rec)
        st = datetime.fromisoformat(rec["start"])
        if st.hour < rules.get("earliest_hour", 11):
            issues.append("Booked before {}:00: {} {} {}".format(
                rules.get("earliest_hour", 11), rec["name"], rec["date"],
                rec["time"]))
        days_out = (st.date() - now.date()).days
        if days_out > rules.get("book_days_ahead", 1):
            notes.append("Booked {} days ahead (allowed only when nearly "
                         "full): {} {} {}".format(days_out, rec["name"],
                                                  rec["date"], rec["time"]))
    for day, recs in per_day.items():
        if len(recs) > rules.get("max_per_day", 2):
            issues.append("More than {} classes booked on {}".format(
                rules.get("max_per_day", 2), day))

    # 6. Booked classes overlapping her other commitments
    buf = timedelta(minutes=rules.get("buffer_minutes", 15))
    for c in tagged:
        s, e = _event_start(c), _end(c)
        for b in beths:
            bs, be = _event_start(b), _end(b)
            if not (bs and be) or (_sig_words(c["summary"])
                                   & _sig_words(b.get("summary"))):
                continue
            if bs < e + buf and s < be + buf:
                issues.append("Booked class overlaps another event: {} {} "
                              "vs '{}' {}".format(_when(c), c["summary"],
                                                  b.get("summary"), _when(b)))

    counts = defaultdict(int)
    for i in ours:
        p = next(p for p in ALL_PREFIXES if i["id"].startswith(p))
        counts[p] += 1
    summary = [
        "Beth's calendar audit, next {} days ({})".format(
            days, now.strftime("%a %b %-d %-I:%M %p")),
        "",
        "Events checked: {} total, {} added by the bot ({} class listings, "
        "{} movies/concerts, {} appointments), {} of her own.".format(
            len(items), len(ours), counts[CLASS_EVENT_PREFIX],
            counts["be0ca1"] + counts["be0cb2"], counts["be0cd4"],
            len(items) - len(ours)),
        "Bot bookings upcoming: {}.".format(len(booked)),
        "",
    ]
    summary.append("PROBLEMS ({})".format(len(issues)) if issues
                   else "No problems found.")
    summary += ["  - " + i for i in issues]
    if notes:
        summary += ["", "FYI"] + ["  - " + n for n in notes]
    summary += ["", "Class listings:"]
    for c in sorted(classes, key=lambda c: _event_start(c)):
        lines = (c.get("description") or "").splitlines()
        summary.append("  {}  {}  [{}]".format(
            _when(c), c["summary"], lines[1] if len(lines) > 1 else "?"))
    report = "\n".join(summary)

    # Public log: same report with her own events' names blanked out
    safe = report
    for b in beths:
        if b.get("summary"):
            safe = safe.replace("'{}'".format(b["summary"]), "(one of her events)")
    print(safe)
    print("Events: {}  bot-managed: {}  classes: {}  bookings: {}  "
          "problems: {}  fyi: {}".format(len(items), len(ours), len(classes),
                                          len(booked), len(issues),
                                          len(notes)))
    if send:
        from notify import send_email
        send_email("Beth calendar audit: {}".format(
            "{} problem(s)".format(len(issues)) if issues else "all good"),
            report)
    return 1 if issues else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--no-email", action="store_true")
    a = ap.parse_args()
    sys.exit(audit(a.days, not a.no_email))
