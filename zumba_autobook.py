"""Sign Beth up for Zumba: the day before, or when it's nearly full.

Connor's rule (Oct 2026): sign Beth up for Zumba, but don't hoard
spots days ahead (the Tice Creek staff don't like that). A class is
booked only when it's tomorrow, or when it's down to its last couple
of spots. At most one Zumba per day; when several qualify, take the
latest. Days where Beth already has a Zumba (one she booked herself,
or one we booked) are left alone.

Two phases, so the cheap check can run every few minutes:

  python zumba_autobook.py --check    # plain HTTP + Google Calendar;
                                      # prints targets, no booking
  python zumba_autobook.py            # check, then book via Playwright
  python zumba_autobook.py --dry-run  # log in and walk the booking
                                      # flow but never confirm

Booking drives Mindbody's branded-web schedule widget (the same page
Tice Creek embeds): click Sign Up, sign in through the
signin.mindbodyonline.com popup, then confirm. It never pays: any
screen showing a non-zero price or a card form aborts the booking.

Results go in autobook_state.json (committed) so a class is never
attempted twice and gcal_sync can show "✅ booked" on the listing.
The repo is public, so nothing from Beth's signed-in pages is logged;
screenshots and page text from failures are emailed to Connor.
"""

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

import tice_schedule

log = logging.getLogger("zumba_autobook")

PACIFIC = ZoneInfo("America/Los_Angeles")
STATE_FILE = Path("autobook_state.json")
DEBUG_DIR = Path("debug")

# Book once a class is down to this many open spots...
MAX_SPOTS_LEFT = 2
# ...or once it's this many days away (1 = the day before)
BOOK_DAYS_AHEAD = 1
# Don't try to book a class that starts this soon: Beth needs time to
# notice it, and Mindbody's booking window closes at class start.
MIN_LEAD_MINUTES = 60

SUCCESS_RE = re.compile(
    r"you'?re (all set|booked|signed up|registered|in)|you are (booked|"
    r"signed up|registered)|booking (is )?(confirmed|complete)|"
    r"successfully (booked|signed up|registered)|see you (there|in class)",
    re.I)
OTP_RE = re.compile(r"verification code|one-time|enter (the )?code|"
                    r"we sent (you )?a code", re.I)
# Logged-in row buttons for a class Beth holds (Mindbody's Booked /
# BookedAtThisTime / SignedIn states)
BOOKED_BUTTON_RE = re.compile(r"booked|cancel|signed in|registered", re.I)
# Buttons that move a free booking forward. Never "Pay", "Buy",
# "Purchase" or "Add card".
ADVANCE_BUTTONS = ["Book", "Book Now", "Book Class", "Confirm",
                   "Confirm Booking", "Complete Booking", "Sign Up",
                   "Reserve", "Continue", "Next", "Done"]
MONEY_RE = re.compile(r"\$\s*([0-9]+(?:\.[0-9]{2})?)")


class BookingError(RuntimeError):
    pass


def _now():
    return datetime.now(PACIFIC).replace(tzinfo=None)


# =========================================================================
# State
# =========================================================================

def load_state():
    if not STATE_FILE.exists():
        return {"booked": {}, "attempted": {}}
    state = json.loads(STATE_FILE.read_text())
    state.setdefault("booked", {})
    state.setdefault("attempted", {})
    return state


def save_state(state):
    # Drop entries more than two weeks old so the file stays small
    cutoff = (_now() - timedelta(days=14)).strftime("%Y-%m-%d")
    for key in ("booked", "attempted"):
        state[key] = {k: v for k, v in state[key].items()
                      if v.get("date", "9999") >= cutoff}
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def booked_class_ids():
    """Mindbody class ids we've booked (read by gcal_sync)."""
    try:
        return {v["mindbody_id"] for v in load_state()["booked"].values()}
    except Exception as e:
        log.warning("Couldn't read %s: %s", STATE_FILE, e)
        return set()


# =========================================================================
# Choosing what to book
# =========================================================================

def is_zumba(cls):
    return "zumba" in cls["name"].lower() and not cls["is_club"]


def spots_left(cls):
    return cls["capacity"] - cls["registered"]


def due_zumbas(classes, now):
    """Zumba classes Beth could book right now that are due: tomorrow
    (or sooner), or down to their last couple of spots."""
    soon = (now.date() + timedelta(days=BOOK_DAYS_AHEAD)).isoformat()
    out = []
    for c in classes:
        if not is_zumba(c) or c["cancelled"] or not c["bookable"]:
            continue
        if c["capacity"] <= 0 or not 1 <= spots_left(c) <= c["capacity"]:
            continue
        start = datetime.fromisoformat(c["start_iso"])
        if start - now < timedelta(minutes=MIN_LEAD_MINUTES):
            continue
        if c["date"] <= soon or spots_left(c) <= MAX_SPOTS_LEFT:
            out.append(c)
    return out


