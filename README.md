# 🏋️ Tice Creek Fitness Calendar Sync

Auto-syncs the [Tice Creek Fitness Center](https://www.ticefitnesscenter.com/) class schedule to Google Calendar for Beth.

**Currently configured for:** Water exercises, Zumba, UJAM — afternoons only.

```
Tice Creek Website  →  Playwright Scraper  →  ICS File  →  GitHub Pages  →  Google Calendar
(Mindbody widgets)     (daily via GitHub       (hosted       (serves the      (auto-refreshes
                        Actions)                publicly)      .ics URL)        every ~12-24h)
```

---

## Quick Start

### 1. Install dependencies

```bash
pip install playwright pyyaml
playwright install chromium
```

### 2. Run discovery mode first (important!)

The schedule pages use Mindbody widgets that render via JavaScript. Run discovery mode to capture how the widget works on your machine:

```bash
python scraper.py --discover --no-headless
```

This opens a visible browser, loads both schedule pages, and dumps everything to `debug/`:
- **Screenshots** (`*.png`) — what the page looks like
- **HTML** (`*.html`) — full rendered DOM
- **Network logs** (`*_network.json`) — all API calls the widget makes
- **Parsed classes** (`all_classes.json`) — what the scraper extracted

If `all_classes.json` has data → you're good! Run normally:

```bash
python scraper.py
```

If it's empty → share the `debug/` folder with me and I'll tune the parser for the exact widget format.

### 3. Deploy to GitHub

```bash
git init && git add -A
git commit -m "Initial commit"
gh repo create tice-creek-calendar --public --push
```

Then enable **GitHub Pages**: Settings → Pages → Branch: `main`, Folder: `/docs` → Save.

### 4. Subscribe in Google Calendar

1. Open [Google Calendar](https://calendar.google.com)
2. Click **+** next to "Other calendars" → **"From URL"**
3. Paste: `https://YOUR_USERNAME.github.io/tice-creek-calendar/tice-creek-classes.ics`
4. Click **"Add calendar"**

Done! Classes appear on Beth's calendar and auto-update daily.

---

## Configuration

Edit `config.yaml` to change which classes appear:

```yaml
include_classes:
  - "aqua"      # matches "Aqua Fit", "Aqua Zumba", etc.
  - "water"     # matches "Water Aerobics", etc.
  - "zumba"     # matches "Zumba", "Aqua Zumba"
  - "ujam"      # matches "UJAM"

earliest_hour: 12  # afternoon only (noon+)
```

Push changes to `main` and the workflow re-runs automatically.

---

## How It Works

The scraper tries three strategies in order:

1. **Network interception** — Captures JSON API responses from the Mindbody widget
2. **DOM parsing** — Reads the rendered widget HTML elements
3. **Mindbody classic page** — Falls back to the server-rendered schedule

It runs daily at 6 AM Pacific via GitHub Actions, generates an ICS file, and commits it to the repo. GitHub Pages serves it as a static file that Google Calendar subscribes to.

---

## Files

```
├── scraper.py          # Main scraper (Playwright + ICS generation)
├── config.yaml         # Beth's class preferences
├── requirements.txt    # Python dependencies
├── docs/
│   ├── index.html      # Landing page with subscribe instructions
│   └── *.ics           # Generated calendar file
└── .github/workflows/
    └── sync.yml        # Daily GitHub Actions workflow
```

## Troubleshooting

**No classes extracted?** Run `python scraper.py --discover --no-headless` and check `debug/`.

**Calendar not updating?** Google refreshes subscribed calendars every 12-24h. Check the ICS file URL directly in your browser to verify it's current.

**GitHub Actions failing?** Check the Actions tab. Debug artifacts are uploaded on failure.
