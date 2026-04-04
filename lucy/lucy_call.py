"""Lucy — Beth's AI companion.

Manages the full call lifecycle:
1. Pre-call: loads calendar events + conversation memory into the prompt
2. Triggers an outbound Vapi call with the enriched context
3. Post-call: fetches the transcript, summarizes it, stores the memory

Usage:
    python lucy_call.py call          # Make a call to Beth now
    python lucy_call.py post-process  # Process recent call transcripts
    python lucy_call.py test          # Test call to Connor's number
"""

import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger("lucy")

PACIFIC = ZoneInfo("America/Los_Angeles")
VAPI_API = "https://api.vapi.ai"
ASSISTANT_ID = "3c6d4439-1323-4d76-ba0d-548f9854f570"
PHONE_NUMBER_ID = "e3894fb6-4ab9-4d49-a418-ea03a09b371a"
LUCY_PHONE = "+19253323335"

MEMORY_DIR = Path(__file__).parent / "memory"
PROMPT_TEMPLATE = Path(__file__).parent / "system_prompt.txt"


def vapi_headers():
    key = os.environ.get("VAPI_API_KEY")
    if not key:
        raise RuntimeError("VAPI_API_KEY not set")
    return {
        "Authorization": "Bearer {}".format(key),
        "Content-Type": "application/json",
    }


# ── Calendar Context ────────────────────────────────────────────

def get_todays_calendar():
    """Fetch today's and tomorrow's events from Google Calendar."""
    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_KEY")
    calendar_id = os.environ.get("GOOGLE_CALENDAR_ID")
    if not creds_json or not calendar_id:
        return "Calendar not available right now."

    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_info(
        json.loads(creds_json),
        scopes=["https://www.googleapis.com/auth/calendar.readonly"])
    service = build("calendar", "v3", credentials=creds,
                    cache_discovery=False)

    now_pt = datetime.now(PACIFIC)
    today_start = now_pt.replace(
        hour=0, minute=0, second=0, microsecond=0)
    tomorrow_end = today_start + timedelta(days=2)

    resp = service.events().list(
        calendarId=calendar_id,
        timeMin=today_start.isoformat(),
        timeMax=tomorrow_end.isoformat(),
        singleEvents=True,
        orderBy="startTime",
        maxResults=20,
    ).execute()

    events = resp.get("items", [])
    if not events:
        return "Beth has no events on the calendar today or tomorrow."

    lines = []
    current_day = ""
    for ev in events:
        # Skip cancelled
        if ev.get("status") == "cancelled":
            continue

        start = ev.get("start", {})
        dt_str = start.get("dateTime", start.get("date", ""))
        summary = ev.get("summary", "Unknown")
        location = ev.get("location", "")

        try:
            dt = datetime.fromisoformat(dt_str)
            day_label = "Today" if dt.date() == now_pt.date() else "Tomorrow"
            time_label = dt.strftime("%-I:%M %p")
        except (ValueError, AttributeError):
            day_label = "Upcoming"
            time_label = ""

        if day_label != current_day:
            current_day = day_label
            lines.append("\n{}:".format(day_label))

        # Clean up emoji from summary for spoken context
        clean = summary
        for ch in ["\u2705", "\u23f3", "\U0001f3cb\ufe0f",
                    "\U0001f3ca", "\U0001f3ac", "\U0001f3b5"]:
            clean = clean.replace(ch, "")
        clean = clean.strip()

        entry = "- {} at {}".format(clean, time_label) if time_label \
            else "- {}".format(clean)
        if location and "Tice Creek" not in location:
            entry += " ({})".format(location.split(",")[0])
        lines.append(entry)

    return "\n".join(lines)


# ── Memory System ───────────────────────────────────────────────

