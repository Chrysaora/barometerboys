#!/usr/bin/env python3
"""
Pressure Watch v1 — multi-user barometric pressure drop alerts.

The user list (name + location, one entry per person) is the source of
truth. It's normally supplied via the USERS_JSON secret (a JSON array) so
it's never committed to the repo — see the note below. Falls back to
reading users.json from disk if USERS_JSON isn't set, which is handy for
local testing.

Notifications go to a per-PERSON ntfy topic (see USER_TOPICS below), not a
per-location one — so a person's subscription never has to change when they
move. Moving is a one-line edit to their "location" field in the user list;
their topic (and their one-time subscribe in the ntfy app) stays the same
forever.

Per run:
  1. Collect the unique locations across all users. For each one: geocode it
     (Open-Meteo Geocoding API, cached) to get lat/lon + IANA timezone, and
     if it's currently ~8 PM local time there (unless --force), pull the
     hourly pressure forecast and scan rolling 5-hour windows for drops >
     DROP_THRESHOLD_INHG, merging overlapping windows into single "drop
     events". Keep only events that start within "tomorrow" (a day = 4:00 AM
     -> next 4:00 AM local) — an event that starts in that window still
     counts even if it runs past the following 4:00 AM. This work happens
     once per unique location, however many people live there.
  2. For each user whose location has qualifying events today: if a chart
     and alert-page section for that location don't exist yet for today,
     render them. Then, if this user hasn't already been notified today
     (dedup is per-person, not per-location, so two people in the same city
     both get pinged), push an ntfy notification to *their own* topic. The
     message text mentions only the location, never the person's name.

The mapping of user name -> topic name lives in the USER_TOPICS secret (a
JSON object). Both USER_TOPICS and the user list itself (USERS_JSON) stay
out of the repo entirely, because the repo has to be public (GitHub Pages
requires that on the free tier) -- so both who's being watched and their
topic names are only private if they're never committed anywhere public.

State on disk (committed back to the repo by the workflow):
  state/geocode_cache.json   location string -> {lat, lon, timezone, display_name}
  state/sent_log.json        "<user name>|<tomorrow's local date>" -> ISO timestamp sent
  docs/alerts/<utc-date>.json   manifest of today's page sections (one per location)
  docs/alerts/<utc-date>.html   rendered shared alert page (all locations)
  docs/alerts/img/*.png         per-event charts
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, date
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except ImportError:
    plt = None

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
USERS_FILE = ROOT / "users.json"
GEOCODE_CACHE_FILE = ROOT / "state" / "geocode_cache.json"
SENT_LOG_FILE = ROOT / "state" / "sent_log.json"
ALERTS_DIR = ROOT / "docs" / "alerts"
IMG_DIR = ALERTS_DIR / "img"
PAGES_BASE_URL = os.environ.get("PAGES_BASE_URL", "").rstrip("/")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")


def _load_json_env(var_name: str, default):
    raw = os.environ.get(var_name, "")
    if not raw.strip():
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"[!] {var_name} is not valid JSON: {exc}")
        return default


# USER_TOPICS: JSON object mapping user name -> ntfy topic name, e.g.
# {"Alex": "bb-alerts-CZhPCqpEA9oVeyf6"}. Lives in a GitHub secret, not in
# the user list, so topic names never end up in the (public) repo. Each
# person subscribes to their topic ONCE, ever -- it never changes even if
# their location does.
USER_TOPICS = _load_json_env("USER_TOPICS", {})

DROP_THRESHOLD_INHG = 0.06
WINDOW_HOURS = 5
FORECAST_DAYS = 3
TARGET_LOCAL_HOUR = 20  # 8 PM
DAY_START_HOUR = 4  # a "day" runs 4:00 AM -> next day's 4:00 AM

HPA_TO_INHG = 0.0295299830714

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def load_json(path: Path, default):
    if not path.exists():
        return default
    with open(path, "r") as f:
        return json.load(f)


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")


def slugify(text: str) -> str:
    out = []
    for ch in text.lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in (" ", ",", "-", "_", "/"):
            out.append("-")
    slug = "".join(out)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")


def fmt_time(dt: datetime) -> str:
    s = dt.strftime("%I:%M %p").lstrip("0")
    return s


def fmt_time_with_date_if_needed(dt: datetime, reference_date: date) -> str:
    s = fmt_time(dt)
    if dt.date() != reference_date:
        s += f" ({fmt_date(dt.date())})"
    return s


def fmt_date(d: date) -> str:
    day = d.day
    return f"{d.strftime('%b')} {day}"


# ---------------------------------------------------------------------------
# Geocoding
# ---------------------------------------------------------------------------

def _geocode_query(query: str):
    """One attempt against the Open-Meteo geocoding API. Returns the top
    result dict, or None if there were no matches."""
    resp = requests.get(
        GEOCODE_URL,
        params={"name": query, "count": 1, "language": "en", "format": "json"},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    results = data.get("results") or []
    return results[0] if results else None


def geocode_location(location: str, cache: dict) -> dict:
    if location in cache:
        return cache[location]

    # Open-Meteo's geocoding search is inconsistent about "City, Region,
    # Country" strings -- some match fine as-is (e.g. "Brooklyn, NY, US"),
    # others return zero results with the qualifiers attached (e.g.
    # "Amsterdam, Netherlands" finds nothing, but "Amsterdam" alone finds
    # it immediately). So: try the full string first, and if that comes up
    # empty, retry with progressively fewer trailing ", Region"/", Country"
    # segments stripped off, down to just the city name.
    parts_to_try = [p.strip() for p in location.split(",")]
    queries = [location] + [
        ", ".join(parts_to_try[:i]) for i in range(len(parts_to_try) - 1, 0, -1)
    ]

    r = None
    for query in queries:
        r = _geocode_query(query)
        if r:
            if query != location:
                print(f"  [i] {location!r} found no direct match; used "
                      f"simplified query {query!r} instead")
            break

    if not r:
        raise ValueError(f"No geocoding match for location: {location!r}")
    parts = [r.get("name")]
    admin1 = r.get("admin1")
    country_code = r.get("country_code")
    if admin1:
        parts.append(admin1)
    if country_code:
        parts.append(country_code)
    display_name = ", ".join(p for p in parts if p)

    entry = {
        "lat": r["latitude"],
        "lon": r["longitude"],
        "timezone": r["timezone"],
        "display_name": display_name,
    }
    cache[location] = entry
    return entry


# ---------------------------------------------------------------------------
# Forecast + drop detection
# ---------------------------------------------------------------------------

def fetch_hourly_pressure(lat: float, lon: float, tz: str):
    resp = requests.get(
        FORECAST_URL,
        params={
            "latitude": lat,
            "longitude": lon,
            "hourly": "surface_pressure",
            "timezone": tz,
            "forecast_days": FORECAST_DAYS,
        },
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    times = data["hourly"]["time"]
    pressures_hpa = data["hourly"]["surface_pressure"]

    series = []
    for t, p in zip(times, pressures_hpa):
        if p is None:
            continue
        dt = datetime.fromisoformat(t)
        series.append((dt, p * HPA_TO_INHG))
    return series


def find_drop_events(series):
    """Scan rolling WINDOW_HOURS windows for drops > threshold, then merge
    overlapping/adjacent flagged windows into single events covering the
    full span of the drop."""
    flagged = []
    n = len(series)
    for i in range(n - WINDOW_HOURS):
        start_dt, start_p = series[i]
        end_dt, end_p = series[i + WINDOW_HOURS]
        drop = start_p - end_p
        if drop > DROP_THRESHOLD_INHG:
            flagged.append((i, i + WINDOW_HOURS))

    if not flagged:
        return []

    # merge overlapping/adjacent index ranges
    flagged.sort()
    clusters = [list(flagged[0])]
    for s, e in flagged[1:]:
        if s <= clusters[-1][1]:
            clusters[-1][1] = max(clusters[-1][1], e)
        else:
            clusters.append([s, e])

    events = []
    for s_idx, e_idx in clusters:
        start_dt, start_p = series[s_idx]
        end_dt, end_p = series[e_idx]
        drop = start_p - end_p
        if drop > DROP_THRESHOLD_INHG:
            events.append({
                "start": start_dt,
                "end": end_dt,
                "drop_inhg": round(drop, 3),
                "series_slice": series[max(0, s_idx - 2): min(len(series), e_idx + 3)],
            })
    return events


def day_window(local_now: datetime):
    """Return (window_start, window_end, tomorrow_date) for the 4AM-4AM day
    that starts tomorrow, relative to local_now."""
    today = local_now.date()
    if local_now.hour < DAY_START_HOUR:
        today = today - timedelta(days=1)
    tomorrow = today + timedelta(days=1)
    tz = local_now.tzinfo
    window_start = datetime(tomorrow.year, tomorrow.month, tomorrow.day, DAY_START_HOUR, tzinfo=tz)
    window_end = window_start + timedelta(days=1)
    return window_start, window_end, tomorrow


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------

def format_message(events, tomorrow_date: date) -> str:
    date_str = fmt_date(tomorrow_date)
    if len(events) == 1:
        e = events[0]
        start_s = fmt_time_with_date_if_needed(e["start"], tomorrow_date)
        end_s = fmt_time_with_date_if_needed(e["end"], tomorrow_date)
        return (
            f"Tomorrow ({date_str}) there will be a sharp pressure drop of "
            f"{e['drop_inhg']:.2f} between {start_s} and {end_s}"
        )
    else:
        lines = [f"Tomorrow ({date_str}) there will be {len(events)} sharp pressure drops:"]
        for e in events:
            start_s = fmt_time_with_date_if_needed(e["start"], tomorrow_date)
            end_s = fmt_time_with_date_if_needed(e["end"], tomorrow_date)
            lines.append(f"- {e['drop_inhg']:.2f} from {start_s} to {end_s}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Chart rendering
# ---------------------------------------------------------------------------

def render_chart(location_display: str, events, tomorrow_date: date, run_utc_date: str) -> str:
    """Render a PNG chart covering the full span of all of tomorrow's events
    for this location. Returns the relative image path (from docs/)."""
    if plt is None:
        return ""

    all_points = []
    for e in events:
        all_points.extend(e["series_slice"])
    all_points = sorted(set(all_points), key=lambda p: p[0])

    xs = [p[0] for p in all_points]
    ys = [p[1] for p in all_points]

    fig, ax = plt.subplots(figsize=(7, 3.2), dpi=150)
    ax.plot(xs, ys, color="#2b6cb0", linewidth=2, marker="o", markersize=3)

    for e in events:
        ax.axvspan(e["start"], e["end"], color="#e53e3e", alpha=0.15)
        mid = e["start"] + (e["end"] - e["start"]) / 2
        y_top = max(p[1] for p in all_points)
        ax.annotate(f"-{e['drop_inhg']:.2f} inHg", xy=(mid, y_top),
                    ha="center", va="bottom", fontsize=9, color="#c53030")

    ax.set_title(f"{location_display} — pressure, {fmt_date(tomorrow_date)}")
    ax.set_ylabel("inHg")
    fig.autofmt_xdate()
    fig.tight_layout()

    IMG_DIR.mkdir(parents=True, exist_ok=True)
    slug = slugify(location_display)
    img_name = f"{slug}-{run_utc_date}.png"
    img_path = IMG_DIR / img_name
    fig.savefig(img_path)
    plt.close(fig)

    return f"img/{img_name}"


# ---------------------------------------------------------------------------
# Shared alert page (manifest-driven, regenerated each run)
# ---------------------------------------------------------------------------

def update_alert_page(run_utc_date: str, location_display: str, location_slug: str,
                       message: str, img_rel_path: str, tomorrow_date: date):
    manifest_path = ALERTS_DIR / f"{run_utc_date}.json"
    manifest = load_json(manifest_path, {"date": run_utc_date, "sections": {}})

    manifest["sections"][location_slug] = {
        "location": location_display,
        "message": message,
        "image": img_rel_path,
        "date": fmt_date(tomorrow_date),
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }

    save_json(manifest_path, manifest)
    render_alert_page_html(manifest)


def render_alert_page_html(manifest: dict):
    html_path = ALERTS_DIR / f"{manifest['date']}.html"

    sections_html = []
    for slug, sec in sorted(manifest["sections"].items()):
        img_tag = f'<img src="{sec["image"]}" alt="Pressure chart for {sec["location"]}">' if sec.get("image") else ""
        body = sec["message"].replace("\n", "<br>")
        sections_html.append(f"""
    <section id="{slug}" class="alert-section">
      <h2>{sec['location']}</h2>
      <p class="message">{body}</p>
      {img_tag}
    </section>""")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pressure Watch — {manifest['date']}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
          max-width: 640px; margin: 2rem auto; padding: 0 1rem; color: #1a202c; }}
  h1 {{ font-size: 1.4rem; }}
  .alert-section {{ border-top: 1px solid #e2e8f0; padding: 1.25rem 0; }}
  .alert-section h2 {{ font-size: 1.1rem; margin-bottom: 0.25rem; }}
  .message {{ white-space: pre-line; color: #2d3748; }}
  img {{ max-width: 100%; border-radius: 8px; margin-top: 0.5rem; }}
</style>
</head>
<body>
  <h1>Pressure Watch alerts — {manifest['date']}</h1>
  {"".join(sections_html)}
</body>
</html>
"""
    html_path.parent.mkdir(parents=True, exist_ok=True)
    with open(html_path, "w") as f:
        f.write(html)


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------

