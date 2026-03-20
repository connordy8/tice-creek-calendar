"""Auto-book fitness classes for Beth on Mindbody.

Logs into Mindbody, finds upcoming classes that match Beth's preferences,
and books them automatically when registration is open. Then checks
Beth's actual enrolled schedule and syncs it to Google Calendar.

Target classes: Zumba (10 AM+), UJAM, Aquacise (Bob only), ForeverFit
(Bob only), Posture Balance Core & Strength, Mat Yoga — all at 11 AM
or later unless noted.
"""

import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta

import yaml

log = logging.getLogger("auto_book")

STUDIO_ID = "72039"
LOGIN_URL = (
    "https://clients.mindbodyonline.com/ASP/su1.asp?studioid={}"
    .format(STUDIO_ID)
)
SCHEDULE_URL = (
    "https://clients.mindbodyonline.com/classic/ws?studioid={}"
    "&stype=-7&sView=day&sLoc=0"
    .format(STUDIO_ID)
)

# Classes Beth wants to book (lowercase for matching).
# Each entry has keywords that ALL must appear in the class text.
# earliest_hour overrides the default for specific classes.
TARGET_CLASSES = [
    {"keywords": ["zumba"], "any_instructor": True, "earliest_hour": 10},
    {"keywords": ["ujam"], "any_instructor": True},
    {"keywords": ["aquacise"], "instructor": "bob"},
    {"keywords": ["aqua"], "instructor": "bob"},
    {"keywords": ["water", "aerobics"], "instructor": "bob"},
    {"keywords": ["posture", "balance"], "any_instructor": True},
    {"keywords": ["mat", "yoga"], "any_instructor": True},
    {"keywords": ["pickleball", "novice"], "any_instructor": True},
    {"keywords": ["let's", "stretch"], "any_instructor": True},
    {"keywords": ["lets", "stretch"], "any_instructor": True},
    {"keywords": ["foreverfit"], "instructor": "bob"},
    {"keywords": ["forever", "fit"], "instructor": "bob"},
]

DEFAULT_EARLIEST_HOUR = 11  # Most classes: 11 AM or later

# Mindbody "My Schedule" URL — shows Beth's enrolled classes
MY_SCHEDULE_URL = (
    "https://clients.mindbodyonline.com/classic/ws?studioid={}"
    "&stype=-7&sView=week&sLoc=0&sTG=22"
    .format(STUDIO_ID)
)

# Google Calendar event ID prefix for auto-booked classes.
# MUST NOT start with "be0ca1" (the scraper's prefix) to avoid
# the scraper's cleanup deleting our events.
BOOKED_EVENT_PREFIX = "ab00ce0d"  # "ab" = auto-booked


def load_config():
    """Load config.yaml if available."""
    config_path = os.path.join(os.path.dirname(__file__), "config.yaml")
    if os.path.exists(config_path):
        with open(config_path) as f:
            return yaml.safe_load(f)
    return {}


def class_matches(text):
    """Check if text matches any of Beth's target classes.

    Returns the matched target dict, or None.
    """
    t = text.lower()
    for target in TARGET_CLASSES:
        if all(kw in t for kw in target["keywords"]):
            if target.get("any_instructor"):
                return target
            if target.get("instructor") and target["instructor"] in t:
                return target
    return None


