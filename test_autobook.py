"""Tests for autobook.py's booking rules. No network, no browser.

    python test_autobook.py           the logic, against a fixed copy of
                                      Connor's rules (so editing
                                      config.yaml never breaks booking)
    python test_autobook.py --config  also check config.yaml still holds
                                      Connor's rules (flags a change)
"""

import json
import os
import sys
import tempfile
import traceback
from datetime import datetime, timedelta
from pathlib import Path

import autobook as ab

# Connor's rules as of Oct 2026 (config.yaml's `autobook` section)
RULES_YAML = """
include_classes: [zumba, ujam, aquacise, "water aerobics", posture,
                  "mat yoga", foreverfit, "functional fitness",
                  "let's stretch", "pilates mat"]
exclude_classes: [cancelled, "introduction to", pickleball, "tai chi"]
autobook:
  enabled: true
  classes:
    - name: zumba
    - name: ujam
    - name: aquacise
      instructor: bob huff
    - name: posture
    - name: mat yoga
  earliest_hour: 11
  book_days_ahead: 1
  max_spots_left: 2
  max_per_day: 2
  buffer_minutes: 15
  min_lead_minutes: 120
  signin_reset: 0
"""
RULES = ab.load_rules(text=RULES_YAML)
NOW = datetime(2026, 10, 19, 9, 0)        # Mon 9 AM
TOMORROW = "2026-10-20"
LATER = "2026-10-23"                      # Fri, 4 days out


def cls(name="Zumba", date=TOMORROW, time="13:00", minutes=50, cap=25,
        reg=10, bookable=True, instructor="Sabrina Monahan", cid=None,
        club=False, cancelled=False, source="group_fitness"):
    start = datetime.fromisoformat("{}T{}".format(date, time))
    end = start + timedelta(minutes=minutes)
    return {
        "mindbody_id": cid or "{}-{}-{}".format(name, date, time),
        "name": name, "instructor": instructor, "source": source,
        "date": date, "day": start.strftime("%A"),
        "time": start.strftime("%I:%M %p").lstrip("0"),
        "start_iso": start.isoformat(timespec="minutes"),
        "end_iso": end.isoformat(timespec="minutes"),
        "capacity": cap, "registered": reg, "bookable": bookable,
        "cancelled": cancelled, "is_club": club,
    }


def state(**bookings):
    return {"bookings": dict(bookings), "alerts": {}, "signin_pause": {}}


def booked(c, result="booked", ctype=None):
    return {"date": c["date"], "type": ctype or ab.class_type(c, RULES),
            "name": c["name"], "time": c["time"], "start": c["start_iso"],
            "end": c["end_iso"], "result": result, "at": NOW.isoformat()}


def event(date, start, end, summary="Dentist", end_date=None):
    return {"start": datetime.fromisoformat("{}T{}".format(date, start)),
            "end": datetime.fromisoformat("{}T{}".format(end_date or date,
                                                         end)),
            "summary": summary, "all_day": False}


def ids(targets):
    return [t[0]["mindbody_id"] for t in targets]


def plan(classes, st=None, events=(), now=NOW, **kw):  # noqa
    return ab.plan(classes, RULES, now, st or state(), list(events), **kw)


# --- config ------------------------------------------------------------

def config_matches_connors_rules():
    """Run with --config: has config.yaml drifted from Connor's rules?"""
    live = ab.load_rules()
    for k in ("earliest_hour", "book_days_ahead", "max_spots_left",
              "max_per_day", "buffer_minutes", "min_lead_minutes"):
        assert live[k] == RULES[k], (k, live[k], RULES[k])
    assert live["classes"] == RULES["classes"], live["classes"]
    assert live.get("enabled", True) is True


def test_live_config_is_valid():
    ab.load_rules()  # raises ValueError if malformed


def test_bad_config_is_rejected():
    for bad in ["max_per_day: two", "earliest_hour: 25", "enabled: maybe",
                "max_spots_left: -1"]:
        text = RULES_YAML.replace("  max_per_day: 2", "  " + bad) \
            if "max_per_day" in bad else RULES_YAML.replace(
                "  signin_reset: 0", "  signin_reset: 0\n  " + bad)
        try:
            ab.load_rules(text=text)
        except ValueError:
            continue
        raise AssertionError("accepted " + bad)


