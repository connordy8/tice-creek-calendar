"""Google Calendar API sync for Beth's calendar.

Pushes fitness classes, movies, and concerts directly onto Beth's
Google Calendar with per-event color coding.

Google Calendar color IDs:
  1  Lavender       5  Banana      9  Blueberry
  2  Sage           6  Tangerine  10  Basil
  3  Grape          7  Peacock    11  Tomato
  4  Flamingo       8  Graphite
"""

import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timedelta

from google.oauth2 import service_account
from googleapiclient.discovery import build

log = logging.getLogger("gcal_sync")

# Color IDs for different event types
COLOR_FITNESS = "2"      # Sage (green)
COLOR_MOVIE = "9"        # Blueberry (blue/purple)
COLOR_CONCERT = "6"      # Tangerine (orange)

SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Prefix for event IDs we manage — lets us find/clean up our events
# Google Calendar IDs must use only lowercase a-v and digits 0-9
EVENT_ID_PREFIX = "be0ca1"


def get_calendar_service():
    """Authenticate and return a Google Calendar API service."""
    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_KEY")
    if not creds_json:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_KEY env var not set")

    creds_info = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(
        creds_info, scopes=SCOPES)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def make_event_id(prefix, unique_str):
    """Create a deterministic Google Calendar event ID.

    Google requires event IDs to be 5-1024 chars, lowercase a-v and 0-9.
    We use a hex hash (0-9, a-f) which is a valid subset.
    """
    raw = "{}-{}".format(prefix, unique_str)
    h = hashlib.md5(raw.encode()).hexdigest()
    return "{}{}".format(EVENT_ID_PREFIX, h)


