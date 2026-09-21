# Pressure Watch v1

Watches barometric pressure at any number of locations and pushes a phone
notification the night before a sharp pressure drop, with a link to a chart
of the full drop.

## How it works

- `locations.json` lists the places to watch (city/region/country strings,
  geocoded via Open-Meteo — works worldwide, not just US zip codes).
- `users.json` is just your own reference for who cares about which
  location; the check itself runs once per unique location, and
  notifications never mention names — only the location.
- A GitHub Actions workflow runs **hourly**. Each run, for each location, it
  checks whether it's currently ~8 PM in *that location's own timezone*. If
  so, it looks at the pressure forecast for "tomorrow" (defined as 4:00 AM →
  the following 4:00 AM, in that location's local time) and flags any
  stretch where pressure drops more than **0.06 inHg** within a rolling
  5-hour window. Overlapping flagged windows are merged into one event so a
  single continuous drop isn't reported multiple times.
  - A drop that starts before tomorrow's 4:00 AM cutoff but keeps going past
    it is still included (only the *start* time has to fall in the window).
  - Multiple distinct drops on the same day are all reported together, in
    one notification.
- Each qualifying location/day gets a small chart (rendered with matplotlib)
  and a section on a shared HTML page for that day, published via GitHub
  Pages. The push notification (via [ntfy](https://ntfy.sh)) links straight
  to that location's section.
- A dedup log (`state/sent_log.json`) makes sure each location only gets
  notified once per day, even though the check runs hourly.

## One-time setup

### 1. Push this repo to GitHub and enable Pages
Repo Settings → Pages → set source to "Deploy from a branch", branch `main`
(or wherever this lives), folder `/docs`. Note the resulting URL (something
like `https://<you>.github.io/<repo>`).

### 2. Pick an ntfy topic and subscribe to it
Pick a long, random, hard-to-guess topic name (it's a public pub/sub
system — anyone who knows the topic name can subscribe). Then:
- Install the [ntfy app](https://ntfy.sh/) (iOS/Android) or use the web
  client, and subscribe to that topic name on `ntfy.sh`.
- Everyone you want notified subscribes to the same topic.

### 3. Set repo secrets and variables
Repo Settings → Secrets and variables → Actions:

**Secrets:**
- `NTFY_TOPIC` — the topic name you picked in step 2.

**Variables:**
- `PAGES_BASE_URL` — the GitHub Pages URL from step 1, e.g.
  `https://<you>.github.io/<repo>` (no trailing slash).
- `NTFY_SERVER` — optional, only needed if self-hosting ntfy later. Defaults
  to `https://ntfy.sh`.

### 4. Edit `locations.json`
Add one entry per place to watch:
```json
[
  { "location": "Brooklyn, NY, US" },
  { "location": "Tokyo, Japan" }
]
```
Use a city name plus enough region/country to disambiguate (there are
multiple "Springfield"s, etc). Optionally list who's in each location in
`users.json` for your own bookkeeping — it isn't read by the script's alert
logic and never appears in notifications.

### 5. Test it
Actions tab → "Pressure check" → "Run workflow". Leave `force` checked
(bypasses the 8PM-local gating and the dedup log) and optionally set `only`
to a single location to limit the test. Check that:
- The run's logs show the location being geocoded and its resolved
  timezone.
- If there's a qualifying drop in the forecast, an ntfy notification
  arrives and its link opens a chart on your Pages site.
- If you want to see the page/chart machinery exercised without waiting for
  a real drop, temporarily lower `DROP_THRESHOLD_INHG` in
  `scripts/check_pressure.py`, run once, then set it back.

## Files

```
locations.json              locations to watch
users.json                  who's where (reference only, not read by the alert logic)
scripts/check_pressure.py   the whole thing
state/geocode_cache.json    cached lat/lon/timezone per location (committed by CI)
state/sent_log.json         dedup log, one entry per location/day sent (committed by CI)
docs/                       GitHub Pages root
docs/alerts/<date>.html     shared alert page for that UTC date, all locations
docs/alerts/<date>.json     manifest backing that page (regenerated each run)
docs/alerts/img/*.png       per-event charts
.github/workflows/pressure_check.yml   the hourly job
```

## Known limitations / follow-ups
- Geocoding takes the first Open-Meteo match for whatever string you put in
  `locations.json` — if a location string is ambiguous, check the workflow
  logs after your first run to confirm it resolved to the right place (the
  resolved `display_name` and timezone are printed).
- GitHub's schedule trigger isn't always perfectly reliable (a known
  Actions quirk from v0). If runs seem to silently stop firing, the fallback
  is an external cron service (e.g. cron-job.org) hitting the Actions API
  via `workflow_dispatch` instead of relying on the native schedule.
- Threshold, window length, and the "day starts at 4 AM" rule are constants
  at the top of `scripts/check_pressure.py` if you want to tune them.
