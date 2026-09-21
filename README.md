# Pressure Watch v1

Watches barometric pressure at any number of locations and pushes a phone
notification the night before a sharp pressure drop, with a link to a chart
of the full drop.

## How it works

- The user list (name + location per person) is the source of truth. It
  lives in the **`USERS_JSON` secret**, not in a file in the repo — see
  "Keeping who's-watched private" below for why.
- Each person gets their **own personal ntfy topic**, which they subscribe
  to **once, ever**. If they move, you just edit their `location` in
  `USERS_JSON` — their topic never changes, so there's nothing for them to
  do in the ntfy app. (The tradeoff: adding a brand-new person does require
  that one-time subscribe, since ntfy has no way to provision a
  subscription remotely.)
- A GitHub Actions workflow runs **hourly**. Each run, for every *unique*
  location across all users, it checks whether it's currently ~8 PM in
  *that location's own timezone*. If so, it looks at the pressure forecast
  for "tomorrow" (defined as 4:00 AM → the following 4:00 AM, in that
  location's local time) and flags any stretch where pressure drops more
  than **0.06 inHg** within a rolling 5-hour window. Overlapping flagged
  windows are merged into one event so a single continuous drop isn't
  reported multiple times.
  - A drop that starts before tomorrow's 4:00 AM cutoff but keeps going past
    it is still included (only the *start* time has to fall in the window).
  - Multiple distinct drops on the same day are all reported together, in
    one notification.
- The weather check runs once per unique location (not once per person), so
  if three people live in the same city it's only checked once — but each
  of them still gets their own notification, sent individually to their own
  topic. Notification text mentions only the location, never a name.
- Each qualifying location/day gets a small chart (rendered with matplotlib)
  and a section on a shared HTML page for that day, published via GitHub
  Pages. The push notification (via [ntfy](https://ntfy.sh)) links straight
  to that location's section.
- A dedup log (`state/sent_log.json`) makes sure each *person* only gets
  notified once per day, even though the check runs hourly.

## Keeping who's-watched private

GitHub Pages on the free tier requires a **public** repo. To avoid
publishing who's being watched and where they live, neither the user list
nor the ntfy topic names are committed as files — both live in GitHub
secrets instead, which the workflow passes in as environment variables at
run time:
- `USERS_JSON` — the whole list of people + locations
- `USER_TOPICS` — the name → topic mapping

`users.example.json` in the repo is just a placeholder showing the expected
shape; it isn't read when `USERS_JSON` is set. (A real `users.json` also
works as a local-testing fallback if you ever run the script outside
Actions — it's git-ignored so it won't get committed by accident.)

One thing this *doesn't* hide: the public alert pages on GitHub Pages still
show location names and drop details (never names) for whichever locations
get checked. If you want that hidden too, the repo itself would need to be
private, which requires GitHub Pro for Pages to keep working.

## One-time setup

### 1. Push this repo to GitHub and enable Pages
Repo Settings → Pages → set source to "Deploy from a branch", branch `main`
(or wherever this lives), folder `/docs`. Note the resulting URL (something
like `https://<you>.github.io/<repo>`).

### 2. Set up ntfy topics — one per person
For each person you want to notify:
- Pick a long, random, hard-to-guess topic name for them (ntfy is a public
  pub/sub system — anyone who knows a topic name can subscribe to it, so
  don't use anything guessable like their name).
- Have them install the [ntfy app](https://ntfy.sh/) (iOS/Android) or use
  the web client, and subscribe to that topic name on `ntfy.sh`.
- This is a **one-time** step per person. They never need to touch the app
  again, even if their location changes later.

### 3. Set repo secrets and variables
Repo Settings → Secrets and variables → Actions:

**Secrets:**
- `USERS_JSON` — the full list of people being watched, e.g.:
  ```json
  [
    { "name": "Alex", "location": "Brooklyn, NY, US" },
    { "name": "Sam", "location": "Tokyo, Japan" }
  ]
  ```
- `USER_TOPICS` — a JSON object mapping each person's name (must match
  `USERS_JSON` exactly) to the topic you picked for them in step 2, e.g.:
  ```json
  {"Alex": "pw-alex-8x7k2m9qz", "Sam": "pw-sam-q3n5j1rtv"}
  ```

Both are secrets (not files in the repo) because the repo is public —
committing either one anywhere public would defeat the point.

**Variables:**
- `PAGES_BASE_URL` — the GitHub Pages URL from step 1, e.g.
  `https://<you>.github.io/<repo>` (no trailing slash).
- `NTFY_SERVER` — optional, only needed if self-hosting ntfy later. Defaults
  to `https://ntfy.sh`.

### 4. Test it
Actions tab → "Pressure check" → "Run workflow". Leave `force` checked
(bypasses the 8PM-local gating and the dedup log) and optionally set
`only_user` to one person's name to limit the test. Check that:
- The run's logs show the location being geocoded and its resolved
  timezone.
- If there's a qualifying drop in the forecast, that person gets an ntfy
  notification and its link opens a chart on your Pages site.
- If you want to see the page/chart machinery exercised without waiting for
  a real drop, temporarily lower `DROP_THRESHOLD_INHG` in
  `scripts/check_pressure.py`, run once, then set it back.

## When someone moves

Edit their `location` in the `USERS_JSON` secret's value, save. That's it —
their topic (and their ntfy subscription) doesn't change, so there's
nothing for them to do.

## Adding a new person

Add them to `USERS_JSON`, pick a fresh random topic name and add it to
`USER_TOPICS`, then send them that topic name so they can do the one-time
ntfy subscribe.

## Files

```
users.example.json          placeholder showing the expected shape (not read if USERS_JSON is set)
scripts/check_pressure.py   the whole thing
state/geocode_cache.json    cached lat/lon/timezone per location (committed by CI)
state/sent_log.json         dedup log, one entry per person/day sent (committed by CI)
docs/                       GitHub Pages root
docs/alerts/<date>.html     shared alert page for that UTC date, all locations
docs/alerts/<date>.json     manifest backing that page (regenerated each run)
docs/alerts/img/*.png       per-event charts
.github/workflows/pressure_check.yml   the hourly job
```

## Known limitations / follow-ups
- Geocoding takes the first Open-Meteo match for whatever location string
  you use — if it's ambiguous, check the workflow logs after your first run
  to confirm it resolved to the right place (the resolved `display_name`
  and timezone are printed).
- Adding a brand-new person still requires their one-time ntfy subscribe —
  there's no way around that with ntfy's account-less model.
- The public alert pages still show location names/drop details, even
  though the user list and topics are private (see "Keeping who's-watched
  private" above).
- GitHub's schedule trigger isn't always perfectly reliable (a known
  Actions quirk from v0). If runs seem to silently stop firing, the fallback
  is an external cron service (e.g. cron-job.org) hitting the Actions API
  via `workflow_dispatch` instead of relying on the native schedule.
- Threshold, window length, and the "day starts at 4 AM" rule are constants
  at the top of `scripts/check_pressure.py` if you want to tune them.
