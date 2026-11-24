"""BookMyShow discovery, availability, comparison, and one-shot services."""

import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import requests

from theatre import theatre_location

CITY_SLUG = os.getenv("BMS_CITY", "hyderabad").strip().lower()
LISTING_URL = f"https://in.bookmyshow.com/explore/movies-{CITY_SLUG}"
STATE_FILE = "bms_state.json"
TELEGRAM_API_URL = "https://api.telegram.org/bot{}/sendMessage"

AVAIL_STATUS_MAP = {
    "0": ("SOLD OUT", "🔴"),
    "1": ("ALMOST FULL", "🟡"),
    "2": ("FILLING FAST", "🟠"),
    "3": ("AVAILABLE", "🟢"),
}
BOOKABLE_STATUSES = {"1", "2", "3"}
DATE_STYLE_MAP = {
    "date-selected": "BOOKABLE",
    "date-disabled": "NOT_OPEN",
    "date-default": "AVAILABLE",
}
DATE_STATUS_PRIORITY = {
    "UNKNOWN": 0,
    "NOT_OPEN": 1,
    "AVAILABLE": 2,
    "BOOKABLE": 3,
}
REGION_MAP = {
    "chennai": ("CHEN", "chennai", "13.056", "80.206", "tf3"),
    "mumbai": ("MUMBAI", "mumbai", "19.076", "72.878", "te7"),
    "delhi-ncr": ("NCR", "delhi-ncr", "28.613", "77.209", "ttn"),
    "delhi": ("NCR", "delhi-ncr", "28.613", "77.209", "ttn"),
    "bengaluru": ("BANG", "bengaluru", "12.972", "77.594", "tdr"),
    "bangalore": ("BANG", "bengaluru", "12.972", "77.594", "tdr"),
    "hyderabad": ("HYD", "hyderabad", "17.385", "78.487", "tep"),
    "kolkata": ("KOLK", "kolkata", "22.573", "88.364", "tun"),
    "pune": ("PUNE", "pune", "18.520", "73.856", "te2"),
    "kochi": ("KOCH", "kochi", "9.932", "76.267", "t9z"),
}
API_URL = (
    "https://in.bookmyshow.com/api/movies-data/v4/"
    "showtimes-by-event/primary-dynamic"
)
BMS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "x-app-code": "WEB",
    "x-location-selection": "manual",
    "x-lsid": "",
}


@dataclass
class CatInfo:
    name: str
    price: str
    status: str


@dataclass
class ShowInfo:
    venue_code: str
    venue_name: str
    session_id: str
    date_code: str
    time: str
    time_code: str
    screen_attr: str
    venue_location: str = ""
    categories: list[CatInfo] = field(default_factory=list)


@dataclass
class DateInfo:
    date_code: str
    status: str


class MovieLinkParser(HTMLParser):
    """Collect movie event links from BookMyShow's server-rendered listing."""

    def __init__(self):
        super().__init__()
        self.movies = {}
        self._href = None
        self._text = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag.lower() != "a" or self._href is None:
            return

        path = urlparse(urljoin("https://in.bookmyshow.com", self._href)).path
        parts = path.strip("/").split("/")
        if "movies" in parts:
            event_code = next(
                (part for part in reversed(parts)
                 if re.fullmatch(r"ET\d{8,}", part)),
                None,
            )
            if event_code:
                name = " ".join(" ".join(self._text).split())
                existing = self.movies.get(event_code)
                if existing:
                    if not existing["name"] and name:
                        existing["name"] = name
                else:
                    self.movies[event_code] = {
                        "event_code": event_code,
                        "name": name or event_code,
                        "url": urljoin("https://in.bookmyshow.com", self._href),
                    }

        self._href = None
        self._text = []


def discover_movies():
    try:
        response = requests.get(LISTING_URL, headers=BMS_HEADERS, timeout=20)
    except requests.RequestException as exc:
        raise RuntimeError(f"Could not load BookMyShow listing {LISTING_URL}: {exc}") from exc
    if response.status_code != 200:
        raise RuntimeError(
            f"BookMyShow listing returned HTTP {response.status_code}: "
            f"{LISTING_URL}"
        )

    parser = MovieLinkParser()
    parser.feed(response.text)
    movies = list(parser.movies.values())
    if not movies:
        raise RuntimeError(
            f"No movie event codes found on {LISTING_URL}; "
            "the BookMyShow listing page may have changed."
        )
    return movies


def resolve_region(slug):
    key = (slug or "").lower().strip()
    if key not in REGION_MAP:
        raise RuntimeError(
            f"Unsupported BMS_CITY '{slug}'. Configure a supported city slug."
        )
    return REGION_MAP[key]


