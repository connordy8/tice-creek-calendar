"""Auto-book fitness classes for Beth on Mindbody.

Logs into Mindbody, finds upcoming classes that match Beth's preferences,
and books them automatically when registration is open.

Target classes: Zumba, UJAM, Aquacise (Bob only), Posture Balance Core
& Strength, Mat Yoga — all at 11 AM or later.
"""

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

# Classes Beth wants to book (lowercase for matching)
TARGET_CLASSES = [
    {"name": "zumba", "any_instructor": True},
    {"name": "ujam", "any_instructor": True},
    {"name": "aquacise", "instructor": "bob"},
    {"name": "water aerobics", "instructor": "bob"},
    {"name": "posture balance core", "any_instructor": True},
    {"name": "mat yoga", "any_instructor": True},
]

EARLIEST_HOUR = 11  # Only book classes at 11 AM or later


def load_config():
    """Load config.yaml if available."""
    config_path = os.path.join(os.path.dirname(__file__), "config.yaml")
    if os.path.exists(config_path):
        with open(config_path) as f:
            return yaml.safe_load(f)
    return {}


def class_matches(class_name, instructor_name):
    """Check if a class matches Beth's target list."""
    cn = class_name.lower().strip()
    inst = instructor_name.lower().strip()

    for target in TARGET_CLASSES:
        target_name = target["name"]
        if target_name in cn:
            if target.get("any_instructor"):
                return True
            if target.get("instructor") and target["instructor"] in inst:
                return True
    return False