def load_recent_memories(n=5):
    """Load the N most recent conversation summaries."""
    MEMORY_DIR.mkdir(exist_ok=True)
    files = sorted(MEMORY_DIR.glob("*.json"), reverse=True)

    if not files:
        return "This is the first conversation with Beth. Get to know her!"

    memories = []
    for f in files[:n]:
        try:
            data = json.loads(f.read_text())
            date = data.get("date", f.stem)
            summary = data.get("summary", "")
            follow_ups = data.get("follow_ups", [])
            mood = data.get("mood", "")

            entry = "{} — {}".format(date, summary)
            if mood:
                entry += " (mood: {})".format(mood)
            if follow_ups:
                entry += "\n  Follow up on: {}".format(
                    "; ".join(follow_ups))
            memories.append(entry)
        except (json.JSONDecodeError, KeyError):
            continue

    if not memories:
        return "This is the first conversation with Beth. Get to know her!"

    return "Recent conversations (most recent first):\n" + "\n".join(memories)


def save_memory(call_data):
    """Save a conversation summary from call transcript."""
    MEMORY_DIR.mkdir(exist_ok=True)

    transcript = call_data.get("transcript", "")
    if not transcript:
        log.info("No transcript to save")
        return

    # Use OpenAI to summarize the conversation
    openai_key = os.environ.get("OPENAI_API_KEY")
    if not openai_key:
        # Fall back to saving raw transcript
        ts = datetime.now(PACIFIC).strftime("%Y-%m-%d_%H%M")
        path = MEMORY_DIR / "{}.json".format(ts)
        path.write_text(json.dumps({
            "date": datetime.now(PACIFIC).strftime("%B %d, %Y"),
            "raw_transcript": transcript[:5000],
            "summary": "Transcript saved but not summarized (no OpenAI key)",
            "mood": "",
            "topics": [],
            "follow_ups": [],
        }, indent=2))
        return

    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={
            "Authorization": "Bearer {}".format(openai_key),
            "Content-Type": "application/json",
        },
        json={
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": (
                    "You summarize phone conversations between Lucy "
                    "(a personal assistant) and Beth (an elderly woman). "
                    "Extract: a brief summary, Beth's mood, key topics "
                    "discussed, and things to follow up on next time. "
                    "Respond in JSON with keys: summary, mood, topics "
                    "(array), follow_ups (array), health_notes (string)."
                )},
                {"role": "user", "content": (
                    "Summarize this conversation:\n\n{}"
                    .format(transcript[:4000])
                )},
            ],
            "response_format": {"type": "json_object"},
        },
    )

    if resp.status_code != 200:
        log.warning("OpenAI summarization failed: {}".format(resp.text[:200]))
        return

    try:
        summary_data = json.loads(
            resp.json()["choices"][0]["message"]["content"])
    except (KeyError, json.JSONDecodeError) as e:
        log.warning("Failed to parse summary: {}".format(e))
        return

    ts = datetime.now(PACIFIC).strftime("%Y-%m-%d_%H%M")
    path = MEMORY_DIR / "{}.json".format(ts)
    summary_data["date"] = datetime.now(PACIFIC).strftime("%B %d, %Y %-I:%M %p")
    summary_data["raw_transcript"] = transcript[:5000]
    path.write_text(json.dumps(summary_data, indent=2))
    log.info("Saved memory to {}".format(path.name))
    log.info("  Summary: {}".format(summary_data.get("summary", "")[:200]))
    log.info("  Mood: {}".format(summary_data.get("mood", "")))
    log.info("  Follow-ups: {}".format(summary_data.get("follow_ups", [])))


# ── Call Management ─────────────────────────────────────────────