def beths_zumba_dates(first_date, last_date):
    """Dates Beth already has a Zumba on her own calendar (events we
    don't manage, e.g. ones the Mindbody app adds when she books)."""
    from gcal_sync import (ALL_PREFIXES, PACIFIC as GCAL_TZ, gcal_api_call,
                           get_calendar_service)
    service = get_calendar_service()
    start = datetime.fromisoformat(first_date).replace(tzinfo=GCAL_TZ)
    end = datetime.fromisoformat(last_date).replace(tzinfo=GCAL_TZ) + \
        timedelta(days=1)
    dates, page_token = set(), None
    while True:
        resp = gcal_api_call(
            service.events().list,
            calendarId=os.environ.get("GOOGLE_CALENDAR_ID", "primary"),
            timeMin=start.isoformat(), timeMax=end.isoformat(),
            maxResults=2500, singleEvents=True, pageToken=page_token)
        for item in resp.get("items", []):
            if item.get("id", "").startswith(ALL_PREFIXES):
                continue
            if item.get("status") == "cancelled":
                continue
            if "zumba" not in (item.get("summary") or "").lower():
                continue
            when = (item.get("start", {}).get("dateTime")
                    or item.get("start", {}).get("date") or "")
            if when:
                dates.add(when[:10])
        page_token = resp.get("nextPageToken")
        if not page_token:
            return dates


def pick_targets(classes, state, now, beth_dates):
    """One class per day: the latest due Zumba, on days Beth doesn't
    already have a Zumba."""
    by_date = {}
    for c in due_zumbas(classes, now):
        by_date.setdefault(c["date"], []).append(c)
    # A failed attempt still counts for its day: if it secretly went
    # through, trying another class would double-book her.
    tried = {v["date"] for v in state["attempted"].values()}
    targets = []
    for date, group in sorted(by_date.items()):
        if date in state["booked"]:
            log.info("  %s: already booked a Zumba for Beth; skipping", date)
            continue
        if date in tried:
            log.info("  %s: already tried booking a Zumba; skipping", date)
            continue
        if date in beth_dates:
            log.info("  %s: Beth already has a Zumba; skipping", date)
            continue
        targets.append(max(group, key=lambda c: c["start_iso"]))
    return targets


def find_targets(state):
    classes, _ = tice_schedule.fetch_schedule("group_fitness")
    now = _now()
    candidates = due_zumbas(classes, now)
    for c in [c for c in classes if is_zumba(c)]:
        log.info("  %s %-8s %-12s %s/%s registered%s", c["date"], c["time"],
                 c["name"], c["registered"], c["capacity"],
                 "  <- due" if c in candidates else "")
    if not candidates:
        return classes, []
    dates = sorted(c["date"] for c in candidates)
    beth_dates = beths_zumba_dates(dates[0], dates[-1])
    return classes, pick_targets(classes, state, now, beth_dates)


# =========================================================================
# Booking (Playwright)
# =========================================================================

def _widget_url():
    return tice_schedule.WIDGET_URL.format(
        tice_schedule.resolve_widget_id("group_fitness"))


def _snap(page, name, shots):
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        path = DEBUG_DIR / "{}-{}.png".format(len(shots) + 1, name)
        page.screenshot(path=str(path), full_page=True)
        text = page.locator("body").inner_text(timeout=5000)
        shots.append((path, page.url, text[:3000]))
    except Exception as e:
        log.warning("  (screenshot failed: %s)", e)


def _open_day(page, cls):
    """Select the class's date in the widget's week strip."""
    day = datetime.fromisoformat(cls["start_iso"])
    label = re.compile(r"^(Today|{})\s*{}$".format(day.strftime("%a"),
                                                     day.day))
    for _ in range(3):
        btn = page.locator("div[role=button]").filter(has_text=label)
        if btn.count():
            btn.first.click()
            page.wait_for_timeout(2500)
            return
        page.get_by_role("button", name="Next").click()
        page.wait_for_timeout(2000)
    raise BookingError("Couldn't find {} in the widget".format(cls["date"]))


def _class_row(page, cls):
    """The widget row for this class: matched on start time + name."""
    row = (page.locator("div.MuiGrid-container")
           .filter(has=page.locator("h6", has_text=re.compile(
               r"^\s*{}\s*$".format(re.escape(cls["time"])))))
           .filter(has=page.locator("h6", has_text=re.compile(
               r"^\s*{}\s*$".format(re.escape(cls["name"])), re.I))))
    page.wait_for_timeout(500)
    if row.count() != 1:
        raise BookingError("Expected 1 widget row for {} {}, found {}".format(
            cls["time"], cls["name"], row.count()))
    return row.first


