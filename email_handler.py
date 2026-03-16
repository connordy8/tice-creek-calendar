#!/usr/bin/env python3
"""
Email-based calendar updates for Beth's Tice Creek calendar.

Checks a dedicated Gmail inbox (bethcalendarupdate@gmail.com) for emails
from family, helpers, or class coordinators. Uses Claude to interpret
freeform messages and extract calendar changes (add, cancel, modify).

Persists changes to manual_events.json which the main scraper merges
into the ICS output.

Usage:
    python3 email_handler.py              # Check inbox, process new emails
    python3 email_handler.py --dry-run    # Parse emails but don't save changes
"""

import imaplib
import email
import json
import os
import sys
import logging
import hashlib
from datetime import datetime, timedelta
from email.header import decode_header
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

DRY_RUN = "--dry-run" in sys.argv

# File where we persist email-sourced calendar events
MANUAL_EVENTS_FILE = Path("manual_events.json")

# How long to keep manual events before auto-expiring (days)
EVENT_TTL_DAYS = 90


# =========================================================================
# Gmail IMAP
# =========================================================================

def connect_gmail():
    """Connect to Gmail via IMAP using App Password."""
    email_addr = os.environ.get("CALENDAR_EMAIL", "bethcalendarupdate@gmail.com")
    email_pass = os.environ.get("CALENDAR_EMAIL_PASSWORD", "")

    if not email_pass:
        log.error("CALENDAR_EMAIL_PASSWORD not set")
        return None

    try:
        imap = imaplib.IMAP4_SSL("imap.gmail.com")
        imap.login(email_addr, email_pass)
        log.info("Connected to Gmail as {}".format(email_addr))
        return imap
    except Exception as e:
        log.error("Gmail login failed: {}".format(e))
        return None


def fetch_unread_emails(imap):
    """Fetch all unread emails from the inbox."""
    imap.select("INBOX")
    _, message_numbers = imap.search(None, "UNSEEN")

    emails = []
    for num in message_numbers[0].split():
        if not num:
            continue
        _, msg_data = imap.fetch(num, "(RFC822)")
        raw = msg_data[0][1]
        msg = email.message_from_bytes(raw)

        # Decode subject
        subject = ""
        if msg["Subject"]:
            decoded = decode_header(msg["Subject"])
            for part, charset in decoded:
                if isinstance(part, bytes):
                    subject += part.decode(charset or "utf-8", errors="replace")
                else:
                    subject += part

        # Decode sender
        sender = msg.get("From", "")

        # Get date
        date_str = msg.get("Date", "")

        # Extract plain text body
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        body = payload.decode(charset, errors="replace")
                    break
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                body = payload.decode(charset, errors="replace")

        emails.append({
            "message_num": num,
            "from": sender,
            "subject": subject,
            "date": date_str,
            "body": body.strip(),
        })

    log.info("Found {} unread email(s)".format(len(emails)))
    return emails


# =========================================================================
# Claude API - interpret emails
# =========================================================================

SYSTEM_PROMPT = """\
You are a calendar assistant for Beth, an active senior living in Rossmoor \
(Walnut Creek, CA). She takes fitness classes at Tice Creek Fitness Center \
(Zumba, UJAM, Aquacise with Bob, Posture Balance Core & Strength, Mat Yoga) \
and attends movies and concerts at Rossmoor.

You will receive forwarded emails about schedule changes, new appointments, \
cancellations, or other calendar updates. Extract structured calendar \
actions from each email.

IMPORTANT RULES:
- The current year is {year}.
- If a day of the week is mentioned without a date, calculate the next \
  occurrence of that day.
- For class cancellations, output a "cancel" action.
- For time changes, output a "modify" action.
- For new appointments (doctor, dentist, personal), output an "add" action.
- For anything you can't confidently parse, output an "unknown" action \
  with a description.
- All times are Pacific time.

Respond with ONLY a JSON array of actions. Each action is an object with:
{{
  "action": "add" | "cancel" | "modify" | "unknown",
  "title": "Event title",
  "date": "YYYY-MM-DD",
  "start_time": "HH:MM" (24h format),
  "end_time": "HH:MM" (24h format, optional),
  "location": "Location if mentioned",
  "notes": "Any relevant details (sub instructor, reason, etc.)",
  "original_class": "Name of original class if this modifies one",
  "source_email": "Brief description of who sent this"
}}

If the email contains no calendar-relevant information, return an empty \
array: []
"""