def test_fixture_matches_connors_rules():
    names = [r["name"] for r in RULES["classes"]]
    assert names == ["zumba", "ujam", "aquacise", "posture", "mat yoga"]
    assert RULES["classes"][2]["instructor"] == "bob huff"
    assert RULES["earliest_hour"] == 11
    assert RULES["book_days_ahead"] == 1
    assert RULES["max_spots_left"] == 2
    assert RULES["max_per_day"] == 2


# --- which classes count ------------------------------------------------

def test_class_types():
    t = lambda **kw: ab.class_type(cls(**kw), RULES)
    assert t(name="Zumba") == "zumba"
    assert t(name="UJAM and Stretch") == "ujam"
    assert t(name="Aqua:  Aquacise", instructor="BOB HUFF") == "aquacise"
    assert t(name="Aqua: Aquacise", instructor="") is None
    assert t(name="Aqua: Aquacise", instructor="Cathy Steen") is None
    assert t(name="Aqua: Aquacise", instructor="Bobbie Ray") is None
    assert t(name="Aqua: Aquacise", instructor="Bob Martinez") is None
    assert t(name="Aqua: Aquacise", instructor="Bob  Huff") == "aquacise"
    assert t(name="Posture, Balance, Core and Strength") == "posture"
    assert t(name="Mat Yoga") == "mat yoga"
    assert t(name="Pilates Mat") is None
    assert t(name="Balance, Strength and Stretch") is None
    assert t(name="CLUB: Zumba", club=True) is None
    assert t(name="Zumba", cancelled=True) is None
    assert t(name="Tai Chi") is None


# --- when a class is due -------------------------------------------------

def test_day_before_is_due():
    assert plan([cls(date=TOMORROW, reg=3)])


def test_far_ahead_not_due_until_nearly_full():
    assert not plan([cls(date=LATER, reg=22)])          # 3 left
    assert plan([cls(date=LATER, reg=23)])              # 2 left
    assert plan([cls(date=LATER, reg=24)])              # 1 left


def test_never_before_11am():
    assert not plan([cls(time="10:59", reg=24)])
    assert not plan([cls(time="10:00", date=TOMORROW)])
    assert plan([cls(time="11:00")])


def test_full_waitlist_and_overbooked_classes_skipped():
    assert not plan([cls(reg=25)])                       # full
    assert not plan([cls(reg=30, bookable=False)])       # waitlist only
    assert not plan([cls(reg=10, bookable=False)])       # not open yet
    assert not plan([cls(cap=0, reg=0)])                 # drop-in
    assert not plan([cls(cap=17, reg=24, bookable=True)])  # bogus count


def test_too_soon_not_booked():
    now = datetime(2026, 10, 20, 11, 30)
    assert not plan([cls(date=TOMORROW, time="13:00")], now=now)  # 90 min
    assert plan([cls(date=TOMORROW, time="13:30")], now=now)      # 2 h


def test_same_day_catch_up_allowed():
    now = datetime(2026, 10, 20, 7, 0)
    assert plan([cls(date="2026-10-20", time="13:00")], now=now)


# --- one per class per day, latest wins -----------------------------------

def test_latest_of_same_class_wins():
    a = cls(time="11:00", cid="a")
    b = cls(time="17:30", cid="b")
    c = cls(time="13:00", cid="c")
    assert ids(plan([a, b, c])) == ["b"]


def test_one_per_class_per_day():
    a = cls(time="11:00", cid="a")
    b = cls(time="17:30", cid="b")
    assert ids(plan([a, b], state(b=booked(b)))) == []


def test_latest_that_fits_when_latest_clashes():
    a = cls(time="11:00", cid="a")
    b = cls(time="17:30", cid="b")
    ev = [event(TOMORROW, "17:00", "19:00", "Dinner")]
    assert ids(plan([a, b], events=ev)) == ["a"]


# --- two a day, priority, no overlaps -----------------------------------

