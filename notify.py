#!/usr/bin/env python3
"""Failure notification module for Beth's calendar system.

Sends email alerts to Connor when any workflow fails or detects anomalies.
Uses the existing bethcalendarupdate@gmail.com account via SMTP.

Usage:
    # As a module
    from notify import send_alert, send_summary

    send_alert("Scraper failed", "Could not load Tice Creek website")
    send_summary("Auto-book", "Booked 3 classes, 1 waitlisted")

    # From command line (used by GitHub Actions failure steps)
    python notify.py --alert "Workflow: sync" "Scraper failed with exit code 1"
    python notify.py --summary "Workflow: auto-book" "Booked 3 classes"
"""

import logging
import os
import smtplib
import sys
from datetime import datetime
from email.mime.text import MIMEText

log = logging.getLogger(__name__)

ALERT_RECIPIENT = os.environ.get("ALERT_EMAIL", "connordy@gmail.com")
SENDER_EMAIL = os.environ.get(
    "CALENDAR_EMAIL", "bethcalendarupdate@gmail.com")
SENDER_PASSWORD = os.environ.get("CALENDAR_EMAIL_PASSWORD", "")

GITHUB_RUN_URL = (
    "https://github.com/{}/actions/runs/{}".format(
        os.environ.get("GITHUB_REPOSITORY", "connordy8/tice-creek-calendar"),
        os.environ.get("GITHUB_RUN_ID", ""),
    )
    if os.environ.get("GITHUB_RUN_ID")
    else None
)


def send_email(subject, body, recipient=None):
    """Send an email via Gmail SMTP."""
    recipient = recipient or ALERT_RECIPIENT

    if not SENDER_PASSWORD:
        log.warning("CALENDAR_EMAIL_PASSWORD not set — cannot send alert")
        return False

    msg = MIMEText(body, "plain")
    msg["Subject"] = subject
    msg["From"] = "Beth Calendar Bot <{}>".format(SENDER_EMAIL)
    msg["To"] = recipient

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.send_message(msg)
        log.info("Alert email sent to {}".format(recipient))
        return True
    except Exception as e:
        log.error("Failed to send alert email: {}".format(e))
        return False


def send_alert(workflow_name, error_details, extra_context=""):
    """Send a failure alert email."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M PT")

    body_parts = [
        "ALERT: Beth's Calendar — {} failed".format(workflow_name),
        "",
        "Time: {}".format(now),
        "Error: {}".format(error_details),
    ]

    if extra_context:
        body_parts.extend(["", "Context:", extra_context])

    if GITHUB_RUN_URL:
        body_parts.extend([
            "",
            "View logs: {}".format(GITHUB_RUN_URL),
        ])

    body_parts.extend([
        "",
        "---",
        "This alert was sent by Beth's calendar automation system.",
        "If workflows keep failing, check GitHub Actions secrets and "
        "external service status (Mindbody, Rossmoor website).",
    ])

    subject = "[Beth Calendar] {} FAILED — {}".format(
        workflow_name, now)
    return send_email(subject, "\n".join(body_parts))


def send_summary(workflow_name, summary_text):
    """Send a success summary email (for important operations)."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M PT")

    body = (
        "Beth's Calendar — {} completed\n\n"
        "Time: {}\n"
        "Summary: {}\n"
    ).format(workflow_name, now, summary_text)

    if GITHUB_RUN_URL:
        body += "\nView logs: {}\n".format(GITHUB_RUN_URL)

    subject = "[Beth Calendar] {} — {}".format(workflow_name, now)
    return send_email(subject, body)


def main():
    """CLI entry point for GitHub Actions steps."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    if len(sys.argv) < 3:
        print("Usage: python notify.py --alert|--summary <title> <message>")
        sys.exit(1)

    mode = sys.argv[1]
    title = sys.argv[2]
    message = sys.argv[3] if len(sys.argv) > 3 else "No details provided"

    if mode == "--alert":
        ok = send_alert(title, message)
    elif mode == "--summary":
        ok = send_summary(title, message)
    else:
        print("Unknown mode: {} (use --alert or --summary)".format(mode))
        sys.exit(1)

    if not ok:
        log.error("Failed to send notification")
        sys.exit(1)


if __name__ == "__main__":
    main()
