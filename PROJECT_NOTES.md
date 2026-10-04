# Project Notes — Beth's Calendar System

Living document capturing the goals, decisions, gotchas, and steers behind this project. Future Claude sessions (and future Connor) should read this before making changes so context isn't lost.

Last updated: 2026-10-03

---

## What this project is

An automated calendar system for Beth (Connor's mom) at Rossmoor 55+ community in Walnut Creek, CA. It keeps her Google Calendar continuously in sync with three sources:

1. **Tice Creek Fitness Center** — classes (fitness + aquatics) scraped from Mindbody widgets
2. **Rossmoor Peacock Hall** — movies and concerts scraped from myrossmoor.com/events-calendar
3. **Email-based manual events** — Connor forwards emails (e.g. appointments) and Claude parses them into calendar entries

Since May 2026 the system does **not** book classes. It *lists* every class Beth likes, with sign-up status in the title ("sign up · 4 left", "FULL · waitlist", "drop-in", "sign-up opens Mon"), and Beth signs up herself. Movies show the Rotten Tomatoes score in the title and a plot summary in the description.

- **Local path:** `/Users/connordy/tice-creek-calendar`
- **GitHub:** `https://github.com/connordy8/tice-creek-calendar`
- **Runs on:** GitHub Actions (no server required)

---

## Beth's preferences (the "why" behind the filters)

These came out of many rounds of feedback. Treat them as load-bearing — don't silently change them.

### Classes she wants
- **Zumba — her favorite.** Every Zumba session is listed regardless of time, including the 9:45 AM Tue/Thu **Zumba Club** (Mindbody: "CLUB: Zumba", shown as "Zumba Club"; Carol Lehr's forwarded emails are about this one)
- UJAM
- Aquacise, Deep Water Aerobics (aquatics)
- Posture / Balance / Core and Strength
- Mat Yoga
- Functional Fitness / Functional Strength
- ForeverFit
- Let's Stretch
- Strength and Stretch

### Classes she does NOT want
- **Pickleball** (explicitly removed — Apr 2026)
- **Tai Chi** (explicitly removed — Oct 2026). Her own "Tai Chi- Begginers" entries are hers; leave them.
- Anything before 11 AM (she's not a morning person), except Zumba
- Anything cancelled

### Display tweaks she likes
- Events start **15 minutes early** on the calendar so she has travel time. The true class time is in the description.
- Class **location** (e.g. "Serenity Studio", "Aerobics Studio", "Aquatics") appears in the event so she knows where to go.
- Emoji prefixes make the calendar scannable: 🏋️ fitness, 🏊 aquatics, ✅ booked, ⏳ waitlist.
- Waitlist entries have "(waitlist)" suffix. The "(From Waitlist - Unconfirmed)" Mindbody suffix is stripped out.

---

## Architecture

```
Tice Creek (Mindbody branded-web widget, plain HTTP) ── tice_schedule.py ─┐
Rossmoor movies/concerts (myrossmoor.com, plain HTTP) ── scraper.py ──────┼─> gcal_sync.py ─> Google Calendar
  └ RT scores + plot summaries ── movie_info.py (cache: movie_info.json)  │
Forwarded emails ── email_handler.py ─> manual_events.json ───────────────┘
```

`scraper.py` is the entry point (run by `sync.yml`). No Playwright, no Mindbody login. Google Calendar is written via a service account (key in GitHub Secrets).

### Key files
| File | Role |
|---|---|
| `scraper.py` | Entry point. Fetches classes + Rossmoor events, filters to Beth's prefs, calls `gcal_sync`. Each source fails independently; exits 1 at the end if any failed. |
| `tice_schedule.py` | Fetches the Tice Creek schedule (group fitness + aquatics) from Mindbody's branded-web widget. Parses the Next.js flight payload. |
| `movie_info.py` | Rotten Tomatoes score (RT search page) + Wikipedia summary, cached in `movie_info.json`. Hand-edit an entry and set `"locked": true` to pin it. |
| `gcal_sync.py` | Reconciles the calendar. Read its docstring: the safety rules live there. |
| `email_handler.py` | IMAPs `bethcalendarupdate@gmail.com`. Two-stage Claude pipeline: classifier → extractor. Confidence-gated. Writes `manual_events.json`. |
| `config.yaml` | Target class list, filters, display preferences. Single source of truth for Beth's prefs. |
| `auto_book.py` | RETIRED (manual-only). Old Playwright booker; its login broke in May 2026. |
| `canary.py`, `weekly_audit.py` | Manual-only monitors. Still use the old Playwright scraper, so currently broken. |

### Workflows (`.github/workflows/`)
- `sync.yml` — every 3 hours 6 AM–9 PM PT: email check + scrape + calendar sync. Also re-enables itself and `check-email.yml` (keep-alive).
- `check-email.yml` — every 15 min 6–11 AM PT, every 30 min until ~10 PM: polls Gmail for forwarded events, then triggers `sync.yml` if anything changed. Crons are deliberately off :00/:30 (GitHub drops many top-of-hour runs); still best-effort, so treat email latency as up to an hour or two.
- `auto-book.yml` — retired, manual-only.
- `dump-calendar.yml` — manual: prints the next 7 days (used for "is the calendar up to date?" checks)
- `add-event.yml` — manual: add a one-off event by form input
- `remove-events.yml` — manual: delete events matching a keyword (used to purge pickleball)
- `cleanup-dupes.yml` — manual: deduplicates events with identical (summary, start time)

---

## Event ID scheme

Deterministic MD5 hashes so reruns update instead of creating duplicates. `gcal_sync` only ever touches events with these prefixes:

- `be0ca1…` — movies + concerts (hash of title/date/time)
- `be0cc3…` — Tice Creek class listings (hash of the Mindbody class instance id; `extendedProperties.private.source` = `group_fitness`/`aquatics`)
- `be0cd4…` — appointments from `manual_events.json` (hash of the entry's `uid`)
- `be0cb2…` — legacy one-off movie events (Oct 2026), swapped out by the first good sync
- `ab00ce0d…` — legacy `auto_book.py` events

**Gotcha:** if you change the hash inputs, you'll orphan existing events. That's what caused the duplicate-events incident in April 2026. Always run `cleanup-dupes.yml` after such a change.

---

## Mindbody quirks (hard-won knowledge)

- **May 2026: Tice Creek switched to Mindbody's "branded web" widget.** The `<healcode-widget>` on their pages now just injects an iframe at `go.mindbodyonline.com/book/widgets/schedules/view/<id>/schedule`. `tice_schedule.py` resolves `<id>` at runtime (healcode id → `widgets.mindbodyonline.com/widgets/schedules/<healcode>.json`), falling back to `3355016f2c5` (group fitness) / `3355017f2c5` (aquatics).
- The widget page embeds the next 7 days (aquatics: ~4 weeks) as JSON in `self.__next_f.push(...)` chunks, under `"initialClasses"`. Repeated objects are de-duplicated into `"$2f"`-style references to other payload rows; resolve them or staff/instructor data goes missing.
- `bookable` is the reliable "can sign up now" flag. `numberRegistered` often exceeds `capacity` (the widget then shows "Only -6 spots left!"), so only show a spot count when it's between 1 and capacity.
- `capacity == 0` means no online sign-up: CLUB classes and Water Aerobics. These are listed as drop-in.
- Beth's own calendar has entries for classes she books (e.g. " Aqua: Aquacise", "Forever Fit" - apparently added by the Mindbody app). `gcal_sync` reads them (never edits them) and skips listing a class when one of hers shares a name word and starts within 20 min.
- Sign-up opens 7 days ahead at 8 AM (`bookingWindowStart`).

- Fitness classes use `sLoc=0`. Aquatics classes use `sLoc=1`. **You must scan both** — missing `sLoc=1` is why Monday Aquacise was missing for a while.
- "Functional Fitness" and "Functional Strength" are different class name strings; keep both in the include list.
- Registration windows open at Rossmoor-specific times. The booking schedule flurries at midnight and 5–7 AM PT exist to catch those windows.
- Mindbody adds a "(From Waitlist - Unconfirmed)" suffix to class names when Beth is promoted off the waitlist. We strip it with:
  ```python
  re.sub(r'\s*\(From\s+Waitlist[^)]*\)', '', name)
  ```
- The BW widget renders room names inline; we extract them from known tokens: "Serenity Studio", "Aerobics Studio", "Serenity Room", "Aquatics", etc.

---

## Self-monitoring (so bugs don't sit silently)

The system used to "succeed" green even while quietly dropping classes from Beth's calendar. We've added two passive monitors that alert Connor without requiring him to spot-check.

### `weekly_audit.py` (workflow: `Weekly Calendar Audit`)
- Runs Sunday 10 PM PT.
- Scrapes Mindbody for the next 7 days, applies Beth's `include_classes` + `earliest_hour` filter.
- Pulls her actual Google Calendar.
- **Diffs the two and emails Connor any preference-matching class that exists at Tice Creek but is NOT on her calendar.**
- Silent if there are no gaps — no spam on healthy weeks.
- Manual trigger available with a `days` input.

### `canary.py` (workflow: `Schema Canary`)
- Runs daily 9 AM PT.
- Scrapes Mindbody and counts classes per day.
- Trips an alert if:
  - Any weekday returns < 5 total classes (Tice Creek normally has 10+) → suggests scraper broken or Mindbody schema changed
  - The whole week returns < 5 Beth-preference matches → suggests `include_classes` filter is out of sync with current Mindbody class names
- This catches the silent-drop failure mode where workflows finish green but scrape nothing useful.

### `booking_probe.py` (workflow: `Booking Window Probe`)
- Manual trigger only.
- Polls Mindbody for day-7 and day-8 classes and logs every (Reserved, Open) count with a timestamp.
- Run frequently across a 24h period to determine **exactly when Tice Creek's booking window opens** for each class.
- Output uploaded as a workflow artifact — review timestamps to find the moment classes flip from "not yet on schedule" to "Reserve Now (10 Open)".
- Use this data to tighten the auto-book cron schedule so Beth wins booking races.

---

## Booking races (waitlist vs. confirmed)

**Symptom:** Beth ends up on waitlists for popular classes (Aquacise, Functional Fitness, Pilates Mat).

**Diagnosis:** Classes hit capacity within minutes/hours of opening. Auto-book runs hit the booking window AFTER it has already filled.

**Why this is hard:** We don't authoritatively know when Tice Creek opens each class. Plausible rules:
- "Midnight 7 days before" (cron at 12:00–12:30 AM PT covers this)
- "Exactly 7 days before to the second" (need cron at the class's actual time on day-7)
- "6:30 AM 7 days before" (cron at 5–7 AM PT covers this)

**Current strategy:** Cast a wide net — 18+ runs/day with flurries at 12 AM and 5–7 AM PT. Plus a daytime sweep every 2 hours for cancellations.

**Next step:** Connor manually triggers `Booking Window Probe` workflow. Review the artifact — when did each class become bookable? Then tighten cron to fire at +/- 1 minute around the actual opening.

**Mindbody UI quirk (fixed Apr 2026):** The auto-booker used to log "Booking may have failed" when it didn't see a separate "Confirm" button after clicking Reserve. In fact, Mindbody now does single-click reservation/waitlist with a redirect to the dashboard. The new code recognizes the dashboard redirect as success.

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

## Calendar safety rules (Oct 2026)

See the `gcal_sync.py` docstring. In short: only touch our own ID prefixes; a source that failed (None) leaves its events alone; never delete started events; class deletions are scoped to the dates the widget covered; circuit breaker refuses to delete most of a category when the scrape came back thin. The old `auto_book.py` deleted ANY event whose title contained a class keyword ("chi", "mat", "water"...). Never reintroduce keyword-based deletion.

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
  - Exception: `scraper.py` exits 1 when a whole source is down (after syncing everything that worked). A dead source going unnoticed is how Beth went five months without classes.
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
| No classes on calendar May 12 – Oct 3 2026 | Tice Creek moved to Mindbody branded-web widget; Playwright scraper found 0 classes and auto-book couldn't find the login form. Alerts had been turned off, so nobody noticed | `tice_schedule.py` (plain HTTP, no login); sync exits non-zero when a source fails |
| No movies Jun 30 – Oct 3 2026 | 0 fitness classes made `scraper.py` exit before reaching movies; then MyRossmoor changed its data format (`movies=[...]` instead of `months=[...]`) | Sources sync independently; parser handles both formats |
| All workflows stopped Aug 9 2026 | GitHub disables scheduled workflows after 60 days without commits | Keep-alive step in `sync.yml` + `check-email.yml` |
| Forwarded appointments never reached the calendar (Mar–Oct 2026) | `gcal_sync` stopped syncing the class list (where email "add" events were merged) when auto-book took over fitness | Appointments are their own category (`be0cd4`) |
| Forwarded emails dismissed as "not relevant" (Oct 2026) | Model `claude-sonnet-4-20250514` retired (404); classifier treated API errors as "not relevant", and emails were marked read on fetch | Model → `claude-sonnet-5-5`; fetch with BODY.PEEK, mark read only after handling; API errors leave the email unread and exit 1. Recover with `check-email.yml` input `reprocess_since` |
| Scrape failure would wipe movies/concerts | Failed scrape returned `[]`, sync deleted everything not in it | Failures return None; circuit breaker |

---

## How to answer common questions

- **"Is the calendar up to date?"** → Trigger `dump-calendar.yml`, read its log, show the next 7 days grouped by day with ✅/⏳ markers.
- **"List activities for the next week"** → Same as above.
- **"Add this appointment"** (often with a screenshot) → Use `add-event.yml` workflow with form fields, OR commit to `manual_events.json` if it's a recurring thing.
- **"Remove X"** → Use `remove-events.yml` with the keyword.
- **"Why isn't class X on her calendar?"** → Check the latest `sync.yml` log ("Beth's classes this period"). Could be: not in `include_classes`, before `earliest_hour`, cancelled, or beyond the widget's 7-day window.

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
- **Scrapers can be tested locally** (`python tice_schedule.py` prints the parsed week; no secrets needed). Calendar writes need the GitHub secrets, so test those via Actions.
- **When in doubt, alert don't crash.** Connor would rather get an email than have the calendar go dark.
- **Keep this file updated.** When you make a non-obvious decision or hit a gotcha, add a row to the relevant section.
