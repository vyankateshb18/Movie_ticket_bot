#!/usr/bin/env python3
"""BookMyShow rolling date watcher (HTML-page mode; no internal API URL needed)."""

import json
import os
import re
import sys
import time
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode

import requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.getenv("CONFIG_PATH", ROOT / "config.json"))
STATE_PATH = Path(os.getenv("STATE_PATH", ROOT / "state.json"))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Referer": "https://in.bookmyshow.com/",
}

def read_json(path, default):
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as f:
        return json.load(f)

def write_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(path)

def load_config():
    cfg = read_json(CONFIG_PATH, {})
    for env, key in {
        "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
        "TELEGRAM_CHAT_ID": "telegram_chat_id",
        "SCRAPERAPI_KEY": "scraperapi_key",
        "BMS_LANGUAGE": "language",
    }.items():
        if os.getenv(env):
            cfg[key] = os.environ[env]

    required = ["url_template", "movie", "venue_codes",
                "telegram_bot_token", "telegram_chat_id"]
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        sys.exit("Missing config fields: " + ", ".join(missing))

    cfg["venue_codes"] = [str(x).strip().upper() for x in cfg["venue_codes"]]
    cfg["language"] = str(cfg.get("language", "Hindi")).strip()
    cfg["window_days"] = max(1, int(cfg.get("window_days", 7)))
    cfg["min_date_references"] = max(1, int(cfg.get("min_date_references", 10)))
    cfg["request_timeout"] = int(cfg.get("request_timeout", 60))
    return cfg

def build_url(cfg, date_code):
    return cfg["url_template"].format(date=date_code)

def fetch_page(cfg, url):
    headers = dict(HEADERS)
    headers.update(cfg.get("headers", {}))
    key = cfg.get("scraperapi_key") or os.getenv("SCRAPERAPI_KEY")
    if key:
        response = requests.get(
            "https://api.scraperapi.com/",
            params={"api_key": key, "country_code": "in", "url": url},
            headers=headers,
            timeout=cfg["request_timeout"],
        )
    else:
        session = requests.Session()
        session.headers.update(headers)
        try:
            session.get("https://in.bookmyshow.com/", timeout=25)
        except requests.RequestException:
            pass
        response = session.get(url, timeout=cfg["request_timeout"])
    response.raise_for_status()
    return response.text

def inspect_page(html, requested_date, venue_codes, min_refs):
    """Conservative HTML heuristic: target date must dominate date tokens,
    occur repeatedly, and at least one configured venue code must be present."""
    date_counts = Counter(re.findall(r"20\d{6}", html))
    target_count = date_counts.get(requested_date, 0)
    other_max = max((n for d, n in date_counts.items() if d != requested_date), default=0)
    matching_venues = [code for code in venue_codes if re.search(
        rf"(?<![A-Z0-9]){re.escape(code)}(?![A-Z0-9])", html, re.I
    )]
    is_open = (
        target_count >= min_refs
        and target_count > other_max
        and bool(matching_venues)
    )
    return is_open, target_count, matching_venues, date_counts.most_common(3)

def send_telegram(cfg, message):
    endpoint = f"https://api.telegram.org/bot{cfg['telegram_bot_token']}/sendMessage"
    response = requests.post(
        endpoint,
        json={"chat_id": cfg["telegram_chat_id"], "text": message,
              "disable_web_page_preview": False},
        timeout=30,
    )
    response.raise_for_status()

def main():
    cfg = load_config()
    state = read_json(STATE_PATH, {"notified_dates": {}})
    notified = state.setdefault("notified_dates", {})
    # Fixed anchor date: never scan before it. After that date, the
    # monitoring window rolls forward from the current date.
    anchor = date.fromisoformat(cfg.get("start_date", date.today().isoformat()))
    start = max(date.today(), anchor)
    errors = 0
    alerts = 0

    for offset in range(cfg["window_days"]):
        day = start + timedelta(days=offset)
        date_code = day.strftime("%Y%m%d")
        url = build_url(cfg, date_code)
        try:
            html = fetch_page(cfg, url)
            opened, count, venues, top_dates = inspect_page(
                html, date_code, cfg["venue_codes"], cfg["min_date_references"]
            )
            print(f"{date_code}: date_refs={count}, venues={venues}, "
                  f"top_dates={top_dates}, opened={opened}")

            # One alert per date if any configured venue appears.
            if opened and date_code not in notified:
                venue_text = ", ".join(venues)
                msg = (
                    f"🎬 POSSIBLE BOOKING OPENED: {cfg['movie']}\n\n"
                    f"Language: {cfg['language']}\n"
                    f"Date: {day.strftime('%d-%m-%Y')}\n"
                    f"Matching venue code(s): {venue_text}\n\n"
                    f"BookMyShow: {url}\n\n"
                    f"Note: detected from the date and venue markers on the page. "
                    f"Please confirm showtimes on BookMyShow."
                )
                send_telegram(cfg, msg)
                notified[date_code] = int(time.time())
                alerts += 1
                print(f"Telegram alert sent for {date_code}")
        except requests.RequestException as exc:
            errors += 1
            print(f"{date_code}: request failed: {exc}")
        except Exception as exc:
            errors += 1
            print(f"{date_code}: error: {exc}")

    # Retain recent notification keys only.
    cutoff = int(time.time()) - 45 * 86400
    state["notified_dates"] = {
        k: v for k, v in notified.items()
        if isinstance(v, int) and v >= cutoff
    }
    state["checked_at"] = int(time.time())
    write_json(STATE_PATH, state)
    print(f"Done. alerts={alerts}, errors={errors}")
    return 1 if errors and not alerts else 0

if __name__ == "__main__":
    raise SystemExit(main())