def fetch_bms(event_code, date_code, region):
    region_code, region_slug, lat, lon, geohash = region
    headers = {
        **BMS_HEADERS,
        "Referer": (
            f"https://in.bookmyshow.com/movies/{region_slug}/"
            f"buytickets/{event_code}/"
        ),
        "x-region-code": region_code,
        "x-region-slug": region_slug,
        "x-geohash": geohash,
        "x-latitude": lat,
        "x-longitude": lon,
    }
    params = {
        "eventCode": event_code,
        "dateCode": date_code or "",
        "isDesktop": "true",
        "regionCode": region_code,
        "xLocationShared": "false",
        "memberId": "",
        "lsId": "",
        "subCode": "",
        "lat": lat,
        "lon": lon,
    }
    try:
        response = requests.get(
            API_URL, headers=headers, params=params, timeout=20
        )
    except requests.RequestException as exc:
        raise RuntimeError(
            f"BookMyShow request failed for {event_code} "
            f"({date_code or 'default date'}): {exc}"
        ) from exc
    if response.status_code != 200:
        raise RuntimeError(
            f"BookMyShow returned HTTP {response.status_code} for "
            f"{event_code} ({date_code or 'default date'})."
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"BookMyShow returned invalid JSON for {event_code}."
        ) from exc
    if not isinstance(data, dict) or not isinstance(data.get("data"), dict):
        raise RuntimeError(f"Unexpected BookMyShow response for {event_code}.")
    return data


def parse_movie_info(data):
    info = {"name": "Unknown Movie", "language": ""}
    for widget in data.get("data", {}).get("topStickyWidgets", []):
        if widget.get("type") == "horizontal-text-list":
            for item in widget.get("data", []):
                for row in item.get("leftText", {}).get("data", []):
                    for component in row.get("components", []):
                        text = component.get("text", "")
                        if "•" in text:
                            info["language"] = text.strip()
    bottom_sheet = data.get("data", {}).get("bottomSheetData", {})
    selector = bottom_sheet.get("format-selector", {})
    for widget in selector.get("widgets", []):
        if widget.get("type") == "vertical-text-list":
            for item in widget.get("data", []):
                if item.get("styleId") == "bottomsheet-subtitle":
                    info["name"] = item.get("text", info["name"])
    return info


def parse_dates(data):
    dates = []
    for widget in data.get("data", {}).get("topStickyWidgets", []):
        if widget.get("type") != "horizontal-block-list":
            continue
        for item in widget.get("data", []):
            date_code = str(item.get("id", "")).strip()
            if not re.fullmatch(r"\d{8}", date_code):
                continue
            dates.append(DateInfo(
                date_code=date_code,
                status=DATE_STYLE_MAP.get(item.get("styleId", ""), "UNKNOWN"),
            ))
    return dates


def parse_shows(data, default_date_code=""):
    shows = []
    for widget in data.get("data", {}).get("showtimeWidgets", []):
        if widget.get("type") != "groupList":
            continue
        for group in widget.get("data", []):
            if group.get("type") != "venueGroup":
                continue
            for card in group.get("data", []):
                if card.get("type") != "venue-card":
                    continue
                additional = card.get("additionalData", {})
                venue_name = additional.get("venueName", "Unknown")
                venue_code = additional.get("venueCode", "")
                venue_location_value = theatre_location(additional)
                for showtime in card.get("showtimes", []):
                    details = showtime.get("additionalData", {})
                    date_code = str(
                        details.get("showDateCode")
                        or details.get("dateCode")
                        or default_date_code
                    ).strip()
                    if not date_code:
                        cutoff = details.get("cutOffDateTime", "")
                        if re.match(r"^\d{8}", cutoff):
                            date_code = cutoff[:8]

                    show = ShowInfo(
                        venue_code=venue_code,
                        venue_name=venue_name,
                        session_id=details.get("sessionId", ""),
                        date_code=date_code,
                        time=showtime.get("title", ""),
                        time_code=str(details.get("showTimeCode", "")),
                        screen_attr=(
                            showtime.get("screenAttr", "")
                            or details.get("attributes", "")
                        ),
                        venue_location=venue_location_value,
                    )
                    for category in details.get("categories", []):
                        show.categories.append(CatInfo(
                            name=category.get("priceDesc", ""),
                            price=str(category.get("curPrice", "0")),
                            status=str(category.get("availStatus", "")),
                        ))
                    shows.append(show)
    return shows