def test_max_two_a_day_by_priority():
    z = cls("Zumba", time="17:30", cid="z")
    m = cls("Mat Yoga", time="14:00", cid="m", cap=17, reg=5)
    u = cls("UJAM and Stretch", time="13:00", cid="u", cap=30, reg=5)
    assert sorted(ids(plan([m, u, z]))) == ["u", "z"]


def test_beths_own_classes_count_toward_two():
    z = cls("Zumba", time="17:30", cid="z")
    u = cls("UJAM and Stretch", time="11:00", cid="u", cap=30, reg=5)
    ev = [event(TOMORROW, "13:00", "13:50", " Aqua: Aquacise"),
          event(TOMORROW, "15:00", "15:50", "Mat Yoga")]
    assert ids(plan([z, u], events=ev)) == []
    ev = ev[:1]
    assert ids(plan([z, u], events=ev)) == ["z"]


def test_her_own_zumba_blocks_ours():
    z = cls("Zumba", time="17:30", cid="z")
    ev = [event(TOMORROW, "11:00", "11:50", "Zumba")]
    assert ids(plan([z], events=ev)) == []


def test_mindbody_app_copy_of_our_booking_not_double_counted():
    z = cls("Zumba", time="13:00", cid="z")
    u = cls("UJAM and Stretch", time="17:00", cid="u", cap=30, reg=5)
    st = state(z=booked(z))
    ev = [event(TOMORROW, "13:00", "13:50", "Zumba")]   # app's copy
    assert ids(plan([z, u], st, ev)) == ["u"]


def test_no_overlap_with_buffer():
    z = cls("Zumba", time="13:00", minutes=50, cid="z")      # 1:00-1:50
    u = cls("UJAM and Stretch", time="14:00", cid="u", cap=30, reg=5)
    m = cls("Mat Yoga", time="14:05", cid="m", cap=17, reg=5)
    # UJAM at 2:00 is only 10 min after Zumba ends: too tight
    assert ids(plan([z, u])) == ["z"]
    assert ids(plan([z, cls("UJAM and Stretch", time="14:05", cid="u2",
                            cap=30, reg=5)])) == ["z", "u2"]
    assert "m" in ids(plan([z, m]))


def test_appointment_blocks_class():
    z = cls("Zumba", time="13:00", cid="z")
    assert ids(plan([z], events=[event(TOMORROW, "12:00", "13:00")])) == []
    assert ids(plan([z], events=[event(TOMORROW, "10:00", "12:50")])) == []
    assert ids(plan([z], events=[event(TOMORROW, "10:00", "12:45")])) == ["z"]


def test_aquacise_only_with_bob():
    bob = cls("Aqua: Aquacise", instructor="BOB HUFF", time="11:30",
              cid="bob", cap=28, reg=5, source="aquatics")
    other = cls("Aqua: Aquacise", instructor="", time="11:30",
                date="2026-10-21", cid="x", cap=28, reg=27,
                source="aquatics")
    assert ids(plan([bob, other])) == ["bob"]


def test_days_are_independent():
    a = cls(date=TOMORROW, cid="a")
    b = cls(date=LATER, reg=24, cid="b")
    assert ids(plan([a, b])) == ["a", "b"]


# --- attempts -------------------------------------------------------------

def test_failed_attempt_counts_and_is_not_retried():
    a = cls(time="11:00", cid="a")
    b = cls(time="17:30", cid="b")
    st = state(b=booked(b, "failed"))
    assert ids(plan([a, b], st)) == []


def test_failed_attempts_count_toward_two():
    z = cls("Zumba", time="17:30", cid="z")
    u = cls("UJAM and Stretch", time="11:00", cid="u", cap=30, reg=5)
    m = cls("Mat Yoga", time="14:00", cid="m", cap=17, reg=5)
    st = state(z=booked(z, "failed"), u=booked(u, "booked"))
    assert ids(plan([z, u, m], st)) == []


def test_unavailable_retried_after_an_hour():
    z = cls(cid="z")
    rec = booked(z, "unavailable")
    rec["at"] = (NOW - timedelta(minutes=30)).isoformat()
    assert ids(plan([z], state(z=rec))) == []
    rec["at"] = (NOW - timedelta(minutes=61)).isoformat()
    assert ids(plan([z], state(z=rec))) == ["z"]