def interpret_email_with_claude(email_data):
    """Send email to Claude API and get structured calendar actions."""
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        log.error("ANTHROPIC_API_KEY not set")
        return []

    client = anthropic.Anthropic(api_key=api_key)

    today = datetime.now()
    prompt = (
        "Today is {today}.\n\n"
        "Email from: {sender}\n"
        "Subject: {subject}\n"
        "Date sent: {date}\n\n"
        "Body:\n{body}"
    ).format(
        today=today.strftime("%A, %B %d, %Y"),
        sender=email_data["from"],
        subject=email_data["subject"],
        date=email_data["date"],
        body=email_data["body"],
    )

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            system=SYSTEM_PROMPT.format(year=today.year),
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text.strip()

        # Extract JSON from response (handle markdown code blocks)
        if "```" in text:
            import re
            json_match = re.search(r'```(?:json)?\s*(.*?)```', text, re.DOTALL)
            if json_match:
                text = json_match.group(1).strip()

        actions = json.loads(text)
        if not isinstance(actions, list):
            actions = [actions]

        log.info("  Claude extracted {} action(s)".format(len(actions)))
        return actions

    except json.JSONDecodeError as e:
        log.warning("  Failed to parse Claude response as JSON: {}".format(e))
        log.debug("  Response was: {}".format(text[:500]))
        return []
    except Exception as e:
        log.error("  Claude API error: {}".format(e))
        return []


# =========================================================================
# Manual events persistence
# =========================================================================

def load_manual_events():
    """Load existing manual events from JSON file."""
    if not MANUAL_EVENTS_FILE.exists():
        return []
    try:
        with open(MANUAL_EVENTS_FILE) as f:
            events = json.load(f)
        return events if isinstance(events, list) else []
    except (json.JSONDecodeError, IOError):
        return []


def save_manual_events(events):
    """Save manual events to JSON file."""
    with open(MANUAL_EVENTS_FILE, "w") as f:
        json.dump(events, f, indent=2, default=str)
    log.info("Saved {} manual event(s) to {}".format(
        len(events), MANUAL_EVENTS_FILE))


def expire_old_events(events):
    """Remove events older than EVENT_TTL_DAYS."""
    cutoff = (datetime.now() - timedelta(days=EVENT_TTL_DAYS)).strftime(
        "%Y-%m-%d")
    before = len(events)
    events = [e for e in events if e.get("date", "9999") >= cutoff]
    removed = before - len(events)
    if removed:
        log.info("Expired {} old event(s)".format(removed))
    return events


def apply_actions(existing_events, actions, email_data):
    """Apply parsed actions to the manual events list."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    for action in actions:
        act_type = action.get("action", "unknown")
        title = action.get("title", "Unknown Event")
        date = action.get("date", "")
        start_time = action.get("start_time", "")
        end_time = action.get("end_time", "")
        location = action.get("location", "")
        notes = action.get("notes", "")
        source = action.get("source_email", email_data.get("from", ""))

        if act_type == "unknown":
            log.warning("  Skipping unknown action: {}".format(
                action.get("notes", "no details")))
            continue

        # Generate a unique ID for this event
        uid_str = "{}-{}-{}".format(title, date, start_time)
        uid = hashlib.md5(uid_str.encode()).hexdigest()[:16]

        if act_type == "cancel":
            # Mark matching events as cancelled, or add a cancellation entry
            original = action.get("original_class", title)
            existing_events.append({
                "uid": uid,
                "type": "cancel",
                "original_class": original.lower(),
                "date": date,
                "notes": notes,
                "source": source,
                "created": now_str,
            })
            log.info("  CANCEL: {} on {}".format(original, date))

        elif act_type == "modify":
            original = action.get("original_class", title)
            existing_events.append({
                "uid": uid,
                "type": "modify",
                "original_class": original.lower(),
                "title": title,
                "date": date,
                "start_time": start_time,
                "end_time": end_time,
                "location": location,
                "notes": notes,
                "source": source,
                "created": now_str,
            })
            log.info("  MODIFY: {} on {} -> {} at {}".format(
                original, date, title, start_time))

        elif act_type == "add":
            existing_events.append({
                "uid": uid,
                "type": "add",
                "title": title,
                "date": date,
                "start_time": start_time,
                "end_time": end_time,
                "location": location,
                "notes": notes,
                "source": source,
                "created": now_str,
            })
            log.info("  ADD: {} on {} at {}".format(title, date, start_time))

    return existing_events


# =========================================================================
# Main
# =========================================================================

def main():
    log.info("=" * 60)
    log.info("Beth's Calendar - Email Handler")
    log.info("Mode: {}".format("DRY RUN" if DRY_RUN else "live"))
    log.info("=" * 60)

    # Connect to Gmail
    imap = connect_gmail()
    if not imap:
        log.warning("Could not connect to email - skipping email check")
        return

    try:
        emails = fetch_unread_emails(imap)
    finally:
        try:
            imap.close()
            imap.logout()
        except Exception:
            pass

    if not emails:
        log.info("No new emails to process")
        return

    # Load existing manual events
    events = load_manual_events()
    events = expire_old_events(events)

    # Process each email
    for em in emails:
        log.info("")
        log.info("Processing: \"{}\" from {}".format(
            em["subject"][:60], em["from"][:40]))

        actions = interpret_email_with_claude(em)

        if not actions:
            log.info("  No calendar actions found in this email")
            continue

        events = apply_actions(events, actions, em)

    # Save
    if not DRY_RUN:
        save_manual_events(events)
        log.info("")
        log.info("Done! {} total manual event(s) on file".format(len(events)))
    else:
        log.info("")
        log.info("[DRY RUN] Would save {} event(s) - not writing".format(
            len(events)))
        print(json.dumps(events, indent=2, default=str))


if __name__ == "__main__":
    main()
