"""Vapi server-side tool handler for Lucy."""

import json
import os
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")


def _get_calendar_service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_KEY")
    calendar_id = os.environ.get("GOOGLE_CALENDAR_ID")
    if not creds_json or not calendar_id:
        return None, None

    creds = service_account.Credentials.from_service_account_info(
        json.loads(creds_json),
        scopes=["https://www.googleapis.com/auth/calendar"],
    )
    service = build("calendar", "v3", credentials=creds,
                    cache_discovery=False)
    return service, calendar_id


def _clean_summary(summary):
    clean = summary
    for ch in ["\u2705", "\u23f3", "\U0001f3cb\ufe0f",
               "\U0001f3ca", "\U0001f3ac", "\U0001f3b5"]:
        clean = clean.replace(ch, "")
    for suffix in ["(waitlist)", "(drop-in)", "(club)"]:
        clean = clean.replace(suffix, "")
    return clean.strip()


def get_calendar_events(args):
    service, calendar_id = _get_calendar_service()
    if not service:
        return "Calendar is not available right now."

    now_pt = datetime.now(PACIFIC)

    start_str = args.get("start_date", "")
    end_str = args.get("end_date", "")

    try:
        start_dt = datetime.strptime(start_str, "%Y-%m-%d").replace(
            tzinfo=PACIFIC) if start_str else now_pt.replace(
            hour=0, minute=0, second=0, microsecond=0)
    except ValueError:
        start_dt = now_pt.replace(hour=0, minute=0, second=0, microsecond=0)

    try:
        end_dt = (datetime.strptime(end_str, "%Y-%m-%d").replace(
            tzinfo=PACIFIC) + timedelta(days=1)) if end_str else (
            start_dt + timedelta(days=3))
    except ValueError:
        end_dt = start_dt + timedelta(days=3)

    resp = service.events().list(
        calendarId=calendar_id,
        timeMin=start_dt.isoformat(),
        timeMax=end_dt.isoformat(),
        singleEvents=True,
        orderBy="startTime",
        maxResults=30,
    ).execute()

    events = resp.get("items", [])
    if not events:
        return "Beth has nothing on the calendar for those dates."

    lines = []
    current_day = ""
    for ev in events:
        if ev.get("status") == "cancelled":
            continue

        start = ev.get("start", {})
        dt_str = start.get("dateTime", start.get("date", ""))
        summary = ev.get("summary", "Unknown")
        location = ev.get("location", "")

        try:
            dt = datetime.fromisoformat(dt_str)
            if dt.date() == now_pt.date():
                day_label = "Today ({})".format(dt.strftime("%A, %B %-d"))
            elif dt.date() == (now_pt + timedelta(days=1)).date():
                day_label = "Tomorrow ({})".format(dt.strftime("%A, %B %-d"))
            else:
                day_label = dt.strftime("%A, %B %-d")
            time_label = dt.strftime("%-I:%M %p")
        except (ValueError, AttributeError):
            day_label = "Upcoming"
            time_label = ""

        if day_label != current_day:
            current_day = day_label
            lines.append("\n{}:".format(day_label))

        clean = _clean_summary(summary)
        entry = "- {} at {}".format(clean, time_label) if time_label \
            else "- {}".format(clean)
        if location and "Tice Creek" not in location:
            entry += " ({})".format(location.split(",")[0])
        lines.append(entry)

    return "\n".join(lines).strip()


def save_reminder_preferences(args):
    service, calendar_id = _get_calendar_service()
    if not service:
        return "Could not save preferences."

    remind_all = args.get("remind_all", True)
    preferences = args.get("preferences", [])
    now_pt = datetime.now(PACIFIC)
    today_end = now_pt.replace(hour=23, minute=59, second=59, microsecond=0)

    resp = service.events().list(
        calendarId=calendar_id,
        timeMin=now_pt.isoformat(),
        timeMax=today_end.isoformat(),
        singleEvents=True,
        orderBy="startTime",
        maxResults=20,
    ).execute()

    skipped = []
    reminded = []

    for ev in resp.get("items", []):
        if ev.get("status") == "cancelled":
            continue
        summary = ev.get("summary", "")
        event_id = ev.get("id", "")
        clean = _clean_summary(summary)

        wants_reminder = remind_all
        if preferences:
            for pref in preferences:
                pref_name = pref.get("event_name", "").lower()
                if pref_name and pref_name in summary.lower():
                    wants_reminder = pref.get("wants_reminder", True)
                    break

        if not wants_reminder:
            try:
                service.events().patch(
                    calendarId=calendar_id,
                    eventId=event_id,
                    body={"extendedProperties": {
                        "private": {"bethSkipReminder": now_pt.isoformat()}
                    }},
                ).execute()
                skipped.append(clean)
            except Exception:
                pass
        else:
            reminded.append(clean)

    parts = []
    if reminded:
        parts.append("Reminders set for: {}".format(", ".join(reminded)))
    if skipped:
        parts.append("No reminder for: {}".format(", ".join(skipped)))
    return ". ".join(parts) if parts else "Beth will get reminders for all events today."


TOOLS = {
    "getCalendarEvents": get_calendar_events,
    "saveReminderPreferences": save_reminder_preferences,
}


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            payload = json.loads(body) if body else {}
        except (json.JSONDecodeError, ValueError):
            self._respond(400, {"error": "Invalid JSON"})
            return

        message = payload.get("message", {})
        tool_calls = message.get("toolCallList", [])
        if not tool_calls:
            tool_calls = payload.get("toolCallList", [])
        if not tool_calls:
            self._respond(400, {"error": "No tool calls found"})
            return

        results = []
        for tc in tool_calls:
            tc_id = tc.get("id", "")
            func = tc.get("function", {})
            name = func.get("name", "")
            try:
                args = json.loads(func.get("arguments", "{}"))
            except json.JSONDecodeError:
                args = {}

            handler_fn = TOOLS.get(name)
            if handler_fn:
                try:
                    result = handler_fn(args)
                except Exception as e:
                    result = "Sorry, I couldn't look that up right now."
            else:
                result = "Unknown tool: {}".format(name)

            results.append({"toolCallId": tc_id, "result": result})

        self._respond(200, {"results": results})

    def do_GET(self):
        self._respond(200, {"status": "ok", "tools": list(TOOLS.keys())})

    def _respond(self, status, data):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())