def test_booked_by_beth_counts():
    z = cls("Zumba", time="17:30", cid="z")
    u = cls("UJAM and Stretch", time="11:00", cid="u", cap=30, reg=5)
    m = cls("Mat Yoga", time="14:00", cid="m", cap=17, reg=5)
    st = state(z=booked(z, "booked-by-beth"), u=booked(u, "booked"))
    assert ids(plan([z, u, m], st)) == []


# --- rehearsal (dry run) ---------------------------------------------------

def test_rehearsal_target_must_follow_the_rules():
    far = cls(date=LATER, reg=5, cid="far")
    early = cls(date=TOMORROW, time="10:00", reg=5, cid="early")
    other = cls("Pilates Mat", cid="other")
    assert ids(plan([far], only_id="far")) == ["far"]
    assert ids(plan([early], only_id="early")) == []
    assert ids(plan([other], only_id="other")) == []
    busy = state(x=booked(cls(time="17:30", cid="x")))
    assert ids(plan([far, cls(date=LATER, time="12:00", reg=5, cid="y")],
                    busy, only_id="far")) == ["far"]
    z = cls(time="13:00", cid="z")
    assert ids(plan([z], events=[event(TOMORROW, "12:30", "14:00")],
                    only_id="z")) == []


def test_cart_verdicts():
    v = ab.cart_verdict
    ok_buttons = ["Edit your booking", "Confirm booking"]
    # a plain free booking: press Confirm booking
    assert v("Your Bookings Zumba Subtotal $0.00 Total due today $0.00",
             ok_buttons) == ("confirm", "Confirm booking")
    assert v("Zumba", ["Confirm booking · $0.00"]) == \
        ("confirm", "Confirm booking · $0.00")
    # cancellation-policy fees aren't a price for booking
    policy = ("Cancellation policy Zumba Free cancellations up to 12 hours "
              "before your booking. Late cancellations will be charged "
              "$5.00. No-shows will be charged $10.00. By confirming, you "
              "agree to the full Cancellation Policy.")
    assert v(policy, ok_buttons) == ("confirm", "Confirm booking")
    # anything that costs money stops it
    assert v("Zumba drop-in $15.00", ok_buttons)[0] == "stop"
    assert v("Zumba", ["Confirm booking · $15.00"])[0] == "stop"
    assert v("Total due today $12.00 " + policy, ok_buttons)[0] == "stop"
    assert v("Zumba", ["Confirm purchase"])[0] == "stop"
    # things the bot won't do
    for text in ["Liability Waiver Type your full name to confirm",
                 "Select a booking option",
                 "A membership is required to book this class.",
                 "This booking requires payment. Please contact us to book.",
                 "You've been added to the waitlist!"]:
        assert v(text, ok_buttons)[0] == "stop", text
    # outcomes
    assert v("You're booked! A confirmation has been sent to your email.",
             []) == ("booked", "")
    assert v("You are already booked for this class.",
             ok_buttons) == ("already", "")
    assert v("Loading", []) == ("wait", "")
    # waitlist / request buttons never count as confirm
    assert v("Zumba", ["Confirm and join waitlist"])[0] == "stop"
    assert v("Zumba", ["Request booking"])[0] == "stop"
    # an unreadable page never confirms
    assert v("", ok_buttons) == ("wait", "")
    # passing errors are retried, not given up on
    assert v("An error occurred and this booking cannot be made.",
             ok_buttons)[0] == "retry"
    assert v("Your session has expired", ok_buttons)[0] == "retry"
    assert v("We're sorry, your booking failed.", ok_buttons)[0] == "retry"
    # "can no longer be booked" may mean she has it
    assert v("This can no longer be booked. You may already be booked for "
             "it, or it may no longer be available.", ok_buttons)[0] == \
        "maybe"
    # pay-later carts and family-account steps stop
    assert v("Zumba Pricing will be determined at time of service",
             ok_buttons)[0] == "stop"
    assert v("Who are you booking for? Beth Already booked Bob",
             ok_buttons)[0] == "stop"
    # another attendee's "Already booked" isn't her
    assert v("Already booked", ok_buttons) == ("confirm", "Confirm booking")
    assert v("This attendee is already booked for this class.",
             ok_buttons) == ("confirm", "Confirm booking")


