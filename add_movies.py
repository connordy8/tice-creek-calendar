"""One-off: push a hand-curated list of Rossmoor movie showings to Beth's calendar.

Reads movie_showings.json and upserts each showing with a deterministic
event ID, so re-running is safe. Uses its own ID prefix (not gcal_sync's
EVENT_ID_PREFIX) so the main sync's cleanup step never deletes these.

Each showing: {title, year, date, time ("19:00"), runtime_min, rating,
series, rt_score, description}
"""

import hashlib
import json
import os
import sys
from datetime import datetime, timedelta

from googleapiclient.errors import HttpError

from gcal_sync import COLOR_MOVIE, get_calendar_service
from scraper import MOVIE_LOCATION

ID_PREFIX = "be0cb2"
EARLY_START_MINUTES = 15  # matches config.yaml early_start_minutes


def event_id(s):
    raw = "manual-movie-{}-{}-{}".format(s["title"], s["date"], s["time"])
    return ID_PREFIX + hashlib.md5(raw.encode()).hexdigest()


def build_body(s):
    start = datetime.fromisoformat("{}T{}".format(s["date"], s["time"]))
    end = start + timedelta(minutes=s.get("runtime_min") or 120)
    cal_start = start - timedelta(minutes=EARLY_START_MINUTES)

    score = s.get("rt_score")
    summary = "\U0001f3ac {} \U0001f345 {}".format(s["title"], score) if score \
        else "\U0001f3ac {}".format(s["title"])

    desc = []
    if s.get("description"):
        desc.append(s["description"])
        desc.append("")
    desc.append("Showtime: {} at Peacock Hall".format(
        start.strftime("%I:%M %p").lstrip("0")))
    details = [str(s["year"])]
    if s.get("runtime_min"):
        details.append("{} min".format(s["runtime_min"]))
    if s.get("rating"):
        details.append(s["rating"])
    desc.append(" · ".join(details))
    if s.get("series"):
        desc.append("Series: {}".format(s["series"]))
    if score:
        desc.append("Rotten Tomatoes: {}".format(score))
    desc.append("Free admission")
    desc.append("Source: myrossmoor.com/events-calendar")

    return {
        "summary": summary,
        "description": "\n".join(desc),
        "location": MOVIE_LOCATION,
        "start": {"dateTime": cal_start.isoformat(),
                  "timeZone": "America/Los_Angeles"},
        "end": {"dateTime": end.isoformat(),
                "timeZone": "America/Los_Angeles"},
        "colorId": COLOR_MOVIE,
    }


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "movie_showings.json"
    with open(path) as f:
        showings = json.load(f)

    calendar_id = os.environ["GOOGLE_CALENDAR_ID"]
    svc = get_calendar_service()

    created = updated = 0
    for s in showings:
        eid = event_id(s)
        body = build_body(s)
        try:
            svc.events().insert(calendarId=calendar_id,
                                body=dict(body, id=eid)).execute()
            created += 1
            print("created  {} {}  {}".format(s["date"], s["time"], body["summary"]))
        except HttpError as e:
            if e.resp.status != 409:
                raise
            svc.events().update(calendarId=calendar_id, eventId=eid,
                                body=body).execute()
            updated += 1
            print("updated  {} {}  {}".format(s["date"], s["time"], body["summary"]))

    print("Done: {} created, {} updated".format(created, updated))


if __name__ == "__main__":
    main()