def _row_button_text(row):
    btn = row.locator("button").last
    return btn.inner_text(timeout=10000).strip() if btn.count() else ""


def _sign_in(popup, email, password, shots):
    popup.wait_for_load_state("domcontentloaded")
    popup.locator("#username").fill(email, timeout=30000)
    popup.locator("button[type=submit], button.MuiLoadingButton-root") \
        .first.click()
    pw = popup.locator("input[type=password]")
    pw.wait_for(timeout=30000)
    pw.fill(password)
    popup.locator("button[type=submit], button.MuiLoadingButton-root") \
        .first.click()
    # On success Mindbody posts back to go.mindbodyonline.com and the
    # popup closes itself.
    for _ in range(30):
        if popup.is_closed():
            return
        popup.wait_for_timeout(1000)
        try:
            text = popup.locator("body").inner_text(timeout=2000)
        except Exception:
            continue
        if OTP_RE.search(text):
            _snap(popup, "verification-code", shots)
            raise BookingError(
                "Mindbody asked for a verification code at sign-in. "
                "Automated booking can't get past that.")
        if re.search(r"incorrect|invalid|try again|doesn'?t match", text,
                     re.I):
            _snap(popup, "sign-in-rejected", shots)
            raise BookingError("Mindbody rejected the sign-in "
                               "(MINDBODY_EMAIL / MINDBODY_PASSWORD).")
    _snap(popup, "sign-in-stuck", shots)
    raise BookingError("Sign-in popup didn't finish within 30s")


def _payment_problem(text, page):
    """Reason to refuse this screen (money or a card form), or None."""
    if page.locator("input[autocomplete^=cc-], iframe[src*=payment], "
                    "iframe[title*=card i]").count():
        return "the page asks for a payment card"
    amounts = [float(a) for a in MONEY_RE.findall(text)]
    if any(a > 0 for a in amounts):
        return "the page shows a price ({})".format(
            ", ".join("${:.2f}".format(a) for a in amounts if a > 0))
    return None


def _visible_button(scope, labels):
    for label in labels:
        btn = scope.get_by_role("button", name=re.compile(
            r"^\s*{}\s*$".format(re.escape(label)), re.I))
        for i in range(btn.count()):
            if btn.nth(i).is_visible() and btn.nth(i).is_enabled():
                return btn.nth(i), label
    return None, None


def _confirm_booking(page, cls, dry_run, shots):
    """Walk whatever screens follow sign-in until Mindbody says Beth is
    booked. Returns True when booked (False on a dry run)."""
    for step in range(6):
        page.wait_for_timeout(3000)
        dialog = page.locator("[role=dialog]:visible")
        scope = dialog.last if dialog.count() else page
        text = scope.inner_text(timeout=10000)
        _snap(page, "step{}".format(step), shots)
        if SUCCESS_RE.search(text):
            return True
        problem = _payment_problem(text, page)
        if problem:
            raise BookingError("Stopped before booking: {}. Beth's Zumba "
                               "should be free with her membership."
                               .format(problem))
        if scope is page:
            # No dialog: maybe the booking already went through and the
            # row now shows her as booked.
            try:
                state = _row_button_text(_class_row(page, cls))
            except BookingError:
                state = ""
            if BOOKED_BUTTON_RE.search(state):
                return True
        btn, label = _visible_button(scope, ADVANCE_BUTTONS)
        if btn is None:
            raise BookingError("No booking button to press on step {}"
                               .format(step))
        if dry_run:
            log.info("  DRY RUN: would press '%s' now; stopping", label)
            return False
        log.info("  Pressing '%s'", label)
        btn.click()
    raise BookingError("Booking flow didn't finish after 6 screens")


def _verify(page, cls, shots):
    """Reload the widget (still signed in) and check the row."""
    page.goto(_widget_url(), wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    _open_day(page, cls)
    state = _row_button_text(_class_row(page, cls))
    _snap(page, "verify", shots)
    log.info("  Row now shows: %s", state)
    return bool(BOOKED_BUTTON_RE.search(state))


def book(cls, shots, dry_run=False):
    """Book one class; True when booked. Screenshots are appended to
    shots as (path, url, text) so the caller can email them."""
    from playwright.sync_api import sync_playwright

    email = os.environ.get("MINDBODY_EMAIL")
    password = os.environ.get("MINDBODY_PASSWORD")
    if not email or not password:
        raise BookingError("MINDBODY_EMAIL / MINDBODY_PASSWORD not set")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1200, "height": 1000},
                                  user_agent=tice_schedule.UA,
                                  timezone_id="America/Los_Angeles")
        page = ctx.new_page()
        page.set_default_timeout(30000)
        try:
            page.goto(_widget_url(), wait_until="domcontentloaded")
            page.wait_for_timeout(4000)
            _open_day(page, cls)
            row = _class_row(page, cls)
            label = _row_button_text(row)
            if label.lower() != "sign up":
                raise BookingError("Class shows '{}' instead of Sign Up "
                                   "(probably just filled)".format(label))
            with ctx.expect_page(timeout=15000) as popup_info:
                row.get_by_role("button", name="Sign Up").click()
            _sign_in(popup_info.value, email, password, shots)
            log.info("  Signed in")
            done = _confirm_booking(page, cls, dry_run, shots)
            if dry_run:
                return False
            if not _verify(page, cls, shots) and not done:
                raise BookingError("Couldn't confirm the booking went "
                                   "through")
            return True
        except Exception:
            _snap(page, "error", shots)
            raise
        finally:
            browser.close()


