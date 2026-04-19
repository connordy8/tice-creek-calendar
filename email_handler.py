#!/usr/bin/env python3
"""
Email-based calendar updates for Beth's Tice Creek calendar.

Checks a dedicated Gmail inbox (bethcalendarupdate@gmail.com) for emails
from family, helpers, or class coordinators. Uses a two-stage Claude
pipeline (classifier + extractor) to pull out ONLY emails that contain
specific, actionable calendar changes.

Persists changes to manual_events.json which the main scraper merges
into the ICS output.

Usage:
    python3 email_handler.py              # Live: process unread emails
    python3 email_handler.py --dry-run    # Process unread, don't save
    python3 email_handler.py --audit 20   # Re-scan last 20 emails (read
                                           # or unread), print decision
                                           # table, don't modify state.
"""

import imaplib
import email
import json
import os
import sys
import re
import logging
import hashlib
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from email.header import decode_header
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# -------------------------------------------------------------------------
# Modes
# -------------------------------------------------------------------------
DRY_RUN = "--dry-run" in sys.argv

AUDIT_COUNT = 0
if "--audit" in sys.argv:
    try:
        idx = sys.argv.index("--audit")
        AUDIT_COUNT = int(sys.argv[idx + 1])
    except (ValueError, IndexError):
        AUDIT_COUNT = 20
AUDIT_MODE = AUDIT_COUNT > 0

# -------------------------------------------------------------------------
# Config
# -------------------------------------------------------------------------
MANUAL_EVENTS_FILE = Path("manual_events.json")
AUDIT_LOG_FILE = Path("email_audit.log")

EVENT_TTL_DAYS = 90

# Confidence thresholds
CONFIDENCE_APPLY = 0.85   # >= this: auto-apply
CONFIDENCE_REVIEW = 0.60  # >= this and < APPLY: alert Connor, don't apply
# < REVIEW: drop silently

# How far in the future an event can be (anything further is suspicious)
MAX_DAYS_OUT = 180

# Known class / event names for fuzzy matching cancel/modify targets
# (pulled from Beth's include_classes preferences; add more as needed)
KNOWN_CLASS_NAMES = [
    "zumba", "zumba club", "zumba fitness",
    "ujam", "ujam and stretch",
    "aquacise", "deep water aerobics",
    "posture", "posture balance core and strength",
    "mat yoga",
    "foreverfit", "forever fit",
    "tai chi",
    "functional strength", "functional fitness",
    "strength and stretch",
    "let's stretch", "lets stretch",
    "memory loss meetup",
    "democrats",
]

# Claude models
# Classifier can be cheap; extractor needs solid reasoning.
CLASSIFIER_MODEL = os.environ.get(
    "CLASSIFIER_MODEL", "claude-sonnet-4-20250514")
EXTRACTOR_MODEL = os.environ.get(
    "EXTRACTOR_MODEL", "claude-sonnet-4-20250514")


# =========================================================================
# Gmail IMAP
# =========================================================================

def connect_gmail(max_retries=3):
    """Connect to Gmail via IMAP using App Password. Retries on failure."""
    email_addr = os.environ.get("CALENDAR_EMAIL", "bethcalendarupdate@gmail.com")
    email_pass = os.environ.get("CALENDAR_EMAIL_PASSWORD", "")

    if not email_pass:
        log.error("CALENDAR_EMAIL_PASSWORD not set")
        return None

    import time as _time
    for attempt in range(1, max_retries + 1):
        try:
            imap = imaplib.IMAP4_SSL("imap.gmail.com")
            imap.login(email_addr, email_pass)
            log.info("Connected to Gmail as {}".format(email_addr))
            return imap
        except Exception as e:
            log.warning("Gmail login attempt {}/{} failed: {}".format(
                attempt, max_retries, e))
            if attempt < max_retries:
                _time.sleep(5 * attempt)
    log.error("Gmail login failed after {} attempts".format(max_retries))
    return None