def send_ntfy(message: str, click_url: str, location_display: str, topic: str):
    if not topic:
        print("  [!] No ntfy topic configured for this user (check the "
              "USER_TOPICS secret), skipping send. Message would have been:")
        print(message)
        return

    headers = {
        "Title": f"Pressure drop - {location_display}".encode("utf-8"),
        "Priority": "high",
    }
    if click_url:
        headers["Click"] = click_url.encode("utf-8")

    resp = requests.post(
        f"{NTFY_SERVER}/{topic}",
        data=message.encode("utf-8"),
        headers=headers,
        timeout=20,
    )
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true",
                         help="Bypass the 8PM-local gating and dedup check.")
    parser.add_argument("--only-user", default=None,
                         help="Only process this one user (name must match exactly).")
    parser.add_argument("--dry-run", action="store_true",
                         help="Do everything except actually POST to ntfy.")
    args = parser.parse_args()

    # USERS_JSON (a secret, containing the whole users array) takes priority
    # over users.json on disk, so in production the repo never has to carry
    # who's being watched or where they live. The file is only a fallback
    # for local testing without secrets set up.
    users = _load_json_env("USERS_JSON", None)
    if users is None:
        users = load_json(USERS_FILE, [])
    if args.only_user:
        users = [u for u in users if u["name"] == args.only_user]

    unique_locations = sorted({u["location"] for u in users})

    geocode_cache = load_json(GEOCODE_CACHE_FILE, {})
    sent_log = load_json(SENT_LOG_FILE, {})

    run_utc_date = datetime.utcnow().strftime("%Y-%m-%d")
    any_state_changed = False

    # --- Phase 1: check each unique location once, regardless of how many
    # users live there. Result per location: None if there's nothing to
    # report (or the location couldn't be checked yet), else the computed
    # message/chart/tomorrow_date ready to hand to each user there.
    location_results = {}

    for location in unique_locations:
        print(f"== {location} ==")
        try:
            geo = geocode_location(location, geocode_cache)
        except Exception as exc:
            print(f"  [!] geocoding failed: {exc}")
            location_results[location] = None
            continue
        any_state_changed = True  # cache may have been updated

        tz = ZoneInfo(geo["timezone"])
        local_now = datetime.now(tz)
        print(f"  local time: {local_now.strftime('%Y-%m-%d %H:%M %Z')}")

        if not args.force and local_now.hour != TARGET_LOCAL_HOUR:
            print(f"  not 8 PM local ({TARGET_LOCAL_HOUR}:00), skipping")
            location_results[location] = None
            continue

        window_start, window_end, tomorrow_date = day_window(local_now)

        try:
            series = fetch_hourly_pressure(geo["lat"], geo["lon"], geo["timezone"])
        except Exception as exc:
            print(f"  [!] forecast fetch failed: {exc}")
            location_results[location] = None
            continue

        all_events = find_drop_events(series)
        events = [e for e in all_events if window_start <= e["start"] < window_end]

        if not events:
            print("  no qualifying drops for tomorrow")
            location_results[location] = None
            continue

        events.sort(key=lambda e: e["start"])
        message = format_message(events, tomorrow_date)
        print(f"  ALERT:\n{message}")

        location_slug = slugify(geo["display_name"])
        img_rel_path = render_chart(geo["display_name"], events, tomorrow_date, run_utc_date)

        update_alert_page(run_utc_date, geo["display_name"], location_slug,
                           message, img_rel_path, tomorrow_date)

        click_url = ""
        if PAGES_BASE_URL:
            click_url = f"{PAGES_BASE_URL}/alerts/{run_utc_date}.html#{location_slug}"

        location_results[location] = {
            "message": message,
            "click_url": click_url,
            "display_name": geo["display_name"],
            "tomorrow_date": tomorrow_date,
        }

    # --- Phase 2: fan out to each user whose location has something to
    # report, deduping per person (not per location) and sending to their
    # own personal topic.
    for user in users:
        name = user["name"]
        location = user["location"]
        result = location_results.get(location)
        if result is None:
            continue

        log_key = f"{name}|{result['tomorrow_date'].isoformat()}"
        if not args.force and log_key in sent_log:
            print(f"  {name}: already sent for {result['tomorrow_date'].isoformat()}, skipping")
            continue

        topic = USER_TOPICS.get(name)
        if not topic:
            print(f"  [!] no ntfy topic configured for user {name!r} in "
                  f"USER_TOPICS secret, skipping (add one to send this person alerts)")
            continue

        if not args.dry_run:
            send_ntfy(result["message"], result["click_url"], result["display_name"], topic)
            print(f"  {name}: sent")
        else:
            print(f"  {name}: [dry-run] would POST to ntfy")

        sent_log[log_key] = datetime.utcnow().isoformat() + "Z"
        any_state_changed = True

    if any_state_changed:
        save_json(GEOCODE_CACHE_FILE, geocode_cache)
        save_json(SENT_LOG_FILE, sent_log)


if __name__ == "__main__":
    main()