def update_assistant_prompt(evening=False):
    """Update Lucy's system prompt with fresh calendar + memory context."""
    template = PROMPT_TEMPLATE.read_text()

    calendar_ctx = get_todays_calendar()
    memory_ctx = load_recent_memories()
    current_time = datetime.now(PACIFIC).strftime(
        "%A, %B %-d, %Y at %-I:%M %p PT")

    prompt = template.replace("{current_time}", current_time)
    prompt = prompt.replace("{calendar_context}", calendar_ctx)
    prompt = prompt.replace("{memory_context}", memory_ctx)

    if evening:
        first_message = (
            "Hi Beth! It's Lucy. Just calling to say goodnight!"
        )
        # Add evening-specific instructions to the prompt
        prompt += (
            "\n\n## TONIGHT'S CALL — EVENING WIND-DOWN\n"
            "This is the 11:30 PM bedtime call. Your priorities:\n"
            "1. PRIMARY: Gently encourage Beth to head to bed and "
            "use her CPAP machine tonight. Be warm about it — "
            "\"Have you got your CPAP all set up?\" or "
            "\"Make sure you use that CPAP tonight, okay?\"\n"
            "2. SECONDARY: Ask if she'd like to hear what's on her "
            "calendar tomorrow, or if there's anything she wants "
            "to be reminded of in the morning.\n"
            "3. Keep this call SHORT and gentle — she should be "
            "going to sleep soon. 2-3 minutes max.\n"
            "4. End the call by saying \"Goodnight, Beth.\" Keep it "
            "simple and warm. Let her know you'll call in the morning.\n"
        )
    else:
        first_message = (
            "Hi Beth! It's Lucy. How are you doing today?"
        )

    log.info("Updating Lucy's prompt ({})...".format(
        "evening" if evening else "morning"))
    log.info("  Calendar: {}".format(calendar_ctx[:200]))
    log.info("  Memory: {}".format(memory_ctx[:200]))

    # Build tools list
    tools = [
        {
            "type": "function",
            "function": {
                "name": "getCalendarEvents",
                "description": "Look up calendar events for a date range.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "start_date": {
                            "type": "string",
                            "description": "YYYY-MM-DD, defaults to today.",
                        },
                        "end_date": {
                            "type": "string",
                            "description": "YYYY-MM-DD, defaults to +3 days.",
                        },
                    },
                },
            },
            "server": {
                "url": "https://tice-creek-calendar.vercel.app/api/vapi_tools"
            },
        },
        {
            "type": "function",
            "function": {
                "name": "saveReminderPreferences",
                "description": "Save reminder preferences for events.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "remind_all": {"type": "boolean"},
                        "preferences": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "event_name": {"type": "string"},
                                    "wants_reminder": {"type": "boolean"},
                                },
                            },
                        },
                    },
                },
            },
            "server": {
                "url": "https://tice-creek-calendar.vercel.app/api/vapi_tools"
            },
        },
    ]

    resp = requests.patch(
        "{}/assistant/{}".format(VAPI_API, ASSISTANT_ID),
        headers=vapi_headers(),
        json={
            "model": {
                "provider": "openai",
                "model": "gpt-4o-mini",
                "messages": [{"role": "system", "content": prompt}],
                "tools": tools,
            },
            "firstMessage": first_message,
        },
    )

    if resp.status_code == 200:
        log.info("  Prompt updated successfully")
    else:
        log.warning("  Failed to update prompt: {}".format(
            resp.text[:200]))

    return resp.status_code == 200


def make_call(phone_number):
    """Place an outbound call to the given number."""
    log.info("Calling {}...".format(phone_number))

    resp = requests.post(
        "{}/call".format(VAPI_API),
        headers=vapi_headers(),
        json={
            "assistantId": ASSISTANT_ID,
            "phoneNumberId": PHONE_NUMBER_ID,
            "customer": {
                "number": phone_number,
            },
        },
    )

    if resp.status_code in (200, 201):
        call_data = resp.json()
        log.info("Call initiated! ID: {}".format(call_data.get("id", "")))
        return call_data
    else:
        log.error("Call failed: {}".format(resp.text[:300]))
        return None


