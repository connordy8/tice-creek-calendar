"""Call Beth 15 minutes before fitness classes and appointments.

Uses Twilio to make an outbound phone call with a friendly TTS
reminder. Skips movies and concerts (those don't need a call).

Runs every 5 minutes via GitHub Actions. Uses Google Calendar
extended properties to track which events already got a call,
so Beth never gets duplicate reminders.
"""

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

log = logging.getLogger("phone_reminder")

# Color IDs that should NOT get a phone call:
#   "9" = Blueberry (movies)
#   "6" = Tangerine (concerts)
SKIP_COLOR_IDS = {"9", "6"}

# How far ahead to look for upcoming events (minutes)
REMINDER_WINDOW_MIN = 10  # Don't call if less than 10 min away
REMINDER_WINDOW_MAX = 20  # Don't call if more than 20 min away

# Extended property key used to mark events we've already called about
REMINDED_KEY = "bethReminded"


def get_calendar_service():
    """Build Google Calendar API service."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_KEY")
    if not creds_json:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_KEY not set")

    creds_info = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(
        creds_info,
        scopes=["https://www.googleapis.com/auth/calendar"])
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def get_upcoming_events(service, calendar_id):
    """Find events starting in the next 10-20 minutes."""
    now = datetime.now(timezone.utc)
    window_start = now + timedelta(minutes=REMINDER_WINDOW_MIN)
    window_end = now + timedelta(minutes=REMINDER_WINDOW_MAX)

    resp = service.events().list(
        calendarId=calendar_id,
        timeMin=window_start.isoformat(),
        timeMax=window_end.isoformat(),
        singleEvents=True,
        orderBy="startTime",
        maxResults=10,
    ).execute()

    return resp.get("items", [])


def should_call(event):
    """Decide whether this event should trigger a phone call."""
    # Skip cancelled events
    if event.get("status") == "cancelled":
        return False

    # Skip movies and concerts (by color)
    color_id = event.get("colorId", "")
    if color_id in SKIP_COLOR_IDS:
        log.info("  Skipping (movie/concert color): {}".format(
            event.get("summary", "")))
        return False

    # Skip if we already called about this event
    ext_props = event.get("extendedProperties", {})
    private = ext_props.get("private", {})
    if private.get(REMINDED_KEY):
        log.info("  Already reminded: {}".format(
            event.get("summary", "")))
        return False

    # Skip all-day events (no specific time)
    start = event.get("start", {})
    if "dateTime" not in start:
        return False

    return True


def extract_class_info(event):
    """Pull out the class name, time, and location for TTS."""
    summary = event.get("summary", "Unknown class")

    # Strip emoji prefixes and status indicators
    clean_name = summary
    for prefix in ["\u2705", "\u23f3", "\U0001f3cb\ufe0f",
                   "\U0001f3ca", "\U0001f3ac", "\U0001f3b5"]:
        clean_name = clean_name.replace(prefix, "")
    # Strip "(waitlist)", "(drop-in)", etc.
    for suffix in ["(waitlist)", "(drop-in)", "(club)"]:
        clean_name = clean_name.replace(suffix, "")
    clean_name = clean_name.strip()

    # Parse start time for a friendly announcement
    start_str = event.get("start", {}).get("dateTime", "")
    try:
        # Parse ISO datetime with timezone
        start_dt = datetime.fromisoformat(start_str)
        time_str = start_dt.strftime("%-I:%M %p")
    except (ValueError, AttributeError):
        time_str = "soon"

    location = event.get("location", "Tice Creek Fitness Center")
    # Shorten the location for TTS
    if "Tice Creek" in location:
        location = "Tice Creek Fitness Center"

    # Try to extract room/studio from description or summary
    description = event.get("description", "")
    room = ""
    for studio in ["Aerobics Studio", "Serenity Studio",
                    "Gymnasium", "Pool", "basketball court"]:
        if studio.lower() in description.lower() or \
                studio.lower() in summary.lower():
            room = studio
            break

    return {
        "name": clean_name,
        "time": time_str,
        "location": location,
        "room": room,
    }


def make_reminder_call(event_info):
    """Place a Twilio call to Beth with a TTS reminder."""
    from twilio.rest import Client

    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    from_number = os.environ.get("TWILIO_PHONE_NUMBER")
    to_number = os.environ.get("BETH_PHONE_NUMBER")

    if not all([account_sid, auth_token, from_number, to_number]):
        log.warning("Twilio credentials not fully configured — skipping call")
        log.info("Would have called: {}".format(event_info))
        return False

    # Build the TTS message
    name = event_info["name"]
    time_str = event_info["time"]
    room = event_info["room"]

    message = (
        "Hi Beth! This is a friendly reminder that your "
        "{name} class starts in 15 minutes at {time}."
    ).format(name=name, time=time_str)

    if room:
        message += " It's in the {}.".format(room)

    message += " Have a wonderful time!"

    log.info("Calling Beth: {}".format(message))

    # TwiML for the call — speaks the message, then hangs up
    twiml = (
        '<Response>'
        '<Say voice="Polly.Joanna" language="en-US">{msg}</Say>'
        '<Pause length="1"/>'
        '<Say voice="Polly.Joanna" language="en-US">'
        'If you need to cancel, ask your caretaker to update the calendar.'
        '</Say>'
        '</Response>'
    ).format(msg=message)

    client = Client(account_sid, auth_token)
    call = client.calls.create(
        twiml=twiml,
        to=to_number,
        from_=from_number,
    )

    log.info("Call placed! SID: {}".format(call.sid))
    return True


def mark_as_reminded(service, calendar_id, event_id):
    """Set an extended property on the event so we don't call again."""
    try:
        service.events().patch(
            calendarId=calendar_id,
            eventId=event_id,
            body={
                "extendedProperties": {
                    "private": {
                        REMINDED_KEY: datetime.now(
                            timezone.utc).isoformat(),
                    }
                }
            },
        ).execute()
        log.info("  Marked event as reminded: {}".format(event_id[:16]))
    except Exception as e:
        log.warning("  Failed to mark event: {}".format(e))


def run():
    """Main entry point."""
    calendar_id = os.environ.get("GOOGLE_CALENDAR_ID")
    if not calendar_id:
        log.error("GOOGLE_CALENDAR_ID not set")
        sys.exit(1)

    service = get_calendar_service()

    log.info("Checking for events starting in {}-{} minutes...".format(
        REMINDER_WINDOW_MIN, REMINDER_WINDOW_MAX))

    events = get_upcoming_events(service, calendar_id)
    log.info("Found {} events in the reminder window".format(len(events)))

    calls_made = 0
    for event in events:
        summary = event.get("summary", "")
        log.info("Checking: {}".format(summary))

        if not should_call(event):
            continue

        info = extract_class_info(event)
        log.info("  Calling about: {} at {}".format(
            info["name"], info["time"]))

        success = make_reminder_call(info)
        if success:
            mark_as_reminded(service, calendar_id, event.get("id", ""))
            calls_made += 1
        else:
            # Still mark it even if Twilio isn't configured (dry run)
            # so we don't spam logs on every run
            mark_as_reminded(service, calendar_id, event.get("id", ""))

    log.info("Done — {} reminder call(s) placed".format(calls_made))


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    run()