def login(page):
    """Log into Mindbody from the schedule page.

    We log in from the schedule page itself (click "Sign In" button)
    so the session stays active on that page.
    """
    email = os.environ.get("MINDBODY_EMAIL")
    password = os.environ.get("MINDBODY_PASSWORD")
    if not email or not password:
        raise RuntimeError(
            "MINDBODY_EMAIL and MINDBODY_PASSWORD env vars required")

    # Go to the schedule page first
    log.info("Navigating to class schedule...")
    page.goto(SCHEDULE_URL, timeout=30000)
    page.wait_for_timeout(3000)

    # Check if already logged in
    body_text = page.inner_text("body")
    if "sign out" in body_text.lower() or "welcome" in body_text.lower():
        log.info("Already logged in!")
        return True

    # Click the "Sign In" link on the schedule page
    sign_in_link = page.query_selector("a:has-text('Sign In')")
    if not sign_in_link:
        sign_in_link = page.query_selector(
            "a[href*='su1'], a[href*='login'], a[href*='Login']")

    if sign_in_link:
        log.info("Clicking Sign In link on schedule page...")
        sign_in_link.click()
        page.wait_for_timeout(3000)
    else:
        # Navigate to login URL directly
        log.info("No Sign In link found, going to login URL...")
        page.goto(LOGIN_URL, timeout=30000)
        page.wait_for_timeout(3000)

    page.screenshot(path="debug/login_form.png")

    # Find and fill login form
    user_field = (
        page.query_selector("#su1UserName")
        or page.query_selector("input[name*='UserName']")
        or page.query_selector("input[type='email']")
    )
    pass_field = (
        page.query_selector("#su1Password")
        or page.query_selector("input[name*='Password']")
        or page.query_selector("input[type='password']")
    )

    if not user_field or not pass_field:
        page.screenshot(path="debug/login_page.png")
        raise RuntimeError("Login form not found")

    log.info("Found login form, submitting credentials...")
    user_field.fill(email)
    pass_field.fill(password)
    page.wait_for_timeout(500)

    login_btn = (
        page.query_selector("#btnSu1Login")
        or page.query_selector("input[type='submit']")
        or page.query_selector("button[type='submit']")
    )
    if login_btn:
        login_btn.click()
    else:
        pass_field.press("Enter")

    # Wait for login to complete
    page.wait_for_timeout(5000)
    page.screenshot(path="debug/after_login.png")

    # Check if login succeeded
    body_text = page.inner_text("body")
    if "sign out" in body_text.lower() or "welcome" in body_text.lower():
        log.info("Login successful!")
        return True

    if "invalid" in body_text.lower() or "incorrect" in body_text.lower():
        page.screenshot(path="debug/login_failed.png")
        raise RuntimeError("Login failed — check credentials")

    log.info("Login submitted. URL: {}".format(page.url[:80]))
    return True


def navigate_to_schedule(page, target_date=None):
    """Navigate to the class schedule for a given date."""
    if target_date is None:
        target_date = datetime.now()

    date_str = target_date.strftime("%m/%d/%Y")
    # Mindbody classic group class schedule URL
    url = (
        "https://clients.mindbodyonline.com/classic/ws?studioid={}"
        "&stype=-7&sView=day&sLoc=0&date={}"
        .format(STUDIO_ID, date_str)
    )
    log.info("Loading schedule for {}...".format(
        target_date.strftime("%A %B %d")))
    page.goto(url, timeout=30000)
    page.wait_for_timeout(3000)

    # Take a screenshot for debugging
    page.screenshot(path="debug/schedule_{}.png".format(
        target_date.strftime("%m%d")))


def find_reservable_classes(page):
    """Use JavaScript to find all Reserve buttons and their associated class info.

    Walks up the DOM from each Reserve button to find the class name,
    instructor, and time — regardless of HTML structure (tr, div, etc.).
    Returns a list of dicts with class info and a selector to click.
    """
    return page.evaluate("""() => {
        const results = [];
        // Find all elements that look like Reserve/Sign Up buttons
        const candidates = [
            ...document.querySelectorAll('input[value*="Reserve"]'),
            ...document.querySelectorAll('input[value*="Sign Up"]'),
            ...document.querySelectorAll('a'),
        ].filter(el => {
            const text = (el.value || el.innerText || '').toLowerCase();
            return text.includes('reserve') || text.includes('sign up');
        });

        candidates.forEach((btn, idx) => {
            // Mark the button with a data attribute
            btn.setAttribute('data-autobook-idx', idx.toString());

            // Mindbody classic uses div.oddRow / div.evenRow for class rows.
            // The row contains: time, reservation count, class name, instructor.
            const row = btn.closest('.oddRow, .evenRow, tr');
            const rowText = row ? (row.innerText || '') : '';

            results.push({
                idx: idx,
                rowText: rowText.substring(0, 500).replace(/\\n/g, ' | '),
                btnTag: btn.tagName,
                btnText: (btn.value || btn.innerText || '').substring(0, 50),
            });
        });
        return results;
    }""")