def wait_for_call_end(call_id, timeout=300, poll_interval=10):
    """Poll Vapi until the call ends or times out. Returns the call data."""
    import time
    elapsed = 0
    while elapsed < timeout:
        time.sleep(poll_interval)
        elapsed += poll_interval
        resp = requests.get(
            "{}/call/{}".format(VAPI_API, call_id),
            headers=vapi_headers(),
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("status") == "ended":
                return data
    return None


def call_was_answered(call_data):
    """Check if a completed call was actually answered by a human."""
    if not call_data:
        return False
    reason = call_data.get("endedReason", "")
    # These reasons mean nobody picked up
    no_answer = ("no-answer", "busy", "failed", "machine-detected")
    if reason in no_answer:
        return False
    # If the call lasted less than 10 seconds, likely not answered
    started = call_data.get("startedAt", "")
    ended = call_data.get("endedAt", "")
    if started and ended:
        try:
            s = datetime.fromisoformat(started.replace("Z", "+00:00"))
            e = datetime.fromisoformat(ended.replace("Z", "+00:00"))
            if (e - s).total_seconds() < 10:
                return False
        except (ValueError, TypeError):
            pass
    return True


def make_call_with_fallback(home_phone, cell_phone):
    """Try home phone first, fall back to cell if no answer."""
    log.info("Trying home phone first: {}".format(home_phone))
    call_data = make_call(home_phone)

    if not call_data:
        log.info("Home call failed to initiate, trying cell...")
        return make_call(cell_phone)

    call_id = call_data.get("id", "")
    log.info("Waiting for home call to complete...")
    result = wait_for_call_end(call_id)

    if call_was_answered(result):
        log.info("Home phone answered! Call complete.")
        return result

    log.info("Home phone not answered (reason: {}). Trying cell: {}".format(
        result.get("endedReason", "unknown") if result else "timeout",
        cell_phone))
    return make_call(cell_phone)


def get_recent_calls(limit=5):
    """Get recent calls from Vapi."""
    resp = requests.get(
        "{}/call".format(VAPI_API),
        headers=vapi_headers(),
        params={"limit": limit, "assistantId": ASSISTANT_ID},
    )
    if resp.status_code == 200:
        return resp.json()
    log.warning("Failed to get calls: {}".format(resp.text[:200]))
    return []


def process_recent_calls():
    """Fetch transcripts from recent calls and save memories."""
    calls = get_recent_calls(limit=3)
    log.info("Found {} recent calls".format(len(calls)))

    for call in calls:
        call_id = call.get("id", "")
        status = call.get("status", "")
        ended = call.get("endedAt", "")

        if status != "ended":
            log.info("  Call {} status: {} — skipping".format(
                call_id[:8], status))
            continue

        # Check if we already processed this call
        marker = MEMORY_DIR / ".processed_{}".format(call_id[:16])
        if marker.exists():
            log.info("  Call {} already processed".format(call_id[:8]))
            continue

        log.info("  Processing call {}...".format(call_id[:8]))

        # Get full call details with transcript
        resp = requests.get(
            "{}/call/{}".format(VAPI_API, call_id),
            headers=vapi_headers(),
        )
        if resp.status_code != 200:
            continue

        call_detail = resp.json()

        # Build transcript from messages
        messages = call_detail.get("messages", [])
        if not messages:
            # Try artifact
            artifact = call_detail.get("artifact", {})
            messages = artifact.get("messages", [])

        transcript_lines = []
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", msg.get("message", ""))
            if role and content:
                speaker = "Lucy" if role == "assistant" else "Beth"
                transcript_lines.append("{}: {}".format(speaker, content))

        transcript = "\n".join(transcript_lines)

        if transcript:
            save_memory({"transcript": transcript})
            marker.touch()
            log.info("  Saved memory for call {}".format(call_id[:8]))
        else:
            log.info("  No transcript found for call {}".format(
                call_id[:8]))


# ── Main ────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Usage: python lucy_call.py [call|post-process|test|update]")
        sys.exit(1)

    command = sys.argv[1]

    if command == "call":
        # Morning call: update prompt, then call Beth
        beth_home = os.environ.get("BETH_PHONE_NUMBER", "+19252781199")
        beth_cell = os.environ.get("BETH_CELL_NUMBER", "+14403211704")
        update_assistant_prompt()
        make_call_with_fallback(beth_home, beth_cell)

    elif command == "call-evening":
        # Evening wind-down call
        beth_home = os.environ.get("BETH_PHONE_NUMBER", "+19252781199")
        beth_cell = os.environ.get("BETH_CELL_NUMBER", "+14403211704")
        update_assistant_prompt(evening=True)
        make_call_with_fallback(beth_home, beth_cell)

    elif command == "test":
        # Test call to Connor
        test_phone = sys.argv[2] if len(sys.argv) > 2 \
            else os.environ.get("TEST_PHONE_NUMBER", "+14404885786")
        update_assistant_prompt()
        make_call(test_phone)

    elif command == "post-process":
        # Process recent call transcripts into memories
        process_recent_calls()

    elif command == "update":
        # Just update the prompt (no call)
        update_assistant_prompt()

    else:
        print("Unknown command: {}".format(command))
        sys.exit(1)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    main()