def scan_movie(movie, region):
    event_code = movie["event_code"]
    initial_data = fetch_bms(event_code, "", region)
    dates = parse_dates(initial_data)
    selected_date = next(
        (date.date_code for date in dates if date.status == "BOOKABLE"),
        "",
    )
    responses = []
    initial_date = selected_date or ""
    responses.append((initial_date, initial_data))

    dates_to_fetch = [
        date.date_code for date in dates
        if date.status in ("AVAILABLE", "BOOKABLE")
        and date.date_code != selected_date
    ]
    with ThreadPoolExecutor(max_workers=4) as executor:
        future_dates = {
            executor.submit(fetch_bms, event_code, date_code, region): date_code
            for date_code in dates_to_fetch
        }
        for future in as_completed(future_dates):
            responses.append((future_dates[future], future.result()))
    return responses


def _record_date(date_state, date):
    current = date_state.get(date.date_code, "UNKNOWN")
    if DATE_STATUS_PRIORITY[date.status] > DATE_STATUS_PRIORITY[current]:
        date_state[date.date_code] = date.status


def build_state(movies, movie_responses):
    state = {"movies": {}}
    for movie in movies:
        event_code = movie["event_code"]
        event_state = {
            "event_code": event_code,
            "name": movie["name"],
            "url": movie["url"],
            "shows": {},
            "dates": {},
            "language": "",
        }
        responses = movie_responses[event_code]
        api_name = movie["name"]
        for _, data in responses:
            info = parse_movie_info(data)
            response_name = info["name"]
            if info.get("language"):
                event_state["language"] = info["language"]
            if response_name != "Unknown Movie":
                api_name = response_name
                break
        event_state["name"] = api_name

        for date_code, data in responses:
            for date in parse_dates(data):
                _record_date(event_state["dates"], date)
            for show in parse_shows(data, date_code):
                for category in show.categories:
                    key = (
                        f"{show.venue_code}|{show.session_id}|"
                        f"{show.date_code}|{category.name}"
                    )
                    event_state["shows"][key] = {
                        "event_code": event_code,
                        "movie": api_name,
                        "url": movie["url"],
                        "venue": show.venue_name,
                        "venue_code": show.venue_code,
                        "venue_location": show.venue_location,
                        "session_id": show.session_id,
                        "time": show.time,
                        "time_code": show.time_code,
                        "screen": show.screen_attr,
                        "date": show.date_code,
                        "category": category.name,
                        "price": category.price,
                        "status": category.status,
                    }
        state["movies"][event_code] = event_state
    return state


def detect_changes(old_state, new_state):
    changes = []
    old_movies = old_state.get("movies", {})
    initial_scan = not old_movies

    for event_code, new_movie in new_state.get("movies", {}).items():
        old_movie = old_movies.get(event_code, {})
        old_dates = old_movie.get("dates", {})
        opening_dates = []
        for date_code, status in new_movie.get("dates", {}).items():
            previous_status = old_dates.get(date_code)
            if (
                status in ("BOOKABLE", "AVAILABLE")
                and (
                    previous_status == "NOT_OPEN"
                    or (previous_status is None and not initial_scan)
                )
            ):
                opening_dates.append(date_code)

        old_shows = old_movie.get("shows", {})
        newly_bookable = []
        for key, show in new_movie.get("shows", {}).items():
            was_available = show["status"] in BOOKABLE_STATUSES
            if not was_available:
                continue
            old_show = old_shows.get(key)
            if (
                initial_scan
                or old_show is None
                or old_show.get("status") == "0"
            ):
                newly_bookable.append(show)

        if newly_bookable:
            if initial_scan:
                prefix = "Currently bookable"
            elif not old_movie:
                prefix = "New movie now bookable"
            else:
                prefix = "Tickets now available"
            changes.append(_availability_summary(
                prefix, new_movie, newly_bookable, opening_dates
            ))
        elif opening_dates:
            dates = ", ".join(_format_date(date) for date in opening_dates)
            changes.append(
                f"📅 {new_movie['name']}: booking opened for {dates}\n"
                f"{new_movie['url']}"
            )

    return changes


def _availability_summary(prefix, movie, available, opening_dates):
    slots = {}
    theatres = set()
    for show in available:
        key = (show["venue"], show["time"], show["date"])
        slots.setdefault(key, []).append(show)
        theatres.add(show["venue"])

    summary = (
        f"🎟 {prefix}: {movie['name']} — {len(available)} bookable ticket "
        f"categories across {len(slots)} showtimes at {len(theatres)} theatres."
    )
    if opening_dates:
        dates = ", ".join(_format_date(date) for date in opening_dates)
        summary += f" Newly open dates: {dates}."

    previews = []
    for (venue, time, date), categories in list(slots.items())[:3]:
        category = categories[0]
        previews.append(
            f"- {venue}, {time} on {_format_date(date)} — "
            f"{category['category']} ₹{category['price']} "
            f"({AVAIL_STATUS_MAP[category['status']][0]})"
        )
    if previews:
        summary += "\n" + "\n".join(previews)
    if len(slots) > len(previews):
        summary += f"\n…and {len(slots) - len(previews)} additional showtimes."
    return f"{summary}\n{movie['url']}"