def find_and_book_classes(page, target_date=None):
    """Find matching classes on the schedule and attempt to book them."""
    if target_date is None:
        target_date = datetime.now()

    navigate_to_schedule(page, target_date)
    booked = []
    skipped = []
    already_booked = []

    # Use JavaScript to find all reservable classes and their context
    reservable = find_reservable_classes(page)
    log.info("Found {} reserve/sign-up buttons on page".format(
        len(reservable)))

    for entry in reservable:
        log.info("  Button {}: {} — row: {}".format(
            entry["idx"], entry["btnText"],
            entry.get("rowText", "")[:150]))

    # Also get the full page text for club class detection
    body_text = page.inner_text("body")

    # For each reserve button, check if it matches a target class
    for entry in reservable:
        ctx = entry.get("rowText", "").lower()

        # Extract time from context
        time_match = re.search(
            r'(\d{1,2}:\d{2}\s*[AaPp][Mm])', ctx, re.IGNORECASE)
        if not time_match:
            continue

        time_str = time_match.group(1).strip().upper()
        try:
            class_time = datetime.strptime(time_str, "%I:%M %p")
        except ValueError:
            continue

        # Check if this matches a target class
        matched_target = class_matches(ctx)
        if not matched_target:
            continue

        # Apply per-class or default earliest hour
        earliest = matched_target.get(
            "earliest_hour", DEFAULT_EARLIEST_HOUR)
        if class_time.hour < earliest:
            continue

        class_desc = "{} at {}".format(
            " ".join(matched_target["keywords"]).title(), time_str)
        log.info("Matched: {} (button {})".format(
            class_desc, entry["idx"]))

        # Check if already enrolled
        if any(x in ctx for x in [
                "registered!", "cancel my", "you're in",
                "you are enrolled"]):
            log.info("  Already enrolled, skipping")
            already_booked.append(class_desc)
            continue

        # Click the reserve button using the data attribute we set
        try:
            selector = '[data-autobook-idx="{}"]'.format(entry["idx"])
            btn = page.query_selector(selector)
            if not btn:
                log.warning("  Could not re-find button {}".format(
                    entry["idx"]))
                skipped.append(class_desc)
                continue

            log.info("  Clicking reserve button...")
            btn.click()
            page.wait_for_timeout(4000)

            page.screenshot(path="debug/booking_{}_{}.png".format(
                target_date.strftime("%m%d"), entry["idx"]))

            # Log what we see after clicking Reserve
            post_click_text = page.inner_text("body")[:500].lower()
            log.info("  After click: {}".format(
                post_click_text[:200].replace('\n', ' | ')))

            # Find all clickable buttons/inputs on the page
            visible_buttons = page.evaluate("""() => {
                const btns = [];
                document.querySelectorAll(
                    'input[type="submit"], button, a'
                ).forEach(el => {
                    const text = (el.value || el.innerText || '').trim();
                    if (text && text.length > 1 && text.length < 50
                        && el.offsetParent !== null) {
                        btns.push(text.substring(0, 60));
                    }
                });
                return btns.slice(0, 10);
            }""")
            log.info("  Visible buttons: {}".format(visible_buttons))

            # Handle confirmation or waitlist page
            # Mindbody may show a "Join Waitlist" button if class is full
            confirm_selectors = [
                "input[value*='Join Waitlist']",
                "a:has-text('Join Waitlist')",
                "button:has-text('Join Waitlist')",
                "input[value*='Add to Waitlist']",
                "a:has-text('Add to Waitlist')",
                "input[value*='Waitlist']",
                "a:has-text('Waitlist')",
                "input[value*='Make Single Payment']",
                "input[value*='Confirm']",
                "input[value*='Complete']",
                "#SubmitEnroll",
                "a:has-text('Confirm')",
                "button:has-text('Confirm')",
                "input[type='submit']",
            ]

            clicked_confirm = False
            for sel in confirm_selectors:
                try:
                    confirm_btn = page.query_selector(sel)
                    if confirm_btn and confirm_btn.is_visible():
                        log.info("  Confirming with: {}".format(sel))
                        confirm_btn.click()
                        page.wait_for_timeout(3000)
                        clicked_confirm = True
                        page.screenshot(
                            path="debug/confirmed_{}_{}.png".format(
                                target_date.strftime("%m%d"),
                                entry["idx"]))
                        break
                except Exception:
                    continue

            if not clicked_confirm:
                log.info("  No confirm button found/clicked")

            # Check result
            result_text = page.inner_text("body").lower()
            if any(w in result_text for w in [
                    "successfully", "confirmed", "you're booked",
                    "you are enrolled", "thank you",
                    "added to your schedule"]):
                log.info("  Booked successfully!")
                booked.append(class_desc)
            elif any(w in result_text for w in [
                    "waitlist", "full", "no spots"]):
                log.info("  Class full, may be waitlisted")
                booked.append("{} (waitlist)".format(class_desc))
            elif any(w in result_text for w in [
                    "error", "failed", "unable"]):
                log.warning("  Booking may have failed")
                skipped.append(class_desc)
            else:
                log.info("  Booking submitted (checking result...)")
                booked.append(class_desc)

            # Navigate back for next class
            navigate_to_schedule(page, target_date)
            # Re-tag buttons after navigation (page reloaded)
            find_reservable_classes(page)

        except Exception as e:
            log.warning("  Failed to book: {}".format(e))
            skipped.append(class_desc)
            try:
                navigate_to_schedule(page, target_date)
            except Exception:
                pass

    # Check for target classes that are "CLUB" (no reservation needed)
    lines = body_text.split('\n')
    for i, line in enumerate(lines):
        line_lower = line.strip().lower()
        if "club" not in line_lower:
            continue
        matched = class_matches(line_lower)
        if matched:
            time_match = re.search(
                r'(\d{1,2}:\d{2}\s*[AaPp][Mm])',
                line_lower, re.IGNORECASE)
            if time_match:
                try:
                    t = datetime.strptime(
                        time_match.group(1).strip().upper(),
                        "%I:%M %p")
                    if t.hour >= matched.get(
                            "earliest_hour", DEFAULT_EARLIEST_HOUR):
                        kw = " ".join(matched["keywords"]).title()
                        desc = "{} (club — no reservation needed)".format(kw)
                        if desc not in already_booked:
                            already_booked.append(desc)
                            log.info("Club class: {} — just show up"
                                     .format(desc))
                except ValueError:
                    pass

    return booked, skipped, already_booked