def sync_to_google_calendar(classes, movies, concerts, config):
    """Push all events to Beth's Google Calendar.

    Uses deterministic event IDs so re-running is idempotent:
    - New events get created
    - Existing events get updated
    - Events we previously created that are no longer in the data get deleted
    """
    calendar_id = os.environ.get("GOOGLE_CALENDAR_ID", "primary")
    service = get_calendar_service()
    default_dur = config.get("default_class_duration_minutes", 45)
    movie_dur = config.get("movie_duration_minutes", 135)
    concert_dur = config.get("concert_duration_minutes", 120)
    early_start = config.get("early_start_minutes", 0)

    # Build fitness time ranges for conflict detection
    fitness_ranges = []
    for cls in (classes or []):
        try:
            fs = datetime.fromisoformat(cls["start_iso"])
            fd = cls.get("duration_minutes", default_dur)
            if fd <= 0:
                fd = default_dur
            fe = fs + timedelta(minutes=fd)
            fitness_ranges.append((fs, fe))
        except (ValueError, KeyError):
            pass

    def conflicts_with_fitness(evt_start, evt_end):
        for fs, fe in fitness_ranges:
            if evt_start < fe and evt_end > fs:
                return True
        return False

    # Collect all events we want to exist
    desired_events = {}  # event_id -> event body

    # --- Fitness classes ---
    # NOTE: Fitness classes are NO LONGER managed by the scraper.
    # The auto-booker (auto_book.py) is the sole source for fitness
    # events. It only adds classes Beth is actually enrolled in or
    # waitlisted for, with ✅/⏳ status indicators.
    # Any old scraper-created fitness events (prefix "be0ca1" with
    # fitness emoji) will be cleaned up by the deletion step below.
    log.info("Skipping {} fitness classes (managed by auto-booker)".format(
        len(classes or [])))

    # --- Movies ---
    for mov in (movies or []):
        start_iso = mov.get("start_iso", "")
        if not start_iso:
            continue
        try:
            start = datetime.fromisoformat(start_iso)
        except ValueError:
            continue

        end = start + timedelta(minutes=movie_dur)
        if conflicts_with_fitness(start, end):
            continue

        title = mov["title"]
        movie_year = mov.get("movie_year", "")
        display_name = "{} ({})".format(title, movie_year)

        desc_parts = []
        movie_desc = mov.get("description", "")
        if movie_desc:
            desc_parts.append(movie_desc)
        show_time = start.strftime("%I:%M %p").lstrip("0")
        desc_parts.append("Showtime: {} at Peacock Hall".format(show_time))
        desc_parts.append("Free admission")
        desc_parts.append(
            "Auto-synced from rossmoor.com recreation calendar")

        cal_start = start - timedelta(minutes=early_start)
        eid = make_event_id("movie", "{}-{}-{}".format(
            title, mov.get("date", ""), start_iso))

        from scraper import MOVIE_LOCATION
        desired_events[eid] = {
            "summary": "\U0001f3ac {}".format(display_name),
            "description": "\n".join(desc_parts),
            "location": MOVIE_LOCATION,
            "start": {
                "dateTime": cal_start.strftime("%Y-%m-%dT%H:%M:%S"),
                "timeZone": "America/Los_Angeles",
            },
            "end": {
                "dateTime": end.strftime("%Y-%m-%dT%H:%M:%S"),
                "timeZone": "America/Los_Angeles",
            },
            "colorId": COLOR_MOVIE,
        }

    # --- Concerts ---
    for evt in (concerts or []):
        start_iso = evt.get("start_iso", "")
        if not start_iso:
            continue
        try:
            start = datetime.fromisoformat(start_iso)
        except ValueError:
            continue

        end = start + timedelta(minutes=concert_dur)
        if conflicts_with_fitness(start, end):
            continue

        title = evt["title"]
        event_type = evt.get("event_type", "Concert")
        cost = evt.get("cost", "")
        loc_code = evt.get("location_code", "EC")

        from scraper import ROSSMOOR_LOCATIONS
        location = ROSSMOOR_LOCATIONS.get(
            loc_code, ROSSMOOR_LOCATIONS["EC"])

        if "Spotlight" in event_type:
            emoji = "\U0001f3b5"
            display_name = "Spotlight: {}".format(title)
        else:
            emoji = "\U0001f3b6"
            display_name = "Concert: {}".format(title)

        desc_parts = []
        show_time = start.strftime("%I:%M %p").lstrip("0")
        desc_parts.append("{} at {}".format(show_time, loc_code))
        if cost:
            desc_parts.append("Tickets: {}".format(cost))
        else:
            desc_parts.append("Free admission")
        desc_parts.append(
            "Tickets at Recreation Dept, Gateway, Mon-Fri 8am-4:30pm")
        desc_parts.append(
            "Auto-synced from rossmoor.com recreation calendar")

        cal_start = start - timedelta(minutes=early_start)
        eid = make_event_id("concert", "{}-{}-{}".format(
            title, evt.get("date", ""), start_iso))

        desired_events[eid] = {
            "summary": "{} {}".format(emoji, display_name),
            "description": "\n".join(desc_parts),
            "location": location,
            "start": {
                "dateTime": cal_start.strftime("%Y-%m-%dT%H:%M:%S"),
                "timeZone": "America/Los_Angeles",
            },
            "end": {
                "dateTime": end.strftime("%Y-%m-%dT%H:%M:%S"),
                "timeZone": "America/Los_Angeles",
            },
            "colorId": COLOR_CONCERT,
        }

    log.info("Desired events: {} movies, {} concerts (fitness managed "
             "by auto-booker)".format(
        sum(1 for e in desired_events.values()
            if e["colorId"] == COLOR_MOVIE),
        sum(1 for e in desired_events.values()
            if e["colorId"] == COLOR_CONCERT),
    ))

    # --- Sync: find existing managed events ---
    # Search a wide window: 7 days ago to 60 days from now
    time_min = (datetime.utcnow() - timedelta(days=7)).isoformat() + "Z"
    time_max = (datetime.utcnow() + timedelta(days=60)).isoformat() + "Z"

    existing_events = {}
    page_token = None
    while True:
        resp = service.events().list(
            calendarId=calendar_id,
            timeMin=time_min,
            timeMax=time_max,
            maxResults=2500,
            singleEvents=True,
            pageToken=page_token,
        ).execute()

        for item in resp.get("items", []):
            eid = item.get("id", "")
            if eid.startswith(EVENT_ID_PREFIX):
                existing_events[eid] = item

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    log.info("Found {} existing managed events on calendar".format(
        len(existing_events)))

    # --- Create or update ---
    created = 0
    updated = 0
    for eid, body in desired_events.items():
        if eid in existing_events:
            # Check if update needed (compare key fields)
            old = existing_events[eid]
            needs_update = (
                old.get("summary") != body["summary"]
                or old.get("colorId") != body.get("colorId")
                or old.get("description") != body.get("description")
                or old.get("start", {}).get("dateTime") !=
                body["start"]["dateTime"]
                or old.get("end", {}).get("dateTime") !=
                body["end"]["dateTime"]
            )
            if needs_update:
                service.events().update(
                    calendarId=calendar_id,
                    eventId=eid,
                    body=body,
                ).execute()
                updated += 1
        else:
            body["id"] = eid
            service.events().insert(
                calendarId=calendar_id,
                body=body,
            ).execute()
            created += 1

    # --- Delete events we no longer want ---
    deleted = 0
    for eid in existing_events:
        if eid not in desired_events:
            try:
                service.events().delete(
                    calendarId=calendar_id,
                    eventId=eid,
                ).execute()
                deleted += 1
            except Exception as e:
                log.warning("Failed to delete event {}: {}".format(eid, e))

    log.info("Sync complete: {} created, {} updated, {} deleted".format(
        created, updated, deleted))
    return created, updated, deleted


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    # Quick test: just authenticate and list upcoming events
    service = get_calendar_service()
    calendar_id = os.environ.get("GOOGLE_CALENDAR_ID", "primary")
    log.info("Testing connection to calendar: {}".format(calendar_id))

    resp = service.events().list(
        calendarId=calendar_id,
        maxResults=5,
        singleEvents=True,
        orderBy="startTime",
        timeMin=datetime.utcnow().isoformat() + "Z",
    ).execute()

    items = resp.get("items", [])
    log.info("Found {} upcoming events".format(len(items)))
    for item in items:
        log.info("  {} - {}".format(
            item.get("start", {}).get("dateTime", "all-day"),
            item.get("summary", "(no title)")))