def test_retry_caps():
    z = cls(cid="z")
    rec = dict(booked(z, "unavailable"), attempts=3)
    rec["at"] = (NOW - timedelta(minutes=61)).isoformat()
    assert ids(plan([z], state(z=rec))) == ["z"]
    rec["attempts"] = 4
    assert ids(plan([z], state(z=rec))) == []
    assert ids(plan([z], state(z=booked(z, "blocked")))) == []


def test_blocked_class_doesnt_use_the_days_slots():
    z = cls("Zumba", time="17:30", cid="z")
    u = cls("UJAM and Stretch", time="13:00", cid="u", cap=30, reg=5)
    st = state(z=booked(z, "blocked"))
    assert ids(plan([z, u], st)) == ["u"]


def test_watch_window():
    w = ab.watching_now
    assert not w(datetime(2026, 10, 20, 2, 59))
    assert w(datetime(2026, 10, 20, 3, 0))
    assert w(datetime(2026, 10, 20, 8, 39))
    assert not w(datetime(2026, 10, 20, 8, 40))
    assert not w(datetime(2026, 10, 20, 13, 0))


def test_watch_health_alerts_only_after_days_without_a_watch():
    sent = []
    real = ab.alert
    ab.alert = lambda st, key, *a, **k: sent.append(key) or True
    try:
        st = state()
        st["last_watch"] = ""
        ab.check_watch_health(st, datetime(2026, 10, 20, 12, 0))
        assert st["last_watch"] == "2026-10-20" and not sent
        ab.check_watch_health(st, datetime(2026, 10, 23, 12, 0))
        assert not sent
        ab.check_watch_health(st, datetime(2026, 10, 24, 12, 0))
        assert sent == ["watch-missed"]
    finally:
        ab.alert = real


def test_merge_states_keeps_both_runs_records():
    a = cls(cid="a")
    b = cls(cid="b", time="17:00")
    ours = state(a=booked(a))
    ours["alerts"] = {"k": "2026-10-19T08:00"}
    theirs = state(b=booked(b, "failed"), a=booked(a, "unavailable"))
    theirs["alerts"] = {"k": "2026-10-19T07:00", "j": "2026-10-19T06:00"}
    m = ab.merge_states(ours, theirs)
    assert m["bookings"]["a"]["result"] == "booked"
    assert m["bookings"]["b"]["result"] == "failed"
    assert m["alerts"] == {"k": "2026-10-19T08:00", "j": "2026-10-19T06:00"}
    ours["last_watch"], theirs["last_watch"] = "2026-10-19", "2026-10-18"
    assert ab.merge_states(ours, theirs)["last_watch"] == "2026-10-19"
    # a final "blocked" beats an older retryable record; more attempts win
    c = cls(cid="c")
    m = ab.merge_states(state(c=dict(booked(c, "blocked"), attempts=3)),
                        state(c=dict(booked(c, "error"), attempts=2)))
    assert m["bookings"]["c"]["result"] == "blocked"
    m = ab.merge_states(state(c=dict(booked(c, "error"), attempts=3)),
                        state(c=dict(booked(c, "error"), attempts=1)))
    assert m["bookings"]["c"]["attempts"] == 3


# --- state + sign-in pause -------------------------------------------------

def test_state_roundtrip_and_pruning():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "s.json"
        st = state(old=dict(booked(cls(date="2026-09-01")), result="booked"),
                   new=booked(cls(cid="new")))
        st["alerts"] = {"x": (NOW - timedelta(days=10)).isoformat(),
                        "y": NOW.isoformat()}
        ab.save_state(st, p, now=NOW)
        back = ab.load_state(p)
        assert set(back["bookings"]) == {"new"}
        assert set(back["alerts"]) == {"y"}
        assert ab.booked_class_ids(p) == {"new"}