def get_enrolled_classes(page):
    """Check Beth's actual enrolled/waitlisted classes on Mindbody.

    Uses TWO sources:
    1. Beth's "My Schedule" page — shows ALL classes she's signed up
       for (confirmed + waitlisted), regardless of target list.
    2. Daily schedule pages — detects club/drop-in classes matching
       her target list (no enrollment needed).

    Returns a list of dicts for calendar sync.
    """
    enrolled = []
    seen = set()  # Dedup key: (date, time, name_lower)

    log.info("Checking Beth's enrolled classes...")

    # ── Source 1: Beth's "My Schedule" page ──
    # Navigate to the account/profile page and find the Schedule tab.
    # This shows confirmed + waitlisted classes.
    my_schedule_urls = [
        ("https://clients.mindbodyonline.com/ASP/my_sch.asp"
         "?studioid={}").format(STUDIO_ID),
        ("https://clients.mindbodyonline.com/classic/myinfo"
         "?studioid={}").format(STUDIO_ID),
    ]

    schedule_text = ""
    for url in my_schedule_urls:
        page.goto(url, timeout=30000)
        page.wait_for_timeout(3000)
        body = page.inner_text("body")
        page.screenshot(path="debug/my_sched_{}.png".format(
            url.split("/")[-1].split("?")[0][:15]))
        log.info("My Schedule page ({}) — {} chars".format(
            url.split("/")[-1].split("?")[0], len(body)))

        # If the page has class-like content, use it
        if len(body) > len(schedule_text):
            schedule_text = body

    # Also try clicking "My Info" from the schedule page
    if not schedule_text or len(schedule_text) < 200:
        page.goto(SCHEDULE_URL, timeout=30000)
        page.wait_for_timeout(2000)
        my_link = page.query_selector(
            "a:has-text('My Info'), a:has-text('My Schedule'), "
            "a:has-text('My Account')")
        if my_link:
            log.info("Clicking '{}' link...".format(
                my_link.inner_text().strip()))
            my_link.click()
            page.wait_for_timeout(3000)
            schedule_text = page.inner_text("body")
            page.screenshot(path="debug/my_info_clicked.png")

    def parse_schedule_section(text, is_waitlist_section=False):
        """Parse a schedule section and return class entries."""
        entries = []
        lines = text.split('\n')

        for i, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue

            # Look for date + time pattern:
            # "Thu 3/19/2026	5:55 pm PDT"
            # or "3/19/2026  5:55 pm"
            date_time_match = re.search(
                r'(\d{1,2}/\d{1,2}/\d{4})\s+'
                r'(\d{1,2}:\d{2}\s*[AaPp][Mm])',
                line, re.IGNORECASE)
            if not date_time_match:
                continue

            date_str = date_time_match.group(1)
            time_str = date_time_match.group(2).strip()

            try:
                parsed_date = datetime.strptime(
                    date_str, "%m/%d/%Y")
                date_iso = parsed_date.strftime("%Y-%m-%d")
            except ValueError:
                continue

            # Class name: check if it's on the same tab-separated line
            # (waitlist format) or on the next line (schedule format)
            class_name = ""

            # Check for tab-separated format:
            # "3/19/2026\t5:00 pm\tUJAM and Stretch\tInstructor\t..."
            tabs = line.split("\t")
            if len(tabs) >= 3:
                # Tab-separated: field after time is the class name
                # Find which tab field contains the time
                for ti, field in enumerate(tabs):
                    if re.search(
                            r'\d{1,2}:\d{2}\s*[AaPp][Mm]',
                            field, re.IGNORECASE):
                        if ti + 1 < len(tabs):
                            class_name = tabs[ti + 1].strip()
                        break

            # If not tab-separated, look on next lines
            if not class_name or len(class_name) < 3:
                for j in range(i + 1, min(i + 5, len(lines))):
                    candidate = lines[j].strip()
                    if not candidate:
                        continue
                    # Skip very short lines (dots, icons, etc.)
                    if len(candidate) < 3:
                        continue
                    # Skip lines that look like instructor/room info
                    if candidate.startswith("Cancel") or \
                            "\tYes\t" in candidate or \
                            "Cancel" == candidate:
                        continue
                    # Skip lines with tab-separated fields
                    # (instructor row has tabs)
                    if "\t" in candidate and len(
                            candidate.split("\t")) > 2:
                        continue
                    # Skip timestamp-like lines
                    if re.match(
                            r'\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}',
                            candidate):
                        continue
                    class_name = candidate
                    break

            if not class_name or len(class_name) < 3:
                continue

            entries.append({
                "name": class_name,
                "date": date_iso,
                "time": time_str,
                "is_waitlist": is_waitlist_section,
                "is_club": False,
                "raw": line[:200],
                "keywords": [],
            })

        return entries

    # Parse the "My Schedule" page for enrolled classes.
    if schedule_text:
        log.info("Parsing My Schedule page ({} chars)...".format(
            len(schedule_text)))
        for chunk_line in schedule_text[:3000].split('\n'):
            if chunk_line.strip():
                log.info("  MYSCHED: {}".format(
                    chunk_line.strip()[:150]))

        # Parse the main "My Schedule" section (confirmed classes)
        confirmed = parse_schedule_section(
            schedule_text, is_waitlist_section=False)
        for cls in confirmed:
            dedup_key = (
                cls["date"], cls["time"].upper(),
                cls["name"].lower()[:20])
            if dedup_key not in seen:
                seen.add(dedup_key)
                enrolled.append(cls)
                log.info("  {} (ENROLLED) {} @ {}".format(
                    cls["date"], cls["name"][:60], cls["time"]))

        log.info("Found {} confirmed classes from My Schedule".format(
            len(confirmed)))

    # Now click the "Waitlist" tab to get waitlisted classes
    log.info("Checking Waitlist tab...")
    # Navigate back to my_sch.asp
    page.goto(my_schedule_urls[0], timeout=30000)
    page.wait_for_timeout(2000)

    waitlist_link = page.query_selector(
        "a:has-text('Waitlist'), a:has-text('waitlist')")
    if waitlist_link:
        wl_text = waitlist_link.inner_text().strip()
        log.info("Found Waitlist tab: '{}'".format(wl_text))
        waitlist_link.click()
        page.wait_for_timeout(3000)
        page.screenshot(path="debug/my_waitlist.png")

        wl_body = page.inner_text("body")
        log.info("Waitlist page ({} chars):".format(len(wl_body)))
        for chunk_line in wl_body[:2000].split('\n'):
            if chunk_line.strip():
                log.info("  WAITLIST: {}".format(
                    chunk_line.strip()[:150]))

        wl_entries = parse_schedule_section(
            wl_body, is_waitlist_section=True)
        for cls in wl_entries:
            dedup_key = (
                cls["date"], cls["time"].upper(),
                cls["name"].lower()[:20])
            if dedup_key not in seen:
                seen.add(dedup_key)
                enrolled.append(cls)
                log.info("  {} (WAITLISTED) {} @ {}".format(
                    cls["date"], cls["name"][:60], cls["time"]))

        log.info("Found {} waitlisted classes".format(len(wl_entries)))
    else:
        log.info("No Waitlist tab found")

    log.info("Found {} classes from My Schedule page".format(len(enrolled)))

    # ── Source 2: Daily schedule pages — club/drop-in classes ──
    # Club classes don't require enrollment, so they won't appear on
    # "My Schedule". Check the daily schedule for target club classes.
    log.info("Checking daily schedules for club/drop-in classes...")
    today = datetime.now()
    for day_offset in range(14):
        target = today + timedelta(days=day_offset)
        date_str = target.strftime("%m/%d/%Y")
        url = (
            "https://clients.mindbodyonline.com/classic/ws?studioid={}"
            "&stype=-7&sView=day&sLoc=0&date={}"
            .format(STUDIO_ID, date_str)
        )
        page.goto(url, timeout=30000)
        page.wait_for_timeout(2000)

        all_rows = page.evaluate("""() => {
            const results = [];
            const rows = document.querySelectorAll('.oddRow, .evenRow');
            rows.forEach(row => {
                const text = (row.innerText || '').trim();
                if (text.length > 20) {
                    results.push(text.substring(0, 500));
                }
            });
            return results;
        }""")

        for row_text in all_rows:
            lower = row_text.lower()

            # Only looking for club classes here
            if "club class" not in lower and "club:" not in lower:
                continue

            m = class_matches(row_text)
            if not m:
                continue

            time_match = re.search(
                r'(\d{1,2}:\d{2}\s*[AaPp][Mm])',
                row_text, re.IGNORECASE)
            if not time_match:
                continue

            time_str = time_match.group(1).strip()
            try:
                class_time = datetime.strptime(
                    time_str.upper(), "%I:%M %p")
                earliest = m.get(
                    "earliest_hour", DEFAULT_EARLIEST_HOUR)
                if class_time.hour < earliest:
                    continue
            except ValueError:
                continue

            date_iso = target.strftime("%Y-%m-%d")
            name = " ".join(m["keywords"]).title()
            dedup_key = (date_iso, time_str.upper(), name.lower()[:20])

            if dedup_key not in seen:
                seen.add(dedup_key)
                enrolled.append({
                    "name": name,
                    "date": date_iso,
                    "time": time_str,
                    "is_waitlist": False,
                    "is_club": True,
                    "raw": row_text[:200],
                    "keywords": m["keywords"],
                })
                log.info("  {} (CLUB) {} @ {}".format(
                    date_iso, name, time_str))

    log.info("Total enrolled/club classes: {}".format(len(enrolled)))
    return enrolled


