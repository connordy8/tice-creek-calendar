# Project Notes — Beth's Calendar System

Living document capturing the goals, decisions, gotchas, and steers behind this project. Future Claude sessions (and future Connor) should read this before making changes so context isn't lost.

Last updated: 2026-04-17

---

## What this project is

An automated calendar system for Beth (Connor's mom) at Rossmoor 55+ community in Walnut Creek, CA. It keeps her Google Calendar continuously in sync with three sources:

1. **Tice Creek Fitness Center** — classes (fitness + aquatics) scraped from Mindbody widgets
2. **Rossmoor Peacock Hall** — movies and concerts scraped from rossmoor.com
3. **Email-based manual events** — Connor forwards emails (e.g. appointments) and Claude parses them into calendar entries

On top of scraping, the system also **auto-books** classes on Beth's behalf the moment registration windows open, and **alerts Connor** at `connordy@gmail.com` when anything goes wrong.

- **Local path:** `/Users/connordy/tice-creek-calendar`
- **GitHub:** `https://github.com/connordy8/tice-creek-calendar`
- **Runs on:** GitHub Actions (no server required)

---

## Beth's preferences (the "why" behind the filters)

These came out of many rounds of feedback. Treat them as load-bearing — don't silently change them.

### Classes she wants
- Zumba, UJAM
- Aquacise, Deep Water Aerobics (aquatics)
- Posture / Balance / Core and Strength
- Mat Yoga
- Functional Fitness / Functional Strength
- Tai Chi
- ForeverFit
- Let's Stretch
- Strength and Stretch

### Classes she does NOT want
- **Pickleball** (explicitly removed — Apr 2026)
- Anything before 11 AM (she's not a morning person)
- Anything cancelled

### Display tweaks she likes
- Events start **15 minutes early** on the calendar so she has travel time. The true class time is in the description.
- Class **location** (e.g. "Serenity Studio", "Aerobics Studio", "Aquatics") appears in the event so she knows where to go.
- Emoji prefixes make the calendar scannable: 🏋️ fitness, 🏊 aquatics, ✅ booked, ⏳ waitlist.
- Waitlist entries have "(waitlist)" suffix. The "(From Waitlist - Unconfirmed)" Mindbody suffix is stripped out.

---

## Architecture

```
Tice Creek (Mindbody widgets)  ─┐
Rossmoor movies/concerts       ─┼─>  scraper.py  ─>  ICS file  ─>  gcal_sync.py  ─>  Google Calendar
Forwarded emails               ─┴─>  email_handler.py (Claude Sonnet parses) ─────┘
                                                                                   ↑
                                     auto_book.py (Playwright → Mindbody) ─────────┘
                                     healthcheck.py (validates calendar)
                                     notify.py (SMTP alerts on failure)
```

Google Calendar is written to via a service account (key stored in GitHub Secrets, never committed).

### Key files
| File | Role |
|---|---|
| `scraper.py` | Playwright scraper: Tice Creek fitness + aquatics + Rossmoor entertainment. Has retry logic. |
| `auto_book.py` | Playwright auto-booker. Logs in to Mindbody, books target classes, marks calendar ✅/⏳. |
| `gcal_sync.py` | Pushes ICS events into Google Calendar. Has API retry wrapper. |
| `email_handler.py` | IMAPs Beth's dedicated inbox (`bethcalendarupdate@gmail.com`). Two-stage Claude pipeline: classifier → extractor. Confidence-gated. See "Email handler" section below. |
| `notify.py` | Sends failure alerts via SMTP to `connordy@gmail.com`. |
| `healthcheck.py` | Cron job that asserts the calendar has events in the next 7 days. |
| `config.yaml` | Target class list, filters, display preferences. Single source of truth for Beth's prefs. |
| `manual_events.json` | One-off events added via the `add-event.yml` workflow. |

### Workflows (`.github/workflows/`)
- `scraper.yml` — periodic scrape + calendar sync
- `auto-book.yml` — **18 runs/day** including midnight (12:00, 12:15, 12:30, 1:00 AM PT) and early morning (5:00, 5:15, 5:30 AM PT) flurries to catch Mindbody booking windows the moment they open
- `check-email.yml` — polls Gmail for forwarded events
- `healthcheck.yml` — asserts calendar has content; alerts if empty
- `dump-calendar.yml` — manual: prints the next 7 days (used for "is the calendar up to date?" checks)
- `add-event.yml` — manual: add a one-off event by form input
- `remove-events.yml` — manual: delete events matching a keyword (used to purge pickleball)
- `cleanup-dupes.yml` — manual: deduplicates events with identical (summary, start time)

---

## Event ID scheme

Deterministic MD5 hashes so reruns update instead of creating duplicates.

- `be0ca1…` — events created by `scraper.py`
- `ab00ce0d…` — events created by `auto_book.py`

**Gotcha:** if you change the hash inputs, you'll orphan existing events. That's what caused the duplicate-events incident in April 2026. Always run `cleanup-dupes.yml` after such a change.

---

## Mindbody quirks (hard-won knowledge)

- Fitness classes use `sLoc=0`. Aquatics classes use `sLoc=1`. **You must scan both** — missing `sLoc=1` is why Monday Aquacise was missing for a while.
- "Functional Fitness" and "Functional Strength" are different class name strings; keep both in the include list.
- Registration windows open at Rossmoor-specific times. The booking schedule flurries at midnight and 5–7 AM PT exist to catch those windows.
- Mindbody adds a "(From Waitlist - Unconfirmed)" suffix to class names when Beth is promoted off the waitlist. We strip it with:
  ```python
  re.sub(r'\s*\(From\s+Waitlist[^)]*\)', '', name)
  ```
- The BW widget renders room names inline; we extract them from known tokens: "Serenity Studio", "Aerobics Studio", "Serenity Room", "Aquatics", etc.

---

## Email handler — bulletproof logic

**Inbox:** `bethcalendarupdate@gmail.com` (forward event-related emails here).

**Why this is hard:** Connor has auto-forwarded some senders (e.g. Zumba instructor). Most of those emails have no calendar impact. We must not hallucinate events from pep talks, newsletters, or "hope to see you" notes.

**Two-stage Claude pipeline:**

1. **Classifier** — strict gatekeeper. "Does this email contain a SPECIFIC, ACTIONABLE calendar change?" Biased toward NO. Rejects newsletters, thank-yous, general announcements, vague mentions of dates. Returns `{relevant: bool, reason: str}`.
2. **Extractor** — only runs if classifier said YES. Returns actions with a `confidence` score (0.0–1.0) and `reasoning`.

**Confidence gates (post-extraction):**
- `≥ 0.85` → auto-apply
- `0.60–0.84` → alert Connor for review, do not apply
- `< 0.60` → drop silently

**Hard sanity checks (all must pass to apply):**
- Date parses and is within `[today - 1 day, today + 180 days]`
- Time parses if present
- For `cancel`/`modify`, `original_class` fuzzy-matches a known class in `KNOWN_CLASS_NAMES`
- For `add`, title is ≥ 3 chars and not a generic word ("class", "event", etc.)
- Dedup on (type, title, date, start_time) — skip if already present

**Anti-spam:** the classifier stage means irrelevant emails are dropped BEFORE the extractor runs, so forwarded newsletters don't trigger review alerts.

**Audit log:** every decision (classifier verdict, extraction, final disposition) is appended to `email_audit.log` and uploaded as a workflow artifact (gitignored). Review to tune thresholds.

**Testing:**
- `python email_handler.py --audit 20` — peek at the last 20 emails (read or unread) without marking them read or modifying state. Prints a decision table.
- GitHub Actions: run the `Audit Email Handler` workflow manually with a count input. Decision table shown in logs, `email_audit.log` downloadable as an artifact.

**Tuning knobs (in `email_handler.py`):**
- `CONFIDENCE_APPLY`, `CONFIDENCE_REVIEW` — raise to be more conservative
- `KNOWN_CLASS_NAMES` — add new class names Beth cares about
- `MAX_DAYS_OUT` — cap how far in the future an event can be scheduled

---

## Reliability steers

Connor's standing direction: **"Make it bulletproof. Don't crash on bad data. Alert me when something's wrong."**

- Every entry point is wrapped in try/except. We log + alert rather than crashing the whole workflow.
- `notify.py` sends an email to `connordy@gmail.com` via SMTP on any caught exception.
- `healthcheck.py` runs on a cron and alerts if the calendar is empty / stale — catches silent failures where workflows "succeed" but did nothing.
- Concurrency groups are set on workflows so two scraper runs can't race each other into duplicates.
- Scraper and gcal writes have retry wrappers for transient network errors.
- The email parser validates Claude's JSON output against a schema. Unknown actions trigger an alert instead of silently being dropped.

### What NOT to do
- Don't `exit(1)` on non-critical errors — it causes the whole workflow to fail and Connor gets spurious alerts. Log + alert, then continue.
- Don't commit `.env.production` or anything with the service account private key. `.env*` is in `.gitignore`.
- Don't use `--no-verify` or skip pre-commit hooks.
- Don't amend commits — create new ones.
- Don't force-push to main.

---

## Phone reminders — separate project

Phone reminders were previously in this repo and have been **removed** (Apr 2026). That's a different project now. If you see references to `phone_reminder.py` or `phone-reminder.yml`, they're stale — the code was deleted and the workflow was producing ~80 failure emails before removal.

---

## History of fixes (so we don't repeat them)

| Symptom | Root cause | Fix |
|---|---|---|
| No Monday classes | Scraper only hit `sLoc=0` (fitness), missed aquatics | Scan both `sLoc=0` and `sLoc=1` |
| Missing Functional Fitness | Keyword was `functional strength` only | Added both variants to `include_classes` |
| Auto-booker crashed 4× | `room` variable referenced before assignment | Moved `room = cls.get("room", "")` above hash line |
| Duplicate events | Event ID hash inputs changed | Built `cleanup-dupes.yml`; groups by (summary, start) and keeps newest |
| "(From Waitlist - Unconfirmed)" clutter | Mindbody suffix on waitlist promotions | Regex strip in display name |
| Pickleball appearing | In `TARGET_CLASSES` | Removed from config + ran `remove-events.yml` |
| 80 failure emails | Phone reminder workflow referenced deleted script | Removed workflow file |
| Silent workflow "successes" doing nothing | No validation of output | Added `healthcheck.py` cron |

---

## How to answer common questions

- **"Is the calendar up to date?"** → Trigger `dump-calendar.yml`, read its log, show the next 7 days grouped by day with ✅/⏳ markers.
- **"List activities for the next week"** → Same as above.
- **"Add this appointment"** (often with a screenshot) → Use `add-event.yml` workflow with form fields, OR commit to `manual_events.json` if it's a recurring thing.
- **"Remove X"** → Use `remove-events.yml` with the keyword.
- **"Why didn't X get booked?"** → Check `auto-book.yml` run logs. Could be: class not in `include_classes`, registration window hadn't opened yet, or Mindbody waitlisted her.

---

## Secrets (stored in GitHub Actions Secrets, never in the repo)

- `GOOGLE_SERVICE_ACCOUNT_KEY` — JSON service account key
- `GOOGLE_CALENDAR_ID` — Beth's calendar ID
- `MINDBODY_USERNAME` / `MINDBODY_PASSWORD` — Beth's login
- `IMAP_USERNAME` / `IMAP_PASSWORD` — for email polling
- `ANTHROPIC_API_KEY` — Claude Sonnet for email parsing
- `SMTP_*` — outgoing alerts to `connordy@gmail.com`

---

## Guidance for future Claude sessions

- **Read this doc first.** It captures decisions that aren't obvious from the code.
- **Don't silently change Beth's preferences** (class list, earliest hour, early-start offset). Ask.
- **Test via GitHub Actions, not locally.** Connor's Mac doesn't have the secrets or Playwright browsers configured; local runs will mislead you.
- **When in doubt, alert don't crash.** Connor would rather get an email than have the calendar go dark.
- **Keep this file updated.** When you make a non-obvious decision or hit a gotcha, add a row to the relevant section.
