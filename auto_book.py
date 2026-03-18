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
    page.goto(LOGIN_URL, timeout=30000)
    page.wait_for_timeout(3000)

    # Check if already logged in
    body_text = page.inner_text("body")
    if "sign out" in body_text.lower() or "welcome" in body_text.lower():
        log.info("Already logged in!")
        return True

    # Find and fill login form
    user_field = page.query_selector("#su1UserName")
    pass_field = page.query_selector("#su1Password")
    login_btn = page.query_selector("#btnSu1Login")

    if not user_field or not pass_field:
        page.screenshot(path="debug/login_page.png")
        raise RuntimeError("Login form not found")

    log.info("Found login form, submitting credentials...")
    user_field.fill(email)
    pass_field.fill(password)
    page.wait_for_timeout(500)

    if login_btn:
        login_btn.click()
    else:
        pass_field.press("Enter")

    # Wait for navigation after login
    page.wait_for_timeout(5000)

    # Check if login succeeded — look for "Welcome" or "Sign Out"
    body_text = page.inner_text("body")
    if "sign out" in body_text.lower() or "welcome" in body_text.lower():
        log.info("Login successful!")
        page.screenshot(path="debug/login_success.png")
        return True

    if "error" in body_text.lower() or "invalid" in body_text.lower():
        page.screenshot(path="debug/login_failed.png")
        raise RuntimeError("Login failed — invalid credentials")

    # Might still be logged in, just a different page
    log.info("Login submitted. Current URL: {}".format(page.url[:80]))
    page.screenshot(path="debug/login_page.png")
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


def find_and_book_classes(page, target_date=None):
    """Find matching classes on the schedule and attempt to book them."""
    if target_date is None:
        target_date = datetime.now()

    navigate_to_schedule(page, target_date)
    booked = []
    skipped = []
    already_booked = []

    # The Mindbody classic schedule renders classes as a list/grid,
    # not a traditional HTML table. Parse the full page text to find
    # classes, then use links on the page to book them.
    body_text = page.inner_text("body")
    log.info("Schedule page length: {} chars".format(len(body_text)))

    # Find all clickable links on the page — these contain sign-up links
    all_links = page.query_selector_all("a")
    log.info("Found {} links on page".format(len(all_links)))

    # Parse schedule text to find matching classes
    # Format from logs: "11:00 am PDT | UJAM and Stretch | SABRINA ..."
    # Or: "(8 Reserved, 0 Open) | Zumba Club | INSTRUCTOR | ..."
    lines = body_text.split('\n')

    for i, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue

        # Look for time pattern
        time_match = re.search(
            r'(\d{1,2}:\d{2}\s*[AaPp][Mm])', line, re.IGNORECASE)
        if not time_match:
            continue

        time_str = time_match.group(1).strip().upper()
        try:
            class_time = datetime.strptime(
                time_str.replace(" AM", " AM").replace(" PM", " PM"),
                "%I:%M %p")
            if class_time.hour < EARLIEST_HOUR:
                continue
        except ValueError:
            continue

        # Combine this line with nearby lines for context
        context = " ".join(
            lines[max(0, i-1):min(len(lines), i+3)]).lower()

        # Check if any target class is in the context
        matched_target = None
        for target in TARGET_CLASSES:
            if target["name"] in context:
                if target.get("any_instructor"):
                    matched_target = target
                    break
                elif target.get("instructor"):
                    if target["instructor"] in context:
                        matched_target = target
                        break

        if not matched_target:
            continue

        class_desc = line.strip()[:80]
        log.info("Found matching class: {} (matched: {})".format(
            class_desc, matched_target["name"]))

        # Check if already booked
        if any(x in context for x in [
                "cancel my", "you're in", "enrolled"]):
            log.info("  Already enrolled, skipping")
            already_booked.append(class_desc)
            continue

        # Find the sign-up link for this class
        # Look for links near this text that say "Sign Up" or "Book"
        # or links that contain the class session ID
        book_link = None

        # Method 1: Find links with sign-up text
        for link in all_links:
            try:
                link_text = link.inner_text().strip().lower()
                href = link.get_attribute("href") or ""

                # Check if this is a sign-up/book link
                if any(w in link_text for w in [
                        "sign up", "book", "register", "enroll",
                        "add to cart"]):
                    # Check if it's near our class by looking at
                    # surrounding text
                    parent = link.evaluate(
                        "el => el.closest('div, td, li, section')"
                        " && el.closest('div, td, li, section')"
                        ".innerText")
                    if parent and matched_target["name"] in str(
                            parent).lower():
                        book_link = link
                        break
                # Also check for links with class IDs in the href
                elif "AddClass" in href or "enroll" in href.lower():
                    parent = link.evaluate(
                        "el => el.closest('div, td, li, section')"
                        " && el.closest('div, td, li, section')"
                        ".innerText")
                    if parent and matched_target["name"] in str(
                            parent).lower():
                        book_link = link
                        break
            except Exception:
                continue

        if not book_link:
            # Method 2: Try to find any sign-up link by checking
            # all links that mention the class name
            for link in all_links:
                try:
                    href = link.get_attribute("href") or ""
                    if "AddClass" in href or "SignUp" in href:
                        # Get parent text to match class
                        parent_text = link.evaluate(
                            "el => {"
                            "  let p = el.parentElement;"
                            "  for(let i=0; i<5 && p; i++) {"
                            "    if(p.innerText.length > 20) "
                            "      return p.innerText;"
                            "    p = p.parentElement;"
                            "  }"
                            "  return '';"
                            "}")
                        if parent_text and matched_target["name"] in str(
                                parent_text).lower():
                            book_link = link
                            log.info("  Found sign-up link via href")
                            break
                except Exception:
                    continue

        if not book_link:
            log.info("  No booking link found — registration may "
                     "not be open yet")
            skipped.append(class_desc)
            continue

        # Click the booking link
        try:
            log.info("  Clicking sign-up link...")
            book_link.click()
            page.wait_for_timeout(4000)

            page.screenshot(path="debug/booking_{}.png".format(
                target_date.strftime("%m%d")))

            # Handle confirmation page
            confirm_selectors = [
                "input[value*='Make Single Payment']",
                "input[value*='Confirm']",
                "input[value*='Complete']",
                "#SubmitEnroll",
                "a:has-text('Confirm')",
                "button:has-text('Confirm')",
                "input[type='submit']",
            ]

            for sel in confirm_selectors:
                try:
                    confirm_btn = page.query_selector(sel)
                    if confirm_btn and confirm_btn.is_visible():
                        log.info("  Confirming with: {}".format(sel))
                        confirm_btn.click()
                        page.wait_for_timeout(3000)
                        break
                except Exception:
                    continue

            # Check result
            result_text = page.inner_text("body").lower()
            if any(w in result_text for w in [
                    "successfully", "confirmed", "you're booked",
                    "you are enrolled", "thank you",
                    "added to your schedule"]):
                log.info("  ✅ Booked successfully!")
                booked.append(class_desc)
            elif any(w in result_text for w in [
                    "waitlist", "full", "no spots"]):
                log.info("  ⚠️ Class full, may be waitlisted")
                booked.append("{} (waitlist)".format(class_desc))
            elif any(w in result_text for w in [
                    "error", "failed", "unable"]):
                log.warning("  ❌ Booking may have failed")
                skipped.append(class_desc)
            else:
                log.info("  Booking submitted")
                booked.append(class_desc)

            # Navigate back
            navigate_to_schedule(page, target_date)

        except Exception as e:
            log.warning("  Failed to book: {}".format(e))
            skipped.append(class_desc)
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