def sync_enrolled_to_gcal(enrolled_classes):
    """Sync Beth's enrolled classes to Google Calendar.

    - Adds confirmed enrollments to the calendar
    - Removes classes she's no longer enrolled in
    """
    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_KEY")
    calendar_id = os.environ.get("GOOGLE_CALENDAR_ID")
    if not creds_json or not calendar_id:
        log.info("Google Calendar credentials not set — skipping sync")
        return

    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds_info = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(
        creds_info,
        scopes=["https://www.googleapis.com/auth/calendar"])
    service = build(
        "calendar", "v3", credentials=creds, cache_discovery=False)

    # Build desired events from enrolled classes
    desired = {}
    for cls in enrolled_classes:
        date_str = cls.get("date", "")
        time_str = cls.get("time", "")
        if not date_str or not time_str:
            continue

        try:
            start = datetime.strptime(
                "{} {}".format(date_str, time_str),
                "%Y-%m-%d %I:%M %p")
        except ValueError:
            try:
                start = datetime.strptime(
                    "{} {}".format(date_str, time_str),
                    "%Y-%m-%d %I:%M%p")
            except ValueError:
                log.warning("  Can't parse time: {} {}".format(
                    date_str, time_str))
                continue

        end = start + timedelta(minutes=50)  # Most classes are 50 min

        name = cls["name"]
        is_waitlist = cls.get("is_waitlist", False)

        is_water = any(
            w in name.lower() for w in ["aqua", "water", "swim"])
        emoji = "\U0001f3ca" if is_water else "\U0001f3cb\ufe0f"

        # Deterministic event ID (same ID whether waitlisted or confirmed,
        # so the event updates in-place when status changes)
        raw = "booked-{}-{}-{}".format(name, date_str, time_str)
        h = hashlib.md5(raw.encode()).hexdigest()
        eid = "{}{}".format(BOOKED_EVENT_PREFIX, h)

        is_club = cls.get("is_club", False)

        if is_waitlist:
            summary = "\u23f3 {} (waitlist)".format(name)
            description = (
                "Beth is on the WAITLIST for this class.\n"
                "The system checks every 2 hours \u2014 if a spot opens, "
                "she'll be moved to confirmed and this will update "
                "to \u2705.\n\n"
                "Managed by Beth's Calendar Bot."
            )
            color_id = "5"  # Banana (yellow) — waitlisted
        elif is_club:
            summary = "{} {} (drop-in)".format(emoji, name)
            description = (
                "No reservation needed \u2014 Beth can just show up!\n"
                "This is an open club class.\n\n"
                "Managed by Beth's Calendar Bot."
            )
            color_id = "2"  # Sage (green) — open/confirmed
        else:
            summary = "{} {} \u2705".format(emoji, name)
            description = (
                "Beth is CONFIRMED for this class!\n"
                "Reserved on Mindbody (auto-booked).\n\n"
                "Managed by Beth's Calendar Bot."
            )
            color_id = "2"  # Sage (green) — confirmed

        desired[eid] = {
            "summary": summary,
            "description": description,
            "location": (
                "Tice Creek Fitness Center, "
                "1751 Tice Creek Dr, Walnut Creek, CA 94595"
            ),
            "start": {
                "dateTime": start.strftime("%Y-%m-%dT%H:%M:%S"),
                "timeZone": "America/Los_Angeles",
            },
            "end": {
                "dateTime": end.strftime("%Y-%m-%dT%H:%M:%S"),
                "timeZone": "America/Los_Angeles",
            },
            "colorId": color_id,
        }

    log.info("Desired booked events: {}".format(len(desired)))

    # Find existing auto-booked events on calendar.
    # Use a wide window to catch all events, including ones that may
    # have shifted dates due to rescheduling.
    time_min = (
        datetime.utcnow() - timedelta(days=3)).isoformat() + "Z"
    time_max = (
        datetime.utcnow() + timedelta(days=30)).isoformat() + "Z"

    existing = {}
    all_calendar_items = []
    page_token = None
    while True:
        resp = service.events().list(
            calendarId=calendar_id,
            timeMin=time_min,
            timeMax=time_max,
            maxResults=500,
            singleEvents=True,
            showDeleted=True,  # Include cancelled events so we can
                               # resurrect them if needed
            pageToken=page_token,
        ).execute()

        for item in resp.get("items", []):
            all_calendar_items.append(item)
            eid = item.get("id", "")
            if eid.startswith(BOOKED_EVENT_PREFIX):
                existing[eid] = item

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    log.info("Found {} existing auto-booked events (out of {} total)".format(
        len(existing), len(all_calendar_items)))

    # Also find and clean up scraper-created fitness events (prefix be0ca1)
    # and any user-added duplicates that match our target class names.
    # This prevents duplicates from multiple sources.
    scraper_fitness = {}
    user_duplicates = {}
    target_keywords_flat = set()
    for t in TARGET_CLASSES:
        for kw in t["keywords"]:
            target_keywords_flat.add(kw)

    for item in all_calendar_items:
        eid = item.get("id", "")
        summary = (item.get("summary") or "").lower()

        # Skip cancelled/deleted events
        if item.get("status") == "cancelled":
            continue

        # Skip our own auto-booked events
        if eid.startswith(BOOKED_EVENT_PREFIX):
            continue

        # Scraper-created fitness events (have be0ca1 prefix + fitness emoji)
        if eid.startswith("be0ca1") and (
                "\U0001f3cb" in summary or "\U0001f3ca" in summary):
            scraper_fitness[eid] = item
            continue

        # User-added events that match target class names
        # (no managed prefix, but contain class keywords)
        for kw in target_keywords_flat:
            if kw in summary:
                user_duplicates[eid] = item
                break

    if scraper_fitness:
        log.info("Found {} scraper-created fitness events to clean up"
                 .format(len(scraper_fitness)))
    if user_duplicates:
        log.info("Found {} user-added duplicate events to clean up"
                 .format(len(user_duplicates)))
        for eid, item in user_duplicates.items():
            log.info("  Duplicate: {} on {}".format(
                item.get("summary", ""),
                item.get("start", {}).get("dateTime", "")))

    # Create/update enrolled classes
    created = 0
    updated = 0
    for eid, body in desired.items():
        if eid in existing:
            old = existing[eid]
            is_cancelled = old.get("status") == "cancelled"
            needs_update = (
                is_cancelled
                or old.get("summary") != body["summary"]
                or old.get("start", {}).get("dateTime") !=
                body["start"]["dateTime"]
                or old.get("colorId") != body.get("colorId")
            )
            if needs_update:
                # Always set status to confirmed — this resurrects
                # cancelled/deleted events.
                body["status"] = "confirmed"
                service.events().update(
                    calendarId=calendar_id,
                    eventId=eid,
                    body=body,
                ).execute()
                updated += 1
                if is_cancelled:
                    log.info("  Resurrected deleted event: {}".format(
                        body.get("summary", "")))
        else:
            body["id"] = eid
            try:
                service.events().insert(
                    calendarId=calendar_id,
                    body=body,
                ).execute()
                created += 1
            except Exception as e:
                if "409" in str(e) or "duplicate" in str(e).lower():
                    # Event ID exists (deleted event retains its ID).
                    # Fall back to update to resurrect it.
                    log.info("Event {} exists (deleted?), updating instead"
                             .format(eid[:16]))
                    try:
                        del body["id"]
                        service.events().update(
                            calendarId=calendar_id,
                            eventId=eid,
                            body=body,
                        ).execute()
                        updated += 1
                    except Exception as e2:
                        log.warning("Failed to update event {}: {}".format(
                            eid[:16], e2))
                else:
                    log.warning("Failed to create event: {}".format(e))

    # Delete events for classes she's no longer enrolled in
    deleted = 0
    for eid in existing:
        if eid not in desired:
            # Skip already-cancelled events
            if existing[eid].get("status") == "cancelled":
                continue
            try:
                service.events().delete(
                    calendarId=calendar_id,
                    eventId=eid,
                ).execute()
                deleted += 1
                log.info("  Removed from calendar: {}".format(
                    existing[eid].get("summary", "")))
            except Exception as e:
                log.warning("Failed to delete event: {}".format(e))

    # Delete scraper-created fitness events (now managed by auto-booker)
    for eid, item in scraper_fitness.items():
        try:
            service.events().delete(
                calendarId=calendar_id,
                eventId=eid,
            ).execute()
            deleted += 1
            log.info("  Removed scraper fitness event: {}".format(
                item.get("summary", "")))
        except Exception as e:
            log.warning("Failed to delete scraper event: {}".format(e))

    # Delete user-added duplicates
    for eid, item in user_duplicates.items():
        try:
            service.events().delete(
                calendarId=calendar_id,
                eventId=eid,
            ).execute()
            deleted += 1
            log.info("  Removed user duplicate: {}".format(
                item.get("summary", "")))
        except Exception as e:
            log.warning("Failed to delete user event: {}".format(e))

    log.info("Calendar sync: {} created, {} updated, {} removed".format(
        created, updated, deleted))