# =========================================================================
# Alerts
# =========================================================================

def email_connor(subject, body, shots=()):
    """Email Connor, attaching screenshots (kept out of the public
    Actions logs because they show Beth's account)."""
    import smtplib
    import notify
    if not notify.SENDER_PASSWORD:
        log.warning("CALENDAR_EMAIL_PASSWORD not set; can't email")
        return
    msg = MIMEMultipart()
    msg["Subject"] = "[Beth Calendar] {}".format(subject)
    msg["From"] = "Beth Calendar Bot <{}>".format(notify.SENDER_EMAIL)
    msg["To"] = notify.ALERT_RECIPIENT
    parts = [body]
    if notify.GITHUB_RUN_URL:
        parts.append("\nRun: {}".format(notify.GITHUB_RUN_URL))
    for path, url, text in shots:
        parts.append("\n--- {} ({})\n{}".format(path.name, url, text))
    msg.attach(MIMEText("\n".join(parts), "plain"))
    for path, _, _ in shots:
        try:
            img = MIMEImage(path.read_bytes())
            img.add_header("Content-Disposition", "attachment",
                           filename=path.name)
            msg.attach(img)
        except Exception:
            pass
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(notify.SENDER_EMAIL, notify.SENDER_PASSWORD)
            s.send_message(msg)
    except Exception as e:
        log.error("Couldn't email Connor: %s", e)


# =========================================================================
# Main
# =========================================================================

def _describe(cls):
    return "{} {} {} {} ({}/{} registered)".format(
        cls["day"], cls["date"], cls["time"], cls["name"],
        cls["registered"], cls["capacity"])


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="only report what would be booked")
    ap.add_argument("--dry-run", action="store_true",
                    help="sign in and walk the flow, but never confirm")
    ap.add_argument("--class-id",
                    help="dry run against this Mindbody class id instead "
                         "of a due Zumba")
    args = ap.parse_args()

    state = load_state()
    log.info("Zumba classes this week:")
    classes, targets = find_targets(state)
    if args.class_id:
        targets = [c for c in classes if c["mindbody_id"] == args.class_id]
        if not targets:
            log.error("Class %s not in the schedule", args.class_id)
            sys.exit(1)
        args.dry_run = True
    if not targets:
        log.info("Nothing to book.")
        return
    for c in targets:
        log.info("Target: %s", _describe(c))
    if args.check:
        out = os.environ.get("GITHUB_OUTPUT")
        if out:
            with open(out, "a") as f:
                f.write("targets={}\n".format(len(targets)))
        return

    failed = False
    for cls in targets:
        log.info("Booking %s%s", _describe(cls),
                 " (dry run)" if args.dry_run else "")
        shots = []
        try:
            book(cls, shots, dry_run=args.dry_run)
        except Exception as e:
            log.error("  Booking failed: %s", e)
            if not args.dry_run:
                state["attempted"][cls["mindbody_id"]] = {
                    "date": cls["date"], "result": "failed",
                    "error": str(e)[:300], "at": _now().isoformat()}
            email_connor("Zumba auto-book FAILED: {} {}".format(
                cls["date"], cls["time"]),
                "Tried to sign Beth up for {} and it "
                "didn't work.\n\nError: {}\n\nIt won't retry this class. "
                "Beth may want to sign up herself.".format(
                    _describe(cls), e), shots)
            failed = True
            continue
        if args.dry_run:
            log.info("  Dry run finished at the final confirm step")
            email_connor("Zumba auto-book dry run: {} {}".format(
                cls["date"], cls["time"]),
                "Dry run reached the final confirm step for {} without "
                "booking. Screenshots attached.".format(_describe(cls)),
                shots)
            continue
        log.info("  ✅ Booked")
        state["booked"][cls["date"]] = {
            "date": cls["date"], "mindbody_id": cls["mindbody_id"],
            "name": cls["name"], "time": cls["time"],
            "at": _now().isoformat()}
        state["attempted"][cls["mindbody_id"]] = {
            "date": cls["date"], "result": "booked",
            "at": _now().isoformat()}
    if not args.dry_run:
        save_state(state)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