def _format_date(date_code):
    if re.fullmatch(r"\d{8}", date_code):
        return f"{date_code[:4]}-{date_code[4:6]}-{date_code[6:]}"
    return date_code or "date not listed"


def _message_chunks(lines, max_chars=3500):
    chunks = []
    current = ""
    for line in lines:
        parts = [line]
        while any(len(part) > max_chars for part in parts):
            part = parts.pop(0)
            parts[0:0] = [
                part[index:index + max_chars]
                for index in range(0, len(part), max_chars)
            ]
        for part in parts:
            candidate = f"{current}\n\n{part}" if current else part
            if len(candidate) > max_chars:
                chunks.append(current)
                current = part
            else:
                current = candidate
    if current:
        chunks.append(current)
    return chunks


def send_telegram(changes):
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not bot_token or not chat_id:
        raise RuntimeError(
            "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID before running "
            "with notifications."
        )
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", bot_token):
        raise RuntimeError("TELEGRAM_BOT_TOKEN does not have a valid format.")
    if not re.fullmatch(r"-?\d+", chat_id):
        raise RuntimeError(
            "TELEGRAM_CHAT_ID must be a numeric Telegram chat ID."
        )

    chunks = _message_chunks(changes)
    for chunk in chunks:
        try:
            response = requests.post(
                TELEGRAM_API_URL.format(bot_token),
                json={
                    "chat_id": chat_id,
                    "text": chunk,
                    "disable_web_page_preview": True,
                },
                timeout=20,
            )
        except requests.RequestException as exc:
            raise RuntimeError(
                "Telegram request failed; check the network and bot token."
            ) from exc
        if response.status_code != 200:
            raise RuntimeError(
                f"Telegram API returned HTTP {response.status_code}: "
                f"{response.text[:500]}"
            )
        try:
            result = response.json()
        except ValueError as exc:
            raise RuntimeError(
                "Telegram returned an invalid response."
            ) from exc
        if not result.get("ok"):
            raise RuntimeError(
                f"Telegram could not deliver the message: "
                f"{result.get('description', 'unknown API error')}"
            )
    print(f"  Telegram alert sent in {len(chunks)} message(s).")


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as state_file:
            return json.load(state_file)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{STATE_FILE} contains invalid JSON; fix or replace it "
            "before monitoring."
        ) from exc


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as state_file:
        json.dump(state, state_file, indent=2, ensure_ascii=False)


def _run_once():
    print(
        f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
        f"BookMyShow watcher — {CITY_SLUG}"
    )
    try:
        region = resolve_region(CITY_SLUG)
        movies = discover_movies()
        print(f"  Found {len(movies)} listed movie event(s).")

        movie_responses = {}
        for index, movie in enumerate(movies, start=1):
            print(f"  Checking {index}/{len(movies)}: {movie['name']}")
            movie_responses[movie["event_code"]] = scan_movie(movie, region)

        old_state = load_state()
        new_state = build_state(movies, movie_responses)
        changes = detect_changes(old_state, new_state)
        if changes:
            print(f"  {len(changes)} bookable ticket change(s) found.")
            send_telegram(changes)
        else:
            print("  No new bookable tickets.")
        save_state(new_state)
    except (RuntimeError, requests.RequestException) as exc:
        print(f"  ERROR: {exc}")
        sys.exit(1)


def main():
    if "--once" in sys.argv[1:]:
        _run_once()
        return

    import threading

    from bot import (
        TelegramBot,
        _log_interaction,
        configure_interaction_logging,
    )
    from monitor import MonitoringService
    from storage import PersistentStore

    stop_event = threading.Event()
    store = PersistentStore()
    log_path = configure_interaction_logging(store.path)
    _log_interaction(
        "application_started",
        text=f"city={CITY_SLUG}; interaction_log={log_path}",
    )
    monitor = MonitoringService(store)
    bot = TelegramBot(store, monitor)
    monitor_thread = threading.Thread(
        target=monitor.run,
        args=(stop_event,),
        name="movie-monitor",
        daemon=True,
    )
    monitor_thread.start()
    print(
        f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
        f"BMS_AGENT running — {CITY_SLUG}. Send /start to your Telegram bot. "
        f"Interaction log: {log_path}"
    )
    try:
        bot.run(stop_event)
    except KeyboardInterrupt:
        print("\nStopping BMS_AGENT...")
    finally:
        stop_event.set()
        monitor_thread.join(timeout=30)


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)