def login(page):
    """Log into Mindbody client portal."""
    email = os.environ.get("MINDBODY_EMAIL")
    password = os.environ.get("MINDBODY_PASSWORD")
    if not email or not password:
        raise RuntimeError(
            "MINDBODY_EMAIL and MINDBODY_PASSWORD env vars required")

    log.info("Navigating to Mindbody login...")
    page.goto(LOGIN_URL, wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(2000)

    # The login page may have different layouts. Try common selectors.
    # Mindbody classic login has username/password fields
    login_selectors = [
        # Classic Mindbody login
        {"user": "#su1UserName", "pass": "#su1Password",
         "btn": "#btnSu1Login"},
        # Alternative selectors
        {"user": "input[name='requiredtxtUserName']",
         "pass": "input[name='requiredtxtPassword']",
         "btn": "input[name='btnLogin']"},
        # Generic fallbacks
        {"user": "input[type='email']", "pass": "input[type='password']",
         "btn": "button[type='submit']"},
        {"user": "input[name='username']", "pass": "input[name='password']",
         "btn": "button[type='submit']"},
    ]

    logged_in = False
    for sel in login_selectors:
        try:
            user_field = page.query_selector(sel["user"])
            pass_field = page.query_selector(sel["pass"])
            if user_field and pass_field:
                log.info("Found login form (selector: {})".format(sel["user"]))
                user_field.fill(email)
                pass_field.fill(password)
                page.wait_for_timeout(500)

                btn = page.query_selector(sel["btn"])
                if btn:
                    btn.click()
                else:
                    pass_field.press("Enter")

                page.wait_for_timeout(3000)
                page.wait_for_load_state("networkidle", timeout=15000)
                logged_in = True
                break
        except Exception as e:
            log.debug("Selector {} failed: {}".format(sel["user"], e))
            continue

    if not logged_in:
        # Try to find any visible login form
        page.screenshot(path="debug/login_page.png")
        log.warning("Could not find login form. Screenshot saved to debug/")
        raise RuntimeError("Login form not found")

    # Check if login succeeded
    page.wait_for_timeout(2000)
    current_url = page.url.lower()
    page_text = page.inner_text("body")

    if "error" in page_text.lower() and "password" in page_text.lower():
        raise RuntimeError("Login failed — check credentials")

    log.info("Login appears successful. Current URL: {}".format(
        page.url[:80]))
    return True


def navigate_to_schedule(page, target_date=None):
    """Navigate to the class schedule for a given date."""
    if target_date is None:
        target_date = datetime.now()

    date_str = target_date.strftime("%m/%d/%Y")
    url = (
        "https://clients.mindbodyonline.com/classic/ws?studioid={}"
        "&stype=-7&sView=day&sLoc=0&date={}"
        .format(STUDIO_ID, date_str)
    )
    log.info("Loading schedule for {}...".format(
        target_date.strftime("%A %B %d")))
    page.goto(url, wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(2000)


def find_and_book_classes(page, target_date=None):
    """Find matching classes on the schedule and attempt to book them."""
    if target_date is None:
        target_date = datetime.now()

    navigate_to_schedule(page, target_date)
    booked = []
    skipped = []
    already_booked = []

    # Mindbody classic schedule uses table rows for classes
    # Each row typically has: time, class name, instructor, sign up button
    # Try multiple approaches to find class rows

    # Approach 1: Look for table rows in the schedule
    rows = page.query_selector_all(
        "tr.classRow, tr[class*='class'], "
        ".bw-session, .schedule-row, table.classSchedule tr")

    if not rows:
        # Broader search
        rows = page.query_selector_all("tr")
        log.info("Using broad row search, found {} rows".format(len(rows)))

    log.info("Found {} schedule rows".format(len(rows)))

    for row in rows:
        try:
            row_text = row.inner_text()
        except Exception:
            continue

        if not row_text.strip():
            continue

        # Try to extract class info from the row
        # Look for time pattern like "11:00 AM" or "2:30 PM"
        time_match = re.search(
            r'(\d{1,2}:\d{2}\s*[AaPp][Mm])', row_text)
        if not time_match:
            continue

        time_str = time_match.group(1).strip()
        try:
            class_time = datetime.strptime(time_str, "%I:%M %p")
            if class_time.hour < EARLIEST_HOUR:
                continue
        except ValueError:
            continue

        # Check if this row contains a target class
        row_lower = row_text.lower()
        matched = False
        for target in TARGET_CLASSES:
            if target["name"] in row_lower:
                if target.get("any_instructor"):
                    matched = True
                    break
                elif target.get("instructor"):
                    if target["instructor"] in row_lower:
                        matched = True
                        break
        if not matched:
            continue

        # Extract class name for logging
        class_name = row_text.split('\n')[0].strip()[:60]
        log.info("Found matching class: {} at {}".format(
            class_name, time_str))

        # Check if already booked
        if any(x in row_lower for x in [
                "cancel", "already", "booked", "enrolled",
                "you're in", "waitlisted"]):
            log.info("  Already booked/enrolled, skipping")
            already_booked.append(class_name)
            continue

        # Look for sign-up / book button in this row
        book_btn = None
        for selector in [
            "a[class*='sign'], a[class*='book']",
            "input[value*='Sign'], input[value*='Book']",
            "button:has-text('Sign'), button:has-text('Book')",
            "a:has-text('Sign Up'), a:has-text('Book')",
            ".SignupButton, .bookButton",
        ]:
            try:
                book_btn = row.query_selector(selector)
                if book_btn:
                    break
            except Exception:
                continue

        if not book_btn:
            # Try finding any clickable link in the row
            links = row.query_selector_all("a")
            for link in links:
                try:
                    link_text = link.inner_text().lower()
                    if any(w in link_text for w in [
                            "sign", "book", "register", "enroll"]):
                        book_btn = link
                        break
                except Exception:
                    continue

        if not book_btn:
            log.info("  No booking button found, may not be open yet")
            skipped.append(class_name)
            continue

        # Click the book button
        try:
            log.info("  Clicking book button...")
            book_btn.click()
            page.wait_for_timeout(3000)
            page.wait_for_load_state("networkidle", timeout=15000)

            # Handle confirmation dialogs/pages
            # Mindbody often has a confirmation step
            confirm_btn = None
            for sel in [
                "input[value*='Confirm'], input[value*='Make']",
                "button:has-text('Confirm'), button:has-text('Complete')",
                "a:has-text('Confirm'), a:has-text('Complete')",
                "#SubmitEnroll, #btnConfirm",
            ]:
                try:
                    confirm_btn = page.query_selector(sel)
                    if confirm_btn:
                        break
                except Exception:
                    continue

            if confirm_btn:
                log.info("  Confirming booking...")
                confirm_btn.click()
                page.wait_for_timeout(3000)
                page.wait_for_load_state("networkidle", timeout=15000)

            # Check for success indicators
            body_text = page.inner_text("body").lower()
            if any(w in body_text for w in [
                    "successfully", "confirmed", "you're booked",
                    "you are enrolled", "added to your schedule"]):
                log.info("  ✅ Booked successfully!")
                booked.append(class_name)
            elif any(w in body_text for w in [
                    "waitlist", "full", "no spots"]):
                log.info("  ⚠️ Class is full, may be on waitlist")
                booked.append("{} (waitlist)".format(class_name))
            else:
                log.info("  Booking submitted (unconfirmed)")
                booked.append(class_name)

            # Navigate back to schedule for next class
            navigate_to_schedule(page, target_date)

        except Exception as e:
            log.warning("  Failed to book: {}".format(e))
            skipped.append(class_name)
            # Navigate back to schedule
            try:
                navigate_to_schedule(page, target_date)
            except Exception:
                pass

    return booked, skipped, already_booked


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

    return total_booked, total_skipped, total_already


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    days = 7
    if len(sys.argv) > 1:
        try:
            days = int(sys.argv[1])
        except ValueError:
            pass

    run_auto_booking(days_ahead=days)