def _parse_msg(num, raw):
    """Turn a raw RFC822 blob into our email dict."""
    msg = email.message_from_bytes(raw)

    # Subject
    subject = ""
    if msg["Subject"]:
        decoded = decode_header(msg["Subject"])
        for part, charset in decoded:
            if isinstance(part, bytes):
                subject += part.decode(charset or "utf-8", errors="replace")
            else:
                subject += part

    sender = msg.get("From", "")
    date_str = msg.get("Date", "")

    # Plain text body (prefer text/plain, fall back to text/html stripped)
    body = ""
    html_body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain" and not body:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    body = payload.decode(charset, errors="replace")
            elif ctype == "text/html" and not html_body:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    html_body = payload.decode(charset, errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            body = payload.decode(charset, errors="replace")

    if not body and html_body:
        # Cheap HTML strip
        body = re.sub(r"<[^>]+>", " ", html_body)
        body = re.sub(r"\s+", " ", body).strip()

    # Truncate very long bodies (common in forwarded newsletters)
    if len(body) > 8000:
        body = body[:8000] + "\n…[truncated]"

    return {
        "message_num": num,
        "from": sender,
        "subject": subject,
        "date": date_str,
        "body": body.strip(),
    }


def fetch_unread_emails(imap):
    """Fetch all unread emails from the inbox (marks them read)."""
    imap.select("INBOX")
    _, message_numbers = imap.search(None, "UNSEEN")
    emails = []
    for num in message_numbers[0].split():
        if not num:
            continue
        _, msg_data = imap.fetch(num, "(RFC822)")
        emails.append(_parse_msg(num, msg_data[0][1]))
    log.info("Found {} unread email(s)".format(len(emails)))
    return emails


def fetch_recent_emails_peek(imap, count):
    """Fetch the last `count` emails WITHOUT marking them read.

    Used by --audit mode to test the pipeline on already-processed mail.
    Uses BODY.PEEK[] which does not set the \\Seen flag.
    """
    imap.select("INBOX")
    _, message_numbers = imap.search(None, "ALL")
    all_nums = message_numbers[0].split()
    recent = all_nums[-count:] if len(all_nums) > count else all_nums
    emails = []
    for num in recent:
        if not num:
            continue
        _, msg_data = imap.fetch(num, "(BODY.PEEK[])")
        if msg_data and msg_data[0]:
            emails.append(_parse_msg(num, msg_data[0][1]))
    log.info("Peeked at last {} email(s)".format(len(emails)))
    return emails


# =========================================================================
# Claude Stage 1: classifier
# =========================================================================

CLASSIFIER_SYSTEM = """\
You are a strict gatekeeper deciding whether a forwarded email contains a \
SPECIFIC, ACTIONABLE calendar change for Beth (a Rossmoor senior who takes \
fitness classes).

Answer YES only if the email clearly states one of these, with a specific \
date or unambiguous day-of-week reference:
  - A class is CANCELLED on a specific date
  - A class is MOVED to a different time/room on a specific date
  - A substitute instructor is teaching a specific class on a specific date
  - A new appointment/event is being scheduled (doctor, lunch, visit, etc.)
  - An existing appointment is being rescheduled or cancelled

Answer NO for anything else, including:
  - General announcements, newsletters, marketing, holiday wishes
  - "Thank you" notes, pep talks, motivational messages
  - Recurring weekly schedule reminders with no change
  - Photos, well-wishes, condolences
  - Instructor personal updates that don't change the schedule
  - Class descriptions or promotional content
  - Vague statements like "hope to see you soon" or "class is going great"
  - Emails that merely MENTION a date but don't schedule/change anything

When in doubt, answer NO. False positives are worse than false negatives — \
Beth forwards lots of unrelated emails and we must not invent calendar \
entries from them.

Respond with ONLY a JSON object:
{
  "relevant": true | false,
  "reason": "<one sentence explaining your decision>"
}
"""


def classify_email(client, email_data):
    """Stage 1: is this email actionable for the calendar?"""
    prompt = (
        "Email from: {sender}\n"
        "Subject: {subject}\n\n"
        "Body:\n{body}"
    ).format(
        sender=email_data["from"],
        subject=email_data["subject"],
        body=email_data["body"][:4000],
    )

    try:
        resp = client.messages.create(
            model=CLASSIFIER_MODEL,
            max_tokens=200,
            system=CLASSIFIER_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        # Strip code fences
        if "```" in text:
            m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
            if m:
                text = m.group(1).strip()
        data = json.loads(text)
        return {
            "relevant": bool(data.get("relevant", False)),
            "reason": str(data.get("reason", ""))[:300],
        }
    except Exception as e:
        log.warning("  Classifier error (treating as NOT relevant): {}"
                    .format(e))
        return {"relevant": False, "reason": "classifier error: {}".format(e)}


# =========================================================================
# Claude Stage 2: extractor
# =========================================================================

EXTRACTOR_SYSTEM = """\
You are a calendar assistant for Beth, an active senior at Rossmoor \
(Walnut Creek, CA). She takes fitness classes at Tice Creek Fitness Center \
(Zumba, UJAM, Aquacise with Bob, Posture Balance Core & Strength, Mat Yoga, \
Tai Chi, Let's Stretch, Strength and Stretch, Functional Fitness, \
Deep Water Aerobics) and attends Rossmoor movies and concerts.

A separate classifier has already determined this email likely contains a \
calendar action. Your job is to extract the PRECISE action(s).

CRITICAL RULES:
- The current year is {year}. Today is {today}.
- All times are Pacific time.
- If a day of the week is mentioned without a date, calculate the NEXT \
  occurrence of that day from today.
- For class cancellations, output "cancel".
- For time/room/instructor changes, output "modify".
- For new appointments (doctor, dentist, lunch, visit), output "add".
- Give each action a CONFIDENCE score 0.0-1.0 reflecting how certain you \
  are the email unambiguously requests this action.
    * 0.9+: explicit date, explicit time, explicit event, no ambiguity
    * 0.7-0.9: one ambiguity (e.g. date inferred from day-of-week)
    * 0.5-0.7: multiple ambiguities or some inference required
    * <0.5: you're really not sure — output "unknown" instead
- If the email is ambiguous or multiple dates are possible, prefer "unknown" \
  over guessing.
- Do NOT hallucinate. If date or time are missing, set confidence low or \
  use "unknown".

Respond with ONLY a JSON array. Each element:
{{
  "action": "add" | "cancel" | "modify" | "unknown",
  "title": "Event title",
  "date": "YYYY-MM-DD",
  "start_time": "HH:MM" (24h),
  "end_time": "HH:MM" (24h, optional),
  "location": "Location if mentioned",
  "notes": "Relevant details (sub instructor, reason, etc.)",
  "original_class": "Name of original class if this is a cancel/modify",
  "source_email": "Brief description of who sent this",
  "confidence": 0.0-1.0,
  "reasoning": "One sentence: what in the email supports this action"
}}

If despite the classifier's verdict you find no extractable action, return \
an empty array: []
"""


def extract_actions(client, email_data):
    """Stage 2: pull structured actions out of a relevant email."""
    today = datetime.now()
    prompt = (
        "Email from: {sender}\n"
        "Subject: {subject}\n"
        "Date sent: {date}\n\n"
        "Body:\n{body}"
    ).format(
        sender=email_data["from"],
        subject=email_data["subject"],
        date=email_data["date"],
        body=email_data["body"],
    )

    try:
        resp = client.messages.create(
            model=EXTRACTOR_MODEL,
            max_tokens=1500,
            system=EXTRACTOR_SYSTEM.format(
                year=today.year,
                today=today.strftime("%A, %B %d, %Y"),
            ),
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        if "```" in text:
            m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
            if m:
                text = m.group(1).strip()
        actions = json.loads(text)
        if not isinstance(actions, list):
            actions = [actions]
        return actions
    except json.JSONDecodeError as e:
        log.warning("  Extractor: JSON parse failed: {}".format(e))
        return []
    except Exception as e:
        log.error("  Extractor error: {}".format(e))
        return []


# =========================================================================
# Validation gates
# =========================================================================

def _fuzzy_match_class(name):
    """Return True if `name` reasonably matches a known class."""
    if not name:
        return False
    n = name.lower().strip()
    for known in KNOWN_CLASS_NAMES:
        if known in n or n in known:
            return True
        if SequenceMatcher(None, n, known).ratio() >= 0.75:
            return True
    return False


def validate_action(action):
    """Return (ok, reason) — run hard sanity checks on one action.

    ok=True means the action is safe to apply.
    ok=False with reason means drop (log the reason).
    """
    act = action.get("action", "")
    if act not in ("add", "cancel", "modify", "unknown"):
        return False, "unknown action type: {}".format(act)

    if act == "unknown":
        # Unknowns never auto-apply; they flow to the review alert.
        return False, "action=unknown"

    # Confidence must be present
    try:
        conf = float(action.get("confidence", 0))
    except (TypeError, ValueError):
        conf = 0.0
    if conf < CONFIDENCE_APPLY:
        return False, "confidence {:.2f} below apply threshold".format(conf)

    # Date must parse, be within window
    date_val = action.get("date", "")
    if not date_val:
        return False, "no date"
    try:
        d = datetime.strptime(date_val, "%Y-%m-%d")
    except ValueError:
        return False, "invalid date format: {}".format(date_val)

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    if d < today - timedelta(days=1):
        return False, "date is in the past: {}".format(date_val)
    if d > today + timedelta(days=MAX_DAYS_OUT):
        return False, "date too far in future: {}".format(date_val)

    # Time must parse if present
    time_val = action.get("start_time", "")
    if time_val:
        try:
            datetime.strptime(time_val, "%H:%M")
        except ValueError:
            return False, "invalid time format: {}".format(time_val)

    # For cancel/modify, original_class must match a known class
    if act in ("cancel", "modify"):
        original = action.get("original_class", "") or action.get("title", "")
        if not _fuzzy_match_class(original):
            return False, ("{} targets unknown class: '{}'"
                           .format(act, original))

    # For add, require a title that isn't trivially generic
    if act == "add":
        title = action.get("title", "").strip()
        if len(title) < 3:
            return False, "title too short: '{}'".format(title)
        generic = {"class", "event", "reminder", "appointment", "meeting"}
        if title.lower() in generic:
            return False, "title too generic: '{}'".format(title)

    return True, "ok"


# =========================================================================
# Persistence
# =========================================================================

def load_manual_events():
    if not MANUAL_EVENTS_FILE.exists():
        return []
    try:
        with open(MANUAL_EVENTS_FILE) as f:
            events = json.load(f)
        return events if isinstance(events, list) else []
    except (json.JSONDecodeError, IOError):
        return []


def save_manual_events(events):
    with open(MANUAL_EVENTS_FILE, "w") as f:
        json.dump(events, f, indent=2, default=str)
    log.info("Saved {} manual event(s) to {}".format(
        len(events), MANUAL_EVENTS_FILE))


def expire_old_events(events):
    cutoff = (datetime.now() - timedelta(days=EVENT_TTL_DAYS)).strftime(
        "%Y-%m-%d")
    before = len(events)
    events = [e for e in events if e.get("date", "9999") >= cutoff]
    removed = before - len(events)
    if removed:
        log.info("Expired {} old event(s)".format(removed))
    return events


def is_duplicate(existing_events, new_event):
    """True if an equivalent event is already in the list."""
    for e in existing_events:
        if (e.get("type") == new_event.get("type")
                and e.get("title", "").lower() ==
                    new_event.get("title", "").lower()
                and e.get("date") == new_event.get("date")
                and e.get("start_time") == new_event.get("start_time")):
            return True
    return False


def write_audit_row(row):
    """Append a one-line JSON audit record."""
    try:
        with open(AUDIT_LOG_FILE, "a") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except Exception as e:
        log.warning("Could not write audit log: {}".format(e))


# =========================================================================
# Alerting
# =========================================================================

def _alert(title, message, extra=""):
    try:
        from notify import send_alert
        send_alert(title, message, extra_context=extra)
    except Exception as e:
        log.warning("Notify failed: {}".format(e))


# =========================================================================
# Apply
# =========================================================================

def apply_actions(existing_events, actions, email_data, decision_rows):
    """Run each action through gates and apply if it passes.

    decision_rows: list we append per-action audit dicts to.
    """
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    applied = 0

    for action in actions:
        act = action.get("action", "unknown")
        conf = float(action.get("confidence", 0) or 0)
        title = action.get("title", "Unknown Event")
        date = action.get("date", "")
        start_time = action.get("start_time", "")

        ok, reason = validate_action(action)

        row = {
            "email_from": email_data.get("from", ""),
            "email_subject": email_data.get("subject", ""),
            "action": act,
            "title": title,
            "date": date,
            "start_time": start_time,
            "confidence": conf,
            "passed_gates": ok,
            "gate_reason": reason,
            "decision": None,
        }

        if not ok:
            # Middle-confidence review cases or unknowns → alert Connor
            if (act == "unknown"
                    or (CONFIDENCE_REVIEW <= conf < CONFIDENCE_APPLY)):
                row["decision"] = "review-alert"
                if not AUDIT_MODE:
                    _alert(
                        "Email Handler Review",
                        "An email may contain a calendar change but wasn't \
confident enough to auto-apply.",
                        extra=(
                            "From: {}\nSubject: {}\nAction: {}\n"
                            "Title: {}\nDate: {} {}\nConfidence: {:.2f}\n"
                            "Reason dropped: {}\nExtractor reasoning: {}"
                        ).format(
                            email_data.get("from", "?"),
                            email_data.get("subject", "?"),
                            act, title, date, start_time, conf, reason,
                            action.get("reasoning", ""),
                        ),
                    )
                log.warning("  REVIEW (conf={:.2f}): {} — {}"
                            .format(conf, title, reason))
            else:
                row["decision"] = "dropped"
                log.info("  dropped ({:.2f}): {} — {}"
                         .format(conf, title, reason))
            decision_rows.append(row)
            continue

        # Passed gates → build event and check dedup
        uid = hashlib.md5(
            "{}-{}-{}".format(title, date, start_time).encode()
        ).hexdigest()[:16]

        new_event = {
            "uid": uid,
            "type": act,
            "title": title,
            "date": date,
            "start_time": start_time,
            "end_time": action.get("end_time", ""),
            "location": action.get("location", ""),
            "notes": action.get("notes", ""),
            "source": action.get("source_email",
                                 email_data.get("from", "")),
            "confidence": conf,
            "created": now_str,
        }
        if act in ("cancel", "modify"):
            new_event["original_class"] = (
                action.get("original_class", title).lower())

        if is_duplicate(existing_events, new_event):
            row["decision"] = "duplicate-skip"
            log.info("  skipped duplicate: {} on {}".format(title, date))
            decision_rows.append(row)
            continue

        row["decision"] = "applied"
        decision_rows.append(row)
        existing_events.append(new_event)
        applied += 1
        log.info("  APPLIED {} (conf={:.2f}): {} on {} at {}"
                 .format(act.upper(), conf, title, date, start_time))

    return existing_events, applied


# =========================================================================
# Main
# =========================================================================

def _format_audit_table(rows):
    """Pretty-print a decision table for --audit mode."""
    if not rows:
        return "(no rows)"
    out = []
    out.append("{:<40} {:<8} {:<30} {:<10} {:>5} {:<16} {}".format(
        "From", "Action", "Title", "Date", "Conf", "Decision", "Reason"))
    out.append("-" * 140)
    for r in rows:
        out.append("{:<40} {:<8} {:<30} {:<10} {:>5.2f} {:<16} {}".format(
            (r.get("email_from") or "")[:38],
            r.get("action") or "",
            (r.get("title") or "")[:28],
            r.get("date") or "",
            r.get("confidence") or 0,
            r.get("decision") or "",
            (r.get("gate_reason") or "")[:60],
        ))
    return "\n".join(out)


def main():
    log.info("=" * 60)
    log.info("Beth's Calendar - Email Handler")
    if AUDIT_MODE:
        log.info("Mode: AUDIT (last {} emails, no state changes)"
                 .format(AUDIT_COUNT))
    elif DRY_RUN:
        log.info("Mode: DRY RUN")
    else:
        log.info("Mode: live")
    log.info("=" * 60)

    import anthropic
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        log.error("ANTHROPIC_API_KEY not set")
        return
    client = anthropic.Anthropic(api_key=api_key)

    imap = connect_gmail()
    if not imap:
        log.warning("Could not connect to email - skipping email check")
        return

    try:
        if AUDIT_MODE:
            emails = fetch_recent_emails_peek(imap, AUDIT_COUNT)
        else:
            emails = fetch_unread_emails(imap)
    finally:
        try:
            imap.close()
            imap.logout()
        except Exception:
            pass

    if not emails:
        log.info("No emails to process")
        return

    events = load_manual_events()
    events = expire_old_events(events)
    pre_count = len(events)

    decision_rows = []
    applied_total = 0
    classified_relevant = 0

    for em in emails:
        log.info("")
        log.info("=> {} | {}".format(
            em["from"][:50], em["subject"][:60]))

        # Stage 1: classify
        verdict = classify_email(client, em)
        log.info("   classifier: relevant={}  reason={}".format(
            verdict["relevant"], verdict["reason"]))

        if not verdict["relevant"]:
            decision_rows.append({
                "email_from": em.get("from", ""),
                "email_subject": em.get("subject", ""),
                "action": "-",
                "title": "-",
                "date": "-",
                "start_time": "-",
                "confidence": 0.0,
                "passed_gates": False,
                "gate_reason": verdict["reason"],
                "decision": "filtered-classifier",
            })
            continue

        classified_relevant += 1

        # Stage 2: extract
        actions = extract_actions(client, em)
        if not actions:
            log.info("   extractor returned no actions")
            decision_rows.append({
                "email_from": em.get("from", ""),
                "email_subject": em.get("subject", ""),
                "action": "-",
                "title": "-",
                "date": "-",
                "start_time": "-",
                "confidence": 0.0,
                "passed_gates": False,
                "gate_reason": "classifier-yes but extractor-empty",
                "decision": "dropped",
            })
            continue

        events, applied = apply_actions(events, actions, em, decision_rows)
        applied_total += applied

    # Write audit log & summary
    for r in decision_rows:
        write_audit_row({"ts": datetime.now().isoformat(), **r})

    log.info("")
    log.info("=" * 60)
    log.info("SUMMARY")
    log.info("  emails processed:      {}".format(len(emails)))
    log.info("  classifier said yes:   {}".format(classified_relevant))
    log.info("  actions applied:       {}".format(applied_total))
    log.info("  total manual events:   {} (was {})".format(
        len(events), pre_count))
    log.info("=" * 60)

    if AUDIT_MODE:
        log.info("")
        log.info("DECISION TABLE:")
        print(_format_audit_table(decision_rows))
        log.info("")
        log.info("[AUDIT] state NOT modified.")
        return

    if DRY_RUN:
        log.info("[DRY RUN] state NOT modified.")
        print(json.dumps(decision_rows, indent=2, default=str))
        return

    save_manual_events(events)
    log.info("Done.")


if __name__ == "__main__":
    main()