def run_auto_booking(days_ahead=7):
    """Main entry point: book classes for the next N days."""
    from playwright.sync_api import sync_playwright

    os.makedirs("debug", exist_ok=True)

    log.info("=" * 60)
    log.info("Beth's Auto-Booking — Tice Creek Fitness Center")
    log.info("Looking ahead {} days".format(days_ahead))
    log.info("=" * 60)

    total_booked = []
    total_skipped = []
    total_already = []
    enrolled_classes = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/121.0.0.0 Safari/537.36"
            ),
        )
        page = ctx.new_page()

        try:
            login(page)
        except Exception as e:
            log.error("Login failed: {}".format(e))
            page.screenshot(path="debug/login_failed.png")
            browser.close()
            sys.exit(1)

        today = datetime.now()
        for day_offset in range(days_ahead):
            target = today + timedelta(days=day_offset)
            log.info("")
            log.info("-" * 40)
            log.info("Checking {}...".format(
                target.strftime("%A %B %d, %Y")))
            log.info("-" * 40)

            try:
                booked, skipped, already = find_and_book_classes(
                    page, target)
                total_booked.extend(booked)
                total_skipped.extend(skipped)
                total_already.extend(already)
            except Exception as e:
                log.warning("Error on {}: {}".format(
                    target.strftime("%m/%d"), e))
                page.screenshot(path="debug/error_{}.png".format(
                    target.strftime("%m%d")))

        # After booking, check what Beth is ACTUALLY enrolled in
        log.info("")
        log.info("=" * 60)
        log.info("Checking Beth's Enrolled Schedule")
        log.info("=" * 60)
        try:
            enrolled_classes = get_enrolled_classes(page)
        except Exception as e:
            log.warning("Failed to check enrolled classes: {}".format(e))
            page.screenshot(path="debug/enrolled_error.png")

        browser.close()

    log.info("")
    log.info("=" * 60)
    log.info("Auto-Booking Summary")
    log.info("=" * 60)
    log.info("  Booked: {}".format(len(total_booked)))
    for b in total_booked:
        log.info("    ✅ {}".format(b))
    log.info("  Already enrolled: {}".format(len(total_already)))
    log.info("  Skipped (not open): {}".format(len(total_skipped)))
    log.info("  Enrolled classes found: {}".format(len(enrolled_classes)))

    # Sync enrolled classes to Google Calendar
    if enrolled_classes:
        try:
            sync_enrolled_to_gcal(enrolled_classes)
        except Exception as e:
            log.warning("Calendar sync failed: {}".format(e))
    else:
        log.info("No enrolled classes to sync — checking if we should "
                 "clear stale calendar events...")
        # If we got 0 enrolled classes, it might be a parsing issue.
        # Only clear calendar if we're confident the check worked.
        # For safety, don't delete anything if enrolled list is empty.

    return total_booked, total_skipped, total_already


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    days = 14
    if len(sys.argv) > 1:
        try:
            days = int(sys.argv[1])
        except ValueError:
            pass

    run_auto_booking(days_ahead=days)