def test_signin_pause_lifts_when_password_changes():
    env = dict(os.environ)
    try:
        os.environ.update(MINDBODY_EMAIL="a@b.c", MINDBODY_PASSWORD="old",
                          GOOGLE_SERVICE_ACCOUNT_KEY="k")
        st = state()
        ab.pause_signin(st, ab.SignInError("wrong", "rejected"), NOW)
        assert ab.signin_paused(st, NOW + timedelta(days=30))
        assert "old" not in json.dumps(st)
        os.environ["MINDBODY_PASSWORD"] = "new"
        assert not ab.signin_paused(st, NOW)
        os.environ["MINDBODY_PASSWORD"] = "old"
        ab.pause_signin(st, ab.SignInError("code", "otp"), NOW)
        assert ab.signin_paused(st, NOW + timedelta(hours=23))
        assert not ab.signin_paused(st, NOW + timedelta(hours=25))
    finally:
        os.environ.clear()
        os.environ.update(env)


def test_mask_keeps_only_mindbody_words():
    env = dict(os.environ)
    try:
        os.environ["MINDBODY_EMAIL"] = "jdoeson@example.com"
        os.environ["MINDBODY_PASSWORD"] = "hunter2!"
        out = ab._mask("Welcome back, Jane Doeson | jdoeson@example.com | "
                       "Confirm booking | Your Bookings | pw hunter2!")
        assert "Jane" not in out and "Doeson" not in out
        assert "jdoeson" not in out and "hunter2" not in out
        assert "Confirm booking" in out and "Your Bookings" in out
        assert ab._scrub("x hunter2! y") == "x <password> y"
    finally:
        os.environ.clear()
        os.environ.update(env)


def test_maybe_booked_counts_and_isnt_retried_or_claimed():
    late = cls(time="17:30", cid="late")
    early = cls(time="13:00", cid="early")
    st = state(late=booked(late, "maybe-booked"))
    assert ids(plan([late, early], st)) == []
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "s.json"
        st["bookings"]["early"] = booked(early, "booked-by-beth")
        ab.save_state(st, p, now=NOW)
        assert ab.booked_class_ids(p) == set()


def test_signin_pause_backs_off_and_resets():
    env = dict(os.environ)
    try:
        os.environ.update(MINDBODY_EMAIL="a@b.c", MINDBODY_PASSWORD="pw",
                          GOOGLE_SERVICE_ACCOUNT_KEY="k")
        st = state()
        otp = ab.SignInError("code", "otp")
        ab.pause_signin(st, otp, NOW)
        assert ab.signin_paused(st, NOW + timedelta(hours=23))
        assert not ab.signin_paused(st, NOW + timedelta(hours=25))
        later = NOW + timedelta(hours=25)
        ab.pause_signin(st, otp, later)                 # 2nd: 72 h
        assert ab.signin_paused(st, later + timedelta(hours=71))
        assert not ab.signin_paused(st, later + timedelta(hours=73))
        ab.pause_signin(st, otp, later + timedelta(hours=73))   # 3rd: stays
        assert ab.signin_paused(st, later + timedelta(days=60))
        # the config knob lifts it without changing the password
        assert not ab.signin_paused(st, later + timedelta(days=60), reset=1)
        # one email per pause: the "paused" reminder is held a day
        assert "signin-paused" in st["alerts"]
        # lockouts are timed, not "wrong password"
        st = state()
        ab.pause_signin(st, ab.SignInError("too many", "locked"), NOW)
        assert not ab.signin_paused(st, NOW + timedelta(hours=25))
        assert ab.LOCKED_RE.search("Too many failed attempts.")
        assert not ab.REJECTED_RE.search("Too many failed attempts.")
    finally:
        os.environ.clear()
        os.environ.update(env)


def test_gcal_listing_shows_booked():
    try:
        import gcal_sync
    except ImportError:
        return  # google libs not installed locally
    tag, line = gcal_sync.class_status(dict(cls(), autobooked=True), NOW)
    assert "booked" in tag and "cancel" in line.lower()


def main():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    if "--config" in sys.argv:
        tests = [("config_matches_connors_rules",
                  config_matches_connors_rules)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("ok   ", name)
        except Exception:
            failed += 1
            print("FAIL ", name)
            traceback.print_exc()
    print("{} passed, {} failed".format(len(tests) - failed, failed))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
