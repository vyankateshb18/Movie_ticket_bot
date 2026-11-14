#!/usr/bin/env python3
"""
BookMyShow showtime watcher for selected theatres/formats/languages.

Expected input: the JSON response from the BookMyShow showtimes API (not the
HTML page). Configure api_url_template with the exact request URL copied from
DevTools > Network > Fetch/XHR. It may contain {date} and {language}.

A notification is sent only when:
  - event code matches,
  - response language matches the configured language (when present),
  - venue code matches,
  - session date is in the rolling window,
  - session format matches (e.g. BIGPIX),
  - at least one seat category is AVAILABLE / ALMOST FULL / FILLING FAST.

The uploaded sample showed the relevant fields under:
data.additionalData.language,
data.showtimeWidgets -> venue-card -> showtimesSections -> showtimes,
showtime.additionalData.{sessionId, showDateCode, showTime, attributes},
and customGestureCTA ... variableData.seatAvalibility.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", ROOT / "config.json"))
STATE_PATH = Path(os.environ.get("STATE_PATH", ROOT / "state.json"))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-IN,en;q=0.9",
    "Origin": "https://in.bookmyshow.com",
    "Referer": "https://in.bookmyshow.com/",
}

LANGUAGE_ALIASES = {
    "english": {"english", "en"},
    "hindi": {"hindi", "hi"},
    "tamil": {"tamil", "ta"},
    "telugu": {"telugu", "te"},
    "malayalam": {"malayalam", "ml"},
    "kannada": {"kannada", "kn"},
    "bengali": {"bengali", "bn"},
    "marathi": {"marathi", "mr"},
    "punjabi": {"punjabi", "pa"},
    "gujarati": {"gujarati", "gu"},
    "odia": {"odia", "oriya", "or"},
}

BOOKABLE_LABELS = {"AVAILABLE", "ALMOST FULL", "FILLING FAST"}


def read_json(path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(path)


def load_config():
    cfg = read_json(CONFIG_PATH, {})
    for env, key in {
        "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
        "TELEGRAM_CHAT_ID": "telegram_chat_id",
        "SCRAPERAPI_KEY": "scraperapi_key",
        "BMS_LANGUAGE": "language",
        "BMS_FORMAT": "format",
    }.items():
        if os.getenv(env):
            cfg[key] = os.environ[env]

    required = [
        "api_url_template", "event_code", "movie",
        "venue_codes", "format", "language",
        "telegram_bot_token", "telegram_chat_id",
    ]
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        sys.exit("Missing config fields: " + ", ".join(missing))

    if os.getenv("BMS_VENUE_CODES"):
        cfg["venue_codes"] = [
            part.strip().upper()
            for part in os.environ["BMS_VENUE_CODES"].split(",")
            if part.strip()
        ]
    cfg["venue_codes"] = [str(x).upper() for x in cfg["venue_codes"]]
    cfg["format"] = str(cfg["format"]).strip().upper()
    cfg["language"] = str(cfg["language"]).strip().lower()
    cfg["window_days"] = int(cfg.get("window_days", 7))
    cfg["lookahead_start_days"] = int(cfg.get("lookahead_start_days", 0))
    cfg["request_timeout"] = int(cfg.get("request_timeout", 60))
    return cfg


def send_telegram(cfg, message):
    url = f"https://api.telegram.org/bot{cfg['telegram_bot_token']}/sendMessage"
    response = requests.post(
        url,
        json={
            "chat_id": cfg["telegram_chat_id"],
            "text": message,
            "disable_web_page_preview": False,
        },
        timeout=30,
    )
    response.raise_for_status()


def make_url(cfg, date_code):
    return cfg["api_url_template"].format(
        date=date_code,
        language=quote(cfg["language"]),
        event_code=quote(str(cfg["event_code"])),
    )


def fetch_json(cfg, date_code):
    url = make_url(cfg, date_code)
    headers = dict(HEADERS)
    headers.update(cfg.get("headers", {}))

    scraper_key = cfg.get("scraperapi_key") or os.getenv("SCRAPERAPI_KEY")
    if scraper_key:
        response = requests.get(
            "https://api.scraperapi.com/",
            params={
                "api_key": scraper_key,
                "country_code": "in",
                "url": url,
            },
            headers=headers,
            timeout=cfg["request_timeout"],
        )
    else:
        response = requests.get(
            url, headers=headers, timeout=cfg["request_timeout"]
        )
    response.raise_for_status()
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(
            "Response was not JSON. Check api_url_template: it must be the "
            "showtimes Fetch/XHR endpoint, not the normal movie HTML page."
        ) from exc


def normalize_language(value):
    return re.sub(r"[^a-z]", "", str(value or "").lower())


def language_matches(response_data, wanted):
    """Validate the language reported by the API; do not infer from URL."""
    actual = normalize_language(response_data.get("additionalData", {}).get("language"))
    event_data = response_data.get("eventData") or {}
    event_lang = normalize_language(event_data.get("eventLang"))
    wanted_aliases = LANGUAGE_ALIASES.get(wanted, {wanted})
    wanted_aliases = {normalize_language(x) for x in wanted_aliases}

    # BMS commonly reports `additionalData.language` as e.g. "hindi".
    # If neither language field is supplied, fail closed rather than alerting
    # for a different language.
    candidates = {x for x in (actual, event_lang) if x}
    return bool(candidates.intersection(wanted_aliases)), actual or event_lang or "unknown"


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def seat_labels(session):
    labels = set()
    # The uploaded BMS response stores these inside customGestureCTA's
    # bottomSheetData widgets. Search only this session subtree.
    for node in walk(session.get("customGestureCTA", {})):
        variable = node.get("variableData") if isinstance(node, dict) else None
        if isinstance(variable, dict) and variable.get("seatAvalibility"):
            labels.add(str(variable["seatAvalibility"]).strip().upper())
    return labels


def get_session_format(session, additional):
    attrs = additional.get("attributes")
    if attrs:
        return str(attrs).strip().upper()
    if session.get("screenAttr"):
        return str(session["screenAttr"]).strip().upper()
    analytics = (session.get("cta") or {}).get("analytics") or {}
    return str(analytics.get("format") or "").strip().upper()


def parse_matching_sessions(payload, cfg, requested_date):
    root = payload.get("data", payload)
    if not isinstance(root, dict):
        return [], "invalid JSON structure"

    event_code = str(root.get("additionalData", {}).get("eventCode") or "")
    if event_code and event_code != str(cfg["event_code"]):
        return [], f"event code mismatch: {event_code}"

    lang_ok, actual_language = language_matches(root, cfg["language"])
    if not lang_ok:
        return [], f"language mismatch/unknown: requested={cfg['language']}, response={actual_language}"

    found = []
    for venue in walk(root.get("showtimeWidgets", [])):
        if venue.get("type") != "venue-card":
            continue

        venue_data = venue.get("additionalData") or {}
        venue_code = str(venue_data.get("venueCode") or "").upper()
        if venue_code not in cfg["venue_codes"]:
            continue

        venue_name = venue_data.get("venueName") or venue_code

        for session in walk(venue.get("showtimesSections", [])):
            additional = session.get("additionalData") or {}
            session_id = additional.get("sessionId")
            if not session_id:
                continue

            show_date = str(additional.get("showDateCode") or "")
            if show_date != requested_date:
                continue

            session_format = get_session_format(session, additional)
            if cfg["format"] not in session_format:
                continue

            labels = seat_labels(session)
            # Fail closed: only alert when BMS provides an explicit positive
            # seat-category state. SOLD OUT alone is not bookable.
            positive = sorted(labels.intersection(BOOKABLE_LABELS))
            if not positive:
                continue

            show_time = (
                additional.get("showTime")
                or session.get("title")
                or additional.get("showTimeCode")
                or "Time not supplied"
            )
            found.append({
                "date": show_date,
                "venue_code": venue_code,
                "venue_name": venue_name,
                "format": session_format,
                "session_id": str(session_id),
                "show_time": str(show_time),
                "seat_status": ", ".join(positive),
            })

    return found, None


def booking_url(cfg, date_code, venue_code):
    template = cfg.get("booking_url_template")
    if template:
        return template.format(date=date_code, venue_code=venue_code)
    # General movie date page fallback. User can set the theatre-specific
    # template after confirming the exact BMS route.
    return cfg.get("movie_url", "https://in.bookmyshow.com/") + "/" + date_code


def main():
    cfg = load_config()
    state = read_json(STATE_PATH, {"notified": {}})
    notified = state.setdefault("notified", {})

    today = datetime.now().date()
    start = today + timedelta(days=cfg["lookahead_start_days"])
    errors = 0
    new_alerts = 0

    for offset in range(cfg["window_days"]):
        date_code = (start + timedelta(days=offset)).strftime("%Y%m%d")
        try:
            payload = fetch_json(cfg, date_code)
            sessions, reason = parse_matching_sessions(payload, cfg, date_code)
            if reason:
                print(f"{date_code}: skipped - {reason}")
                continue

            print(f"{date_code}: {len(sessions)} matching bookable session(s)")
            for item in sessions:
                # A unique alert per date + venue + format + session.
                key = "|".join((
                    item["date"], item["venue_code"],
                    item["format"], item["session_id"],
                ))
                if key in notified:
                    continue

                link = booking_url(cfg, item["date"], item["venue_code"])
                message = (
                    f"🎬 BOOKING AVAILABLE: {cfg['movie']}\n\n"
                    f"Language: {cfg['language'].title()}\n"
                    f"Theatre: {item['venue_name']}\n"
                    f"Format: {item['format']}\n"
                    f"Date: {item['date'][6:8]}-{item['date'][4:6]}-{item['date'][:4]}\n"
                    f"Showtime: {item['show_time']}\n"
                    f"Seat status: {item['seat_status']}\n\n"
                    f"Book: {link}"
                )
                send_telegram(cfg, message)
                notified[key] = int(time.time())
                new_alerts += 1
                print(f"Telegram alert sent: {key}")

        except requests.RequestException as exc:
            errors += 1
            print(f"{date_code}: request failed: {exc}")
        except Exception as exc:
            errors += 1
            print(f"{date_code}: processing failed: {exc}")

    # Keep the state file bounded.
    cutoff = int(time.time()) - 60 * 60 * 24 * 30
    state["notified"] = {
        key: ts for key, ts in notified.items()
        if isinstance(ts, int) and ts >= cutoff
    }
    state["checked_at"] = int(time.time())
    write_json(STATE_PATH, state)

    print(f"Finished. New alerts={new_alerts}; errors={errors}")
    return 1 if errors and not new_alerts else 0


if __name__ == "__main__":
    raise SystemExit(main())
