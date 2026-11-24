"""Telegram long-polling interface for interactive movie monitoring."""

import json
import logging
import os
import re
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests

from monitor import BOOKABLE, MonitoringService
from theatre import format_date, theatre_identifier


PAGE_SIZE = 6
CATALOG_PAGE_SIZE = 8
STATUS_LABELS = {
    "0": "Sold out",
    "1": "Almost full",
    "2": "Filling fast",
    "3": "Available",
}
INTERACTION_LOG = logging.getLogger("bms.interactions")


class _InteractionFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps(
            {
                "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
                "event": getattr(record, "interaction_event", "application"),
                "action": getattr(record, "interaction_action", ""),
                "text": getattr(record, "interaction_text", ""),
            },
            ensure_ascii=False,
        )


def configure_interaction_logging(data_path=None):
    log_path = Path(
        os.getenv("BMS_LOG_FILE")
        or (
            Path(data_path).parent / "logs" / "bms_bot.log"
            if data_path
            else Path(__file__).parent / "logs" / "bms_bot.log"
        )
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_path = str(log_path.resolve())
    if any(
        getattr(handler, "bms_log_path", None) == resolved_path
        for handler in INTERACTION_LOG.handlers
    ):
        return log_path
    for handler in list(INTERACTION_LOG.handlers):
        INTERACTION_LOG.removeHandler(handler)
        handler.close()
    handler = RotatingFileHandler(
        log_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.bms_log_path = resolved_path
    handler.setFormatter(_InteractionFormatter())
    INTERACTION_LOG.addHandler(handler)
    INTERACTION_LOG.setLevel(logging.INFO)
    INTERACTION_LOG.propagate = False
    return log_path


def _log_interaction(event, action="", text=""):
    INTERACTION_LOG.info(
        event,
        extra={
            "interaction_event": event,
            "interaction_action": action,
            "interaction_text": text,
        },
    )


class TelegramConfigurationError(RuntimeError):
    pass


class TelegramClient:
    def __init__(self, token=None):
        self.token = (token or os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", self.token):
            raise RuntimeError(
                "Set a valid replacement TELEGRAM_BOT_TOKEN environment variable."
            )
        self.base_url = f"https://api.telegram.org/bot{self.token}"
        self._send_lock = threading.Lock()
        self._last_send_at = 0.0

    def _request(self, method, payload=None, timeout=30):
        try:
            response = requests.post(
                f"{self.base_url}/{method}",
                json=payload or {},
                timeout=timeout,
            )
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Telegram {method} request failed; check the network."
            ) from exc
        if response.status_code in (401, 409):
            message = (
                "Telegram rejected the bot token."
                if response.status_code == 401
                else "Another process is already polling this bot token."
            )
            raise TelegramConfigurationError(message)
        if response.status_code != 200:
            raise RuntimeError(
                f"Telegram {method} returned HTTP {response.status_code}."
            )
        try:
            result = response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"Telegram {method} returned invalid JSON."
            ) from exc
        if not isinstance(result, dict) or not result.get("ok"):
            description = (
                result.get("description", "unknown Telegram API error")
                if isinstance(result, dict)
                else "invalid Telegram API response"
            )
            raise RuntimeError(f"Telegram {method} failed: {description}")
        return result.get("result")

    def get_updates(self, offset, timeout=25):
        result = self._request(
            "getUpdates",
            {
                "offset": offset,
                "timeout": timeout,
                "allowed_updates": ["message", "callback_query"],
            },
            timeout=timeout + 10,
        )
        if not isinstance(result, list):
            raise RuntimeError("Telegram getUpdates returned an invalid update list.")
        return result

    def send_message(self, chat_id, text, reply_markup=None):
        payload = {
            "chat_id": str(chat_id),
            "text": text[:4000],
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        with self._send_lock:
            delay = 1.0 - (time.monotonic() - self._last_send_at)
            if delay > 0:
                time.sleep(delay)
            self._last_send_at = time.monotonic()
            try:
                self._request("sendMessage", payload)
            except Exception as exc:
                _log_interaction(
                    "response_delivery_failed",
                    type(exc).__name__,
                    payload["text"],
                )
                raise
            _log_interaction("response_sent", text=payload["text"])

    def answer_callback(self, callback_id, text=None):
        payload = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text[:200]
        try:
            self._request("answerCallbackQuery", payload, timeout=15)
        except Exception as exc:
            _log_interaction(
                "callback_answer_failed",
                type(exc).__name__,
                payload.get("text", "Callback acknowledged"),
            )
            raise
        _log_interaction(
            "callback_answer",
            text=payload.get("text", "Callback acknowledged"),
        )


class TelegramBot:
    def __init__(self, store, monitor, client=None):
        self.store = store
        self.monitor = monitor
        self.client = client or TelegramClient()
        self.monitor.notify = self.client.send_message
        self._refresh_lock = threading.Lock()
        self._menu_lock = threading.Lock()

    @staticmethod
    def _button(text, data=None, url=None):
        button = {"text": text}
        if url:
            button["url"] = url
        else:
            button["callback_data"] = data
        return button

    @staticmethod
    def _keyboard(rows):
        return {"inline_keyboard": rows}

    def _send(self, chat_id, text, rows=None):
        self.client.send_message(
            chat_id,
            text,
            self._keyboard(rows) if rows else None,
        )

    def _start_refresh(self, chat_id, user_id=None, after="catalog"):
        if not self._refresh_lock.acquire(blocking=False):
            self._send(chat_id, "A movie refresh is already running. Please wait.")
            return
        self._send(
            chat_id,
            "Refreshing the Hyderabad movie catalogue and checking listings. "
            "This can take a few minutes; I’ll send the results here.",
        )

        def work():
            try:
                movies = self.monitor.refresh_catalog()
                if after == "favourites" and user_id is not None:
                    self._open_favourites(chat_id, user_id, 0)
                elif after == "menu" and user_id is not None:
                    self._main_menu(chat_id, user_id)
                else:
                    self._send_catalog(chat_id, 0, movies=movies)
            except (RuntimeError, requests.RequestException) as exc:
                print(f"Movie catalogue refresh failed: {exc}")
                self._send(
                    chat_id,
                    f"Refresh failed; the last valid catalogue was kept.\n{exc}",
                )
            finally:
                self._refresh_lock.release()

        threading.Thread(target=work, name="catalog-refresh", daemon=True).start()

    def _catalog(self):
        state = self.store.read()
        return state.get("catalog", []), state.get("catalog_updated_at")

    def _main_menu(self, chat_id, user_id):
        user = self.store.user(user_id)
        if not user.get("theatre_preferences_saved") or not user.get(
            "favourite_theatres"
        ):
            self._open_favourites(chat_id, user_id, 0)
            return
        self._send(
            chat_id,
            "🎬 BMS_AGENT MENU\n\n"
            "Your priority movie results and monitoring are restricted to "
            "your saved favourite theatres.",
            [
                [
                    self._button("🎬 Discover Movies", "menu:discover"),
                    self._button("⭐ My Priority Movies", "menu:priorities"),
                ],
                [
                    self._button("🏛️ My Favourite Theatres", "menu:favourites"),
                    self._button("🔔 Monitoring Notifications", "menu:notifications"),
                ],
                [
                    self._button("📅 Available Dates", "menu:priorities"),
                    self._button("⚙️ Monitoring Settings", "priorities:0"),
                ],
                [self._button("🔄 Refresh", "menu:refresh")],
            ],
        )

    def _available_theatres(self):
        state = self.store.read()
        theatres = {
            entry["theatre_id"]: entry
            for entry in state.get("theatres", [])
            if entry.get("theatre_id")
        }
        for snapshot in state.get("snapshots", {}).values():
            for show in snapshot.get("shows", {}).values():
                theatre_id = theatre_identifier(
                    show.get("venue_code"), show.get("venue")
                )
                if not theatre_id:
                    continue
                existing = theatres.get(theatre_id, {})
                theatres[theatre_id] = {
                    "theatre_id": theatre_id,
                    "venue_code": str(show.get("venue_code") or ""),
                    "theatre_name": str(show.get("venue") or ""),
                    "location": show.get("venue_location")
                    or existing.get("location", ""),
                }
        return sorted(
            theatres.values(),
            key=lambda theatre: (
                theatre.get("theatre_name", "").casefold(),
                theatre.get("location", "").casefold(),
                theatre["theatre_id"],
            ),
        )

    def _open_favourites(self, chat_id, user_id, page=0, reset_draft=False):
        user = self.store.user(user_id)
        ui = user.get("ui", {})
        if "theatre_selection_draft" not in ui or reset_draft:
            selected = list(user.get("favourite_theatres", {}).keys())
            self.store.set_ui(user_id, "theatre_selection_draft", selected)
        theatres = self._available_theatres()
        if not theatres:
            state = self.store.read()
            if not state.get("catalog_updated_at"):
                self._send(chat_id, "Loading current theatre names from BookMyShow…")
                self._start_refresh(chat_id, user_id, after="favourites")
            else:
                self._send(
                    chat_id,
                    "No theatre names were available in the last successful "
                    "BookMyShow response. Refresh later to try again.",
                    [
                        [self._button("🔄 Refresh Catalogue", "refresh:favourites")],
                        [self._button("⬅ Main Menu", "menu:home")],
                    ],
                )
            return

        user = self.store.user(user_id)
        selected = set(
            user.get("ui", {}).get("theatre_selection_draft", [])
        )
        search = user.get("ui", {}).get("theatre_search", "").strip().casefold()
        if search:
            theatres = [
                theatre for theatre in theatres
                if search in theatre.get("theatre_name", "").casefold()
                or search in theatre.get("location", "").casefold()
            ]
        page_size = 6
        page_count = max(1, (len(theatres) + page_size - 1) // page_size)
        page = max(0, min(page, page_count - 1))
        page_theatres = theatres[page * page_size:(page + 1) * page_size]
        lines = [
            "🏛️ SELECT FAVOURITE THEATRES",
            "Select one or more venues. Priority movies, showtimes, monitoring, "
            "history, and alerts will be limited to this selection.",
            f"Selected: {len(selected)}",
        ]
        if search:
            lines.append(f"Search: {search}")
        if not page_theatres:
            lines.append("\nNo theatres match that search.")
        rows = []
        for index, theatre in enumerate(page_theatres, start=page * page_size):
            selected_mark = "☑" if theatre["theatre_id"] in selected else "☐"
            location = theatre.get("location") or "Theatre information unavailable"
            lines.append(
                f"\n{selected_mark} {theatre['theatre_name']}\n"
                f"   📍 {location}"
            )
            rows.append(
                [
                    self._button(
                        (
                            "Remove: " if theatre["theatre_id"] in selected
                            else "Add: "
                        )
                        + theatre["theatre_name"][:24],
                        f"favtoggle:{page}:{index}",
                    )
                ]
            )
        nav = []
        if page > 0:
            nav.append(self._button("⬅ Previous", f"favourites:{page - 1}"))
        if page + 1 < page_count:
            nav.append(self._button("Next ➡", f"favourites:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.extend(
            [
                [
                    self._button("Select page", f"favselectpage:{page}"),
                    self._button("Clear selection", "favclear"),
                ],
                [
                    self._button("🔎 Search", "favsearch"),
                    self._button("Clear search", "favsearchclear"),
                ],
                [self._button("💾 Save Favourite Theatres", "favsave")],
                [
                    self._button("⬅ Main Menu", "menu:home"),
                    self._button("⭐ My Priority Movies", "menu:priorities"),
                ],
            ]
        )
        self._send(chat_id, "\n".join(lines), rows)

    def _toggle_favourite(self, user_id, theatre):
        user = self.store.user(user_id)
        draft = set(
            user.get("ui", {}).get("theatre_selection_draft", [])
        )
        theatre_id = theatre["theatre_id"]
        if theatre_id in draft:
            draft.remove(theatre_id)
        else:
            draft.add(theatre_id)
        self.store.set_ui(user_id, "theatre_selection_draft", sorted(draft))

    def _filtered_priority_shows(self, user, snapshot):
        favourites = user.get("favourite_theatres", {})
        if not favourites:
            return []
        shows = []
        today = time.strftime("%Y%m%d")
        for key, show in snapshot.get("shows", {}).items():
            theatre_id = theatre_identifier(
                show.get("venue_code"), show.get("venue")
            )
            if theatre_id not in favourites:
                continue
            if str(show.get("status", "")) not in BOOKABLE:
                continue
            if show.get("date") and show["date"] < today:
                continue
            shows.append((key, show, theatre_id))
        return shows

    def _send_catalog(self, chat_id, page, movies=None):
        if movies is None:
            movies, updated_at = self._catalog()
        else:
            updated_at = self.store.read().get("catalog_updated_at")
        if not movies:
            self._send(
                chat_id,
                "No cached bookable movies yet. Refresh the catalogue to check "
                "BookMyShow now.",
                [[self._button("🔄 Refresh Movies", "refresh")]],
            )
            return

        page_count = max(1, (len(movies) + CATALOG_PAGE_SIZE - 1) // CATALOG_PAGE_SIZE)
        page = max(0, min(page, page_count - 1))
        page_movies = movies[
            page * CATALOG_PAGE_SIZE:(page + 1) * CATALOG_PAGE_SIZE
        ]
        lines = ["🎬 NOW SHOWING — HYDERABAD"]
        rows = []
        for index, movie in enumerate(
            page_movies, start=page * CATALOG_PAGE_SIZE + 1
        ):
            language = movie.get("language")
            dates = [
                self._format_date(value)
                for value in (movie.get("earliest_date"), movie.get("latest_date"))
                if value
            ]
            metadata = f"   ID {movie['event_code']}"
            if language:
                metadata += f" · {language}"
            if dates:
                metadata += f"\n   Dates: {' – '.join(dates)}"
            lines.append(
                f"\n{index}. {movie['name']}\n"
                f"   {movie.get('showtime_count', 0)} bookable showtimes | "
                f"{movie.get('theatre_count', 0)} theatres\n"
                f"   {movie.get('bookable_count', 0)} ticket categories\n"
                f"{metadata}"
            )
            rows.append(
                [
                    self._button(
                        f"🎟 {movie['name'][:24]}",
                        f"movie:{movie['event_code']}:0",
                    ),
                    self._button(
                        "⭐ Add",
                        f"priority:add:{movie['event_code']}",
                    ),
                ]
            )
        lines.append(f"\nPage {page + 1}/{page_count}")
        if updated_at:
            lines.append(f"Updated: {updated_at}")
        nav = []
        if page > 0:
            nav.append(self._button("⬅ Previous", f"catalog:{page - 1}"))
        if page + 1 < page_count:
            nav.append(self._button("Next ➡", f"catalog:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append(
            [
                self._button("🔄 Refresh Movies", "refresh"),
                self._button("⭐ My Priorities", "priorities:0"),
            ]
        )
        self._send(chat_id, "\n".join(lines), rows)

    def _movie_page(self, chat_id, user_id, event_code, page=0):
        state = self.store.read()
        movie = state.get("snapshots", {}).get(event_code)
        if not movie:
            self._send(
                chat_id,
                "I don’t have a successful availability snapshot for this movie. "
                "Refresh the movie catalogue first.",
                [[self._button("🔄 Refresh Movies", "refresh")]],
            )
            return

        user = self.store.user(user_id)
        filters = user.get("ui", {}).get("filters", {}).get(event_code, {})
        shows = self._matching_shows(user_id, event_code, movie)
        sort_by = filters.get("sort", "default")

        page_count = max(1, (len(shows) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, page_count - 1))
        page_shows = shows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        filters_text = ", ".join(
            f"{key}={self._format_date(value) if key == 'date' else value}"
            for key, value in filters.items()
            if value and key not in ("sort", "available_only")
        ) or "none"
        all_shows = list(movie.get("shows", {}).values())
        theatres = {
            show.get("venue", "")
            for show in all_shows
            if show.get("venue")
        }
        bookable_count = sum(
            str(show.get("status", "")) in BOOKABLE for show in all_shows
        )
        bookable_show_ids = {
            "|".join(
                (
                    str(show.get("venue_code") or show.get("venue", "")),
                    str(show.get("session_id") or show.get("time_code", "")),
                    str(show.get("date", "")),
                    str(show.get("time_code") or show.get("time", "")),
                )
            )
            for show in all_shows
            if str(show.get("status", "")) in BOOKABLE
        }
        lines = [f"🎬 {movie.get('name', event_code)}"]
        if movie.get("language"):
            lines.append(f"Language: {movie['language']}")
        lines.extend(
            [
                f"Movie ID: {event_code}",
                f"{len(bookable_show_ids)} bookable showtimes | "
                f"{bookable_count} ticket categories | "
                f"{len(theatres)} theatres",
                f"{len(shows)} matching ticket categories",
                f"Filters: {filters_text}",
                f"Sort: {sort_by}; available only: "
                f"{'yes' if filters.get('available_only') else 'no'}",
            ]
        )
        rows = []
        for index, (_, show) in enumerate(
            page_shows, start=page * PAGE_SIZE
        ):
            status = STATUS_LABELS.get(
                str(show.get("status", "")), "Status unknown"
            )
            date = self._format_date(show.get("date", ""))
            screen = f" · {show['screen']}" if show.get("screen") else ""
            lines.append(
                f"\n{index + 1}. {show.get('venue', 'Unknown theatre')}\n"
                f"   {date} · {show.get('time', 'Time unavailable')}{screen}\n"
                f"   {show.get('category', 'Ticket')} · "
                f"₹{show.get('price', 'unknown')} · {status}"
            )
            rows.append(
                [
                    self._button(
                        (
                            f"🕒 {show.get('time', 'Showtime')} · "
                            f"{show.get('category', 'Tickets')}"
                        )[:60],
                        f"detail:{event_code}:{index}",
                    )
                ]
            )

        lines.append(f"\nPage {page + 1}/{page_count}")
        nav = []
        if page > 0:
            nav.append(
                self._button("⬅ Previous", f"shows:{event_code}:{page - 1}")
            )
        if page + 1 < page_count:
            nav.append(self._button("Next ➡", f"shows:{event_code}:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.extend(
            [
                [
                    self._button("⚙ Filters", f"filters:{event_code}"),
                    self._button("↕ Sort", f"sortmenu:{event_code}"),
                ],
                [
                    self._button("🔄 Refresh", f"refreshmovie:{event_code}"),
                    self._button(
                        "⭐ Add to Priority",
                        f"priority:add:{event_code}",
                    ),
                ],
                [
                    self._button("⬅ Movies", "catalog:0"),
                    self._button("⭐ My Priorities", "priorities:0"),
                ],
            ]
        )
        self._send(chat_id, "\n".join(lines), rows)

    def _matching_shows(self, user_id, event_code, movie):
        filters = (
            self.store.user(user_id)
            .get("ui", {})
            .get("filters", {})
            .get(event_code, {})
        )
        shows = list(movie.get("shows", {}).items())
        for field, key in (
            ("date", "date"),
            ("theatre", "venue"),
            ("time", "time"),
            ("category", "category"),
            ("status", "status"),
        ):
            if filters.get(field):
                shows = [
                    item
                    for item in shows
                    if str(item[1].get(key, "")) == str(filters[field])
                ]
        if filters.get("available_only"):
            shows = [
                item
                for item in shows
                if str(item[1].get("status")) in BOOKABLE
            ]
        ordering = filters.get("sort", "default")
        if ordering == "price":
            shows.sort(key=lambda item: self._price_value(item[1].get("price")))
        else:
            shows.sort(
                key=lambda item: (
                    item[1].get("date", ""),
                    item[1].get("time_code", ""),
                    item[1].get("time", ""),
                ),
                reverse=ordering == "latest",
            )
        return shows

    @staticmethod
    def _price_value(value):
        try:
            return float(str(value).replace(",", "").replace("₹", "").strip())
        except (TypeError, ValueError):
            return float("inf")

    @staticmethod
    def _format_date(value):
        return format_date(value)

    def _filter_menu(self, chat_id, event_code):
        state = self.store.read()
        movie = state.get("snapshots", {}).get(event_code, {})
        shows = list(movie.get("shows", {}).values())
        fields = (
            ("date", "Date", lambda show: show.get("date", "")),
            ("theatre", "Theatre", lambda show: show.get("venue", "")),
            ("time", "Showtime", lambda show: show.get("time", "")),
            ("category", "Category", lambda show: show.get("category", "")),
            ("status", "Status", lambda show: show.get("status", "")),
        )
        rows = []
        for field, label, _ in fields:
            rows.append([self._button(label, f"filtervalues:{event_code}:{field}")])
            if field == "status":
                rows[-1][0]["text"] = "Choose status"
        rows.extend(
            [
                [
                    self._button("Toggle available-only", f"only:{event_code}"),
                    self._button("Clear filters", f"clearfilters:{event_code}"),
                ],
                [self._button("⬅ Showtimes", f"shows:{event_code}:0")],
            ]
        )
        self._send(chat_id, "Select a filter to see available values.", rows)

    def _filter_values(self, chat_id, event_code, field):
        state = self.store.read()
        movie = state.get("snapshots", {}).get(event_code, {})
        getter_map = {
            "date": ("date", "Date"),
            "theatre": ("venue", "Theatre"),
            "time": ("time", "Showtime"),
            "category": ("category", "Category"),
            "status": ("status", "Status"),
        }
        if field not in getter_map:
            return
        key, label = getter_map[field]
        values = sorted(
            {
                str(show.get(key))
                for show in movie.get("shows", {}).values()
                if show.get(key)
            }
        )
        rows = [
            [
                self._button(
                    (
                        self._format_date(value)[:30]
                        if field == "date"
                        else value[:30]
                    ),
                    f"filterset:{event_code}:{field}:{index}",
                )
            ]
            for index, value in enumerate(values[:50])
        ]
        rows.append([self._button("⬅ Filters", f"filters:{event_code}")])
        self._send(chat_id, f"Choose {label}:", rows)

    def _sort_menu(self, chat_id, event_code):
        rows = [
            [
                self._button("Lowest price", f"sort:{event_code}:price"),
                self._button("Earliest show", f"sort:{event_code}:earliest"),
            ],
            [
                self._button("Latest show", f"sort:{event_code}:latest"),
                self._button("Default order", f"sort:{event_code}:default"),
            ],
            [self._button("⬅ Showtimes", f"shows:{event_code}:0")],
        ]
        self._send(chat_id, "Choose showtime order:", rows)

    def _show_detail(self, chat_id, user_id, event_code, index):
        state = self.store.read()
        movie = state.get("snapshots", {}).get(event_code)
        if not movie:
            self._send(chat_id, "Availability snapshot not found.")
            return
        shows = self._matching_shows(user_id, event_code, movie)
        if index < 0 or index >= len(shows):
            self._send(chat_id, "That show is no longer in the current snapshot.")
            return
        _, show = shows[index]
        status = STATUS_LABELS.get(str(show.get("status", "")), "Unknown")
        text = (
            f"🎬 {show.get('movie', movie.get('name', event_code))}\n"
            f"🏢 {show.get('venue', 'Unknown theatre')}\n"
            f"📅 {self._format_date(show.get('date', ''))}\n"
            f"🕒 {show.get('time', 'Time unavailable')}\n"
            f"🎟 {show.get('category', 'Ticket')}\n"
            f"💰 ₹{show.get('price', 'unknown')}\n"
            f"Status: {status}\n\n"
            "Use the official BookMyShow page to continue. This bot does not "
            "book tickets."
        )
        if show.get("screen"):
            text = text.replace(
                f"📅 {self._format_date(show.get('date', ''))}",
                f"🖥 {show['screen']}\n"
                f"📅 {self._format_date(show.get('date', ''))}",
            )
        rows = []
        if show.get("url"):
            rows.append([self._button("Open BookMyShow", url=show["url"])])
        rows.append([self._button("⬅ Showtimes", f"shows:{event_code}:0")])
        self._send(chat_id, text, rows)

    def _priorities(self, chat_id, user_id, page=0):
        state = self.store.read()
        user = state.get("users", {}).get(str(user_id), {})
        if not user.get("theatre_preferences_saved") or not user.get(
            "favourite_theatres"
        ):
            self._open_favourites(chat_id, user_id, 0)
            return

        priorities = []
        for code, priority in user.get("priorities", {}).items():
            snapshot = state.get("snapshots", {}).get(code, {})
            shows = self._filtered_priority_shows(user, snapshot)
            if shows:
                priorities.append((code, priority, shows))
        priorities.sort(key=lambda item: item[1].get("name", "").casefold())
        page_count = max(1, (len(priorities) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, page_count - 1))
        selected = priorities[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        lines = ["⭐ MY PRIORITY MOVIES — FAVOURITE THEATRES ONLY"]
        rows = []
        for index, (code, priority, shows) in enumerate(
            selected, start=page * PAGE_SIZE + 1
        ):
            showtime_ids = {
                "|".join(key.split("|")[:3])
                for key, _, _ in shows
            }
            theatre_ids = {theatre_id for _, _, theatre_id in shows}
            upcoming_showtimes = {}
            for key, show, theatre_id in shows:
                showtime_id = (
                    theatre_id,
                    show.get("date", ""),
                    show.get("time_code") or show.get("time", ""),
                )
                upcoming_showtimes.setdefault(
                    showtime_id,
                    (
                        show.get("date", ""),
                        show.get("time_code", ""),
                        show.get("time", ""),
                        show.get("venue", ""),
                    ),
                )
            upcoming_showtimes = sorted(
                upcoming_showtimes.values(),
                key=lambda showtime: (
                    showtime[0],
                    showtime[1] or showtime[2],
                    showtime[3],
                ),
            )
            favourite_names = [
                user["favourite_theatres"][theatre_id]["theatre_name"]
                for theatre_id in theatre_ids
                if theatre_id in user["favourite_theatres"]
            ]
            latest_event = next(
                (
                    event
                    for event in reversed(state.get("events", []))
                    if event.get("movie_id") == code
                    and event.get("theatre_id") in user["favourite_theatres"]
                ),
                None,
            )
            monitor_status = (
                "Monitoring active"
                if priority.get("monitoring_enabled", True)
                else "Monitoring paused"
            )
            notify_status = (
                "notifications on"
                if priority.get("notification_enabled", True)
                else "notifications off"
            )
            lines.append(
                f"\n{index}. {priority['name']} — {monitor_status}, "
                f"{notify_status}\n"
                f"{len(showtime_ids)} upcoming shows across "
                f"{len(theatre_ids)} favourite theatres\n"
                f"Playing at: {', '.join(sorted(favourite_names))}\n"
                f"Last successful check: "
                f"{priority.get('last_successful_check', 'not checked yet')}"
            )
            if upcoming_showtimes:
                preview = upcoming_showtimes[:5]
                lines.append(
                    "Upcoming dates & showtimes:\n"
                    + "\n".join(
                        f"  • {self._format_date(date)} · {showtime}"
                        + (f" · {venue}" if venue else "")
                        for date, _, showtime, venue in preview
                    )
                )
                if len(upcoming_showtimes) > len(preview):
                    lines.append(
                        f"  + {len(upcoming_showtimes) - len(preview)} more "
                        "— tap View for all"
                    )
            if latest_event:
                lines.append(
                    f"Latest favourite-theatre event: "
                    f"{latest_event.get('detected_at', '')} — "
                    f"{latest_event.get('summary', '')}"
                )
            if priority.get("last_error"):
                lines.append(f"Last error: {priority['last_error']}")
            rows.append(
                [
                    self._button("📅 Show dates", f"pfmovie:{code}"),
                    self._button("Refresh now", f"refreshmovie:{code}"),
                ]
            )
            rows.append(
                [
                    self._button(
                        "Pause" if priority.get("monitoring_enabled", True) else "Resume",
                        f"monitor:{code}:"
                        f"{'pause' if priority.get('monitoring_enabled', True) else 'resume'}",
                    ),
                    self._button(
                        "Mute" if priority.get("notification_enabled", True) else "Notify",
                        f"notify:{code}:"
                        f"{'off' if priority.get('notification_enabled', True) else 'on'}",
                    ),
                    self._button("Remove", f"priority:remove:{code}"),
                ]
            )
        if not priorities:
            lines.append(
                "\nNo upcoming bookable shows found in your favourite theatres."
            )
        lines.append(f"\nPage {page + 1}/{page_count}")
        nav = []
        if page > 0:
            nav.append(self._button("⬅ Previous", f"priorities:{page - 1}"))
        if page + 1 < page_count:
            nav.append(self._button("Next ➡", f"priorities:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append(
            [
                self._button("🎬 Discover Movies", "menu:discover"),
                self._button("🏛️ Favourite Theatres", "menu:favourites"),
            ]
        )
        rows.append(
            [
                self._button("📜 Event History", "history:0"),
                self._button("🏠 Main Menu", "menu:home"),
            ]
        )
        self._send(chat_id, "\n".join(lines), rows)

    def _priority_movie(self, chat_id, user_id, event_code):
        state = self.store.read()
        user = state.get("users", {}).get(str(user_id), {})
        if not user.get("favourite_theatres"):
            self._open_favourites(chat_id, user_id, 0)
            return
        priority = user.get("priorities", {}).get(event_code)
        snapshot = state.get("snapshots", {}).get(event_code, {})
        if not priority or not snapshot:
            self._send(chat_id, "That priority movie is no longer available.")
            return
        shows = self._filtered_priority_shows(user, snapshot)
        if not shows:
            self._send(
                chat_id,
                "No upcoming bookable shows found in your favourite theatres.",
                [[self._button("⬅ My Priority Movies", "priorities:0")]],
            )
            return
        dates = sorted({show.get("date", "") for _, show, _ in shows})
        lines = [f"⭐ {priority['name']}"]
        rows = []
        for date in dates:
            date_shows = [
                show for _, show, _ in shows if show.get("date") == date
            ]
            lines.append(
                f"\n📅 {self._format_date(date)} — "
                f"{len(date_shows)} bookable ticket categories"
            )
            rows.append(
                [
                    self._button(
                        f"📅 {self._format_date(date)}",
                        f"pfdate:{event_code}:{date}",
                    )
                ]
            )
        rows.extend(
            [
                [self._button("🏛️ Edit Favourite Theatres", "menu:favourites")],
                [self._button("⬅ My Priority Movies", "priorities:0")],
            ]
        )
        self._send(chat_id, "\n".join(lines), rows)

    def _priority_date(self, chat_id, user_id, event_code, date_code):
        state = self.store.read()
        user = state.get("users", {}).get(str(user_id), {})
        if event_code not in user.get("priorities", {}):
            self._send(chat_id, "That movie is no longer in your priority list.")
            return
        snapshot = state.get("snapshots", {}).get(event_code, {})
        shows = [
            (key, show, theatre_id)
            for key, show, theatre_id in self._filtered_priority_shows(
                user, snapshot
            )
            if show.get("date") == date_code
        ]
        theatres = {}
        for _, show, theatre_id in shows:
            theatres[theatre_id] = show
        lines = [
            f"🏛️ FAVOURITE THEATRES — {self._format_date(date_code)}"
        ]
        rows = []
        for theatre_id, show in sorted(
            theatres.items(),
            key=lambda item: item[1].get("venue", "").casefold(),
        ):
            name = user["favourite_theatres"][theatre_id]["theatre_name"]
            location = (
                user["favourite_theatres"][theatre_id].get("location")
                or "Theatre information unavailable"
            )
            count = sum(
                1 for _, _, current_id in shows if current_id == theatre_id
            )
            lines.append(f"\n{name}\n📍 {location} · {count} categories")
            rows.append(
                [
                    self._button(
                        name[:32],
                        f"pftheatre:{event_code}:{date_code}:{theatre_id}",
                    )
                ]
            )
        if not theatres:
            lines.append(
                "\nNo upcoming bookable shows found in your favourite theatres."
            )
        rows.append(
            [self._button("⬅ Available Dates", f"pfmovie:{event_code}")]
        )
        self._send(chat_id, "\n".join(lines), rows)

    def _priority_theatre(
        self, chat_id, user_id, event_code, date_code, theatre_id, page=0
    ):
        state = self.store.read()
        user = state.get("users", {}).get(str(user_id), {})
        if event_code not in user.get("priorities", {}):
            self._send(chat_id, "That movie is no longer in your priority list.")
            return
        favourite = user.get("favourite_theatres", {}).get(theatre_id)
        if favourite is None:
            self._send(
                chat_id,
                "This theatre is no longer in your favourite selection. "
                "No other theatres were included.",
                [[self._button("🏛️ Edit Favourite Theatres", "menu:favourites")]],
            )
            return
        snapshot = state.get("snapshots", {}).get(event_code, {})
        priority = user.get("priorities", {}).get(event_code, {})
        shows = [
            (key, show)
            for key, show, current_id in self._filtered_priority_shows(
                user, snapshot
            )
            if current_id == theatre_id and show.get("date") == date_code
        ]
        page_count = max(1, (len(shows) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, page_count - 1))
        selected = shows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        lines = [
            f"🎬 {priority.get('name', snapshot.get('name', event_code))}",
            f"🏛️ {favourite['theatre_name']}",
            f"📍 {favourite.get('location') or 'Theatre information unavailable'}",
            f"📅 {self._format_date(date_code)}",
        ]
        rows = []
        for index, (_, show) in enumerate(selected, start=page * PAGE_SIZE):
            status = STATUS_LABELS.get(str(show.get("status", "")), "Unknown")
            lines.append(
                f"\n🕒 {show.get('time', 'Time unavailable')}"
                f"{' · ' + show['screen'] if show.get('screen') else ''}\n"
                f"🎟 {show.get('category', 'Ticket')} · "
                f"₹{show.get('price', 'unknown')} · {status}"
            )
            rows.append(
                [
                    self._button(
                        (
                            f"🕒 {show.get('time', 'Showtime')} · "
                            f"{show.get('category', 'Tickets')}"
                        )[:60],
                        f"pfshow:{event_code}:{date_code}:{theatre_id}:{index}",
                    )
                ]
            )
        if not shows:
            lines.append("No upcoming bookable shows found in your favourite theatres.")
        nav = []
        if page > 0:
            nav.append(
                self._button(
                    "⬅ Previous",
                    f"pftheatre:{event_code}:{date_code}:{theatre_id}:{page - 1}",
                )
            )
        if page + 1 < page_count:
            nav.append(
                self._button(
                    "Next ➡",
                    f"pftheatre:{event_code}:{date_code}:{theatre_id}:{page + 1}",
                )
            )
        if nav:
            rows.append(nav)
        rows.extend(
            [
                [
                    self._button(
                        "⬅ Favourite Theatres",
                        f"pfdate:{event_code}:{date_code}",
                    )
                ],
                [self._button("⭐ My Priority Movies", "priorities:0")],
            ]
        )
        self._send(chat_id, "\n".join(lines), rows)

    def _priority_show(
        self, chat_id, user_id, event_code, date_code, theatre_id, index
    ):
        state = self.store.read()
        user = state.get("users", {}).get(str(user_id), {})
        if event_code not in user.get("priorities", {}):
            self._send(chat_id, "That movie is no longer in your priority list.")
            return
        favourite = user.get("favourite_theatres", {}).get(theatre_id)
        if favourite is None:
            self._send(
                chat_id,
                "This theatre is no longer selected; no other theatre data is shown.",
            )
            return
        snapshot = state.get("snapshots", {}).get(event_code, {})
        shows = [
            (key, show)
            for key, show, current_id in self._filtered_priority_shows(
                user, snapshot
            )
            if current_id == theatre_id and show.get("date") == date_code
        ]
        if index < 0 or index >= len(shows):
            self._send(chat_id, "That show is no longer available.")
            return
        _, show = shows[index]
        location = (
            favourite.get("location") or "Theatre information unavailable"
        )
        text = (
            f"🎬 {show.get('movie', snapshot.get('name', event_code))}\n"
            f"🏛️ {favourite['theatre_name']}\n"
            f"📍 {location}\n"
            f"📅 {self._format_date(show.get('date', ''))}\n"
            f"🕒 {show.get('time', 'Time unavailable')}\n"
            f"🎟 {show.get('category', 'Ticket')}\n"
            f"💰 ₹{show.get('price', 'unknown')}\n"
            f"Status: {STATUS_LABELS.get(str(show.get('status', '')), 'Unknown')}\n"
        )
        if show.get("screen"):
            text += f"Screen: {show['screen']}\n"
        text += (
            "\nSeat rows and individual seat availability are not provided by "
            "the current BookMyShow data source. This bot does not book tickets."
        )
        rows = []
        if show.get("url"):
            rows.append([self._button("Book Tickets", url=show["url"])])
        rows.append(
            [
                self._button(
                    "⬅ Showtimes",
                    f"pftheatre:{event_code}:{date_code}:{theatre_id}:0",
                )
            ]
        )
        self._send(chat_id, text, rows)

    def _history(self, chat_id, user_id, page=0, event_code=None):
        state = self.store.read()
        user = state.get("users", {}).get(str(user_id), {})
        if not user.get("theatre_preferences_saved") or not user.get(
            "favourite_theatres"
        ):
            self._open_favourites(chat_id, user_id, 0)
            return
        priority_codes = set(user.get("priorities", {}))
        events = [
            event
            for event in state.get("events", [])
            if event.get("movie_id") in priority_codes
            and event.get("theatre_id") in user.get("favourite_theatres", {})
            and (event_code is None or event.get("movie_id") == event_code)
        ]
        events.sort(key=lambda event: event.get("detected_at", ""), reverse=True)
        page_count = max(1, (len(events) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, page_count - 1))
        selected = events[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        lines = ["📜 EVENT HISTORY"]
        for index, event in enumerate(selected, start=page * PAGE_SIZE + 1):
            lines.append(
                f"\n{index}. {event.get('detected_at', 'Unknown time')}\n"
                f"🎬 {event.get('movie_name', 'Movie')}\n"
                f"🏛️ {event.get('theatre_name') or 'Theatre information unavailable'}\n"
                f"📍 {event.get('location') or 'Theatre information unavailable'}\n"
                f"{event.get('event_type', '').replace('_', ' ')}: "
                f"{event.get('summary', '')}"
            )
        if not events:
            lines.append(
                "\nNo recorded events for your priority movies in your "
                "favourite theatres."
            )
        lines.append(f"\nPage {page + 1}/{page_count}")
        rows = []
        nav = []
        if page > 0:
            nav.append(self._button("⬅ Previous", f"history:{page - 1}"))
        if page + 1 < page_count:
            nav.append(self._button("Next ➡", f"history:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([self._button("⭐ My Priorities", "priorities:0")])
        rows.append([self._button("🏛️ Favourite Theatres", "menu:favourites")])
        self._send(chat_id, "\n".join(lines), rows)

    def _save_favourites(self, chat_id, user_id):
        user = self.store.user(user_id)
        selected = user.get("ui", {}).get("theatre_selection_draft", [])
        if not selected:
            self._send(
                chat_id,
                "Select at least one favourite theatre before saving. "
                "Priority movies and alerts never fall back to other venues.",
            )
            self._open_favourites(chat_id, user_id, 0)
            return
        try:
            self.store.save_favourite_theatres(user_id, selected)
        except (RuntimeError, ValueError) as exc:
            _log_interaction("favourite_save_failed", type(exc).__name__, str(exc))
            self._send(
                chat_id,
                f"Could not save favourite theatres: {exc}",
                [[self._button("🔄 Reload Theatres", "menu:favourites")]],
            )
            return
        saved = self.store.user(user_id).get("favourite_theatres", {})
        if set(saved) != set(selected):
            _log_interaction(
                "favourite_save_mismatch",
                "save_favourites",
                f"requested={len(selected)} saved={len(saved)}",
            )
            self._send(
                chat_id,
                "The saved selection did not match what you selected. "
                "Please reopen Favourite Theatres and try again.",
                [[self._button("🏛️ Favourite Theatres", "menu:favourites")]],
            )
            return
        self.monitor.trigger()
        self._send(
            chat_id,
            f"Saved {len(saved)} "
            "favourite theatre(s). Priority browsing and monitoring now use "
            "only this selection.",
        )
        self._main_menu(chat_id, user_id)

    def _search_favourites(self, chat_id, user_id, query):
        self.store.set_ui(user_id, "theatre_search", query.strip())
        self._open_favourites(chat_id, user_id, 0)

    def _refresh_movie(self, chat_id, user_id, event_code):
        state = self.store.read()
        priority = state.get("users", {}).get(str(user_id), {}).get(
            "priorities", {}
        ).get(event_code)
        snapshot = state.get("snapshots", {}).get(event_code, {})
        movie = {
            "event_code": event_code,
            "name": (
                priority.get("name")
                if priority
                else snapshot.get("name", event_code)
            ),
            "url": (
                priority.get("url")
                if priority
                else snapshot.get("url", "")
            ),
        }
        if not snapshot and not priority:
            self._send(chat_id, "Movie snapshot not found; refresh the catalogue first.")
            return
        self._send(chat_id, f"Checking {movie['name']} now…")

        def work():
            try:
                self.monitor.refresh_movie(event_code, movie)
                self._send(chat_id, f"Check completed for {movie['name']}.")
            except (RuntimeError, requests.RequestException) as exc:
                self.store.record_scan_error(event_code, exc)
                self._send(chat_id, f"Check failed; previous data was kept.\n{exc}")

        threading.Thread(
            target=work,
            name=f"refresh-{event_code}",
            daemon=True,
        ).start()

    def _handle_callback(self, callback):
        message = callback.get("message", {})
        chat_id = message.get("chat", {}).get("id")
        user_id = callback.get("from", {}).get("id")
        if chat_id is None or user_id is None:
            self.client.answer_callback(callback["id"], "This action is unavailable.")
            return
        self.store.ensure_user(user_id, chat_id)
        data = str(callback.get("data", ""))
        _log_interaction("button_click", action=data)
        self.client.answer_callback(callback["id"])
        parts = data.split(":")
        action = parts[0]

        if action == "refresh" and len(parts) in (1, 2):
            if len(parts) > 1 and parts[1] == "favourites":
                self._start_refresh(chat_id, user_id, after="favourites")
            else:
                self._start_refresh(chat_id)
        elif action == "catalog":
            self._send_catalog(chat_id, int(parts[1]))
        elif action == "movie":
            self._movie_page(chat_id, user_id, parts[1], int(parts[2]))
        elif action == "shows":
            self._movie_page(chat_id, user_id, parts[1], int(parts[2]))
        elif action == "detail":
            self._show_detail(chat_id, user_id, parts[1], int(parts[2]))
        elif action == "priority" and parts[1] == "add":
            movie = next(
                (
                    item
                    for item in self.store.read().get("catalog", [])
                    if item["event_code"] == parts[2]
                ),
                None,
            )
            if movie is None:
                snapshot = self.store.read()["snapshots"].get(parts[2], {})
                if snapshot:
                    movie = {
                        "event_code": parts[2],
                        "name": snapshot.get("name", parts[2]),
                        "url": snapshot.get("url", ""),
                    }
            if movie is None:
                self._send(chat_id, "Movie not found in the current saved catalogue.")
            else:
                self.store.set_priority(user_id, movie)
                self._send(
                    chat_id,
                    f"⭐ {movie['name']} added to your priority list. "
                    "Monitoring starts in the background.",
                    [[self._button("⭐ My Priorities", "priorities:0")]],
                )
        elif action == "priority" and parts[1] == "remove":
            self.store.set_priority(
                user_id,
                {"event_code": parts[2], "name": "", "url": ""},
                enabled=False,
            )
            self._priorities(chat_id, user_id, 0)
        elif action == "priorities":
            self._priorities(chat_id, user_id, int(parts[1]))
        elif action == "monitor":
            enabled = parts[2] == "resume"
            self.store.set_priority_option(
                user_id, parts[1], "monitoring_enabled", enabled
            )
            self._priorities(chat_id, user_id, 0)
        elif action == "notify":
            enabled = parts[2] == "on"
            self.store.set_priority_option(
                user_id, parts[1], "notification_enabled", enabled
            )
            self._priorities(chat_id, user_id, 0)
        elif action == "refreshmovie":
            self._refresh_movie(chat_id, user_id, parts[1])
        elif action == "filters":
            self._filter_menu(chat_id, parts[1])
        elif action == "filtervalues":
            self._filter_values(chat_id, parts[1], parts[2])
        elif action == "filterset":
            code, field, index = parts[1], parts[2], int(parts[3])
            snapshot = self.store.read()["snapshots"].get(code, {})
            key = {
                "date": "date",
                "theatre": "venue",
                "time": "time",
                "category": "category",
                "status": "status",
            }.get(field)
            values = sorted(
                {
                    str(show.get(key))
                    for show in snapshot.get("shows", {}).values()
                    if key and show.get(key)
                }
            )
            if index < len(values):
                filters = (
                    self.store.user(user_id)
                    .get("ui", {})
                    .get("filters", {})
                    .get(code, {})
                )
                filters[field] = values[index]
                self.store.set_ui(user_id, "filters", {
                    **self.store.user(user_id).get("ui", {}).get("filters", {}),
                    code: filters,
                })
            self._movie_page(chat_id, user_id, code, 0)
        elif action == "clearfilters":
            filters = (
                self.store.user(user_id)
                .get("ui", {})
                .get("filters", {})
            )
            filters.pop(parts[1], None)
            self.store.set_ui(user_id, "filters", filters)
            self._movie_page(chat_id, user_id, parts[1], 0)
        elif action == "only":
            code = parts[1]
            filters = (
                self.store.user(user_id)
                .get("ui", {})
                .get("filters", {})
                .get(code, {})
            )
            filters["available_only"] = not filters.get("available_only", False)
            all_filters = (
                self.store.user(user_id)
                .get("ui", {})
                .get("filters", {})
            )
            all_filters[code] = filters
            self.store.set_ui(user_id, "filters", all_filters)
            self._movie_page(chat_id, user_id, code, 0)
        elif action == "sortmenu":
            self._sort_menu(chat_id, parts[1])
        elif action == "sort":
            code, ordering = parts[1], parts[2]
            all_filters = (
                self.store.user(user_id)
                .get("ui", {})
                .get("filters", {})
            )
            filters = all_filters.get(code, {})
            filters["sort"] = ordering
            all_filters[code] = filters
            self.store.set_ui(user_id, "filters", all_filters)
            self._movie_page(chat_id, user_id, code, 0)
        elif action == "history":
            self._history(chat_id, user_id, int(parts[1]))
        elif action == "menu":
            destination = parts[1] if len(parts) > 1 else "home"
            if destination == "home":
                self._main_menu(chat_id, user_id)
            elif destination == "discover":
                self._start_refresh(chat_id)
            elif destination == "priorities":
                self._priorities(chat_id, user_id)
            elif destination == "favourites":
                self._open_favourites(
                    chat_id, user_id, 0, reset_draft=True
                )
            elif destination == "notifications":
                self._priorities(chat_id, user_id)
            elif destination == "refresh":
                self._start_refresh(chat_id, user_id, after="menu")
        elif action == "favourites":
            self._open_favourites(chat_id, user_id, int(parts[1]))
        elif action == "favtoggle":
            page, index = int(parts[1]), int(parts[2])
            page = max(0, page)
            user = self.store.user(user_id)
            search = user.get("ui", {}).get("theatre_search", "").strip().casefold()
            theatres = self._available_theatres()
            if search:
                theatres = [
                    theatre
                    for theatre in theatres
                    if search in theatre.get("theatre_name", "").casefold()
                    or search in theatre.get("location", "").casefold()
                ]
            selected_theatres = theatres[page * 6:(page + 1) * 6]
            if 0 <= index < len(theatres):
                theatre = theatres[index]
                if theatre in selected_theatres:
                    self._toggle_favourite(user_id, theatre)
            self._open_favourites(chat_id, user_id, page)
        elif action == "favselectpage":
            page = int(parts[1])
            user = self.store.user(user_id)
            search = user.get("ui", {}).get("theatre_search", "").strip().casefold()
            theatres = self._available_theatres()
            if search:
                theatres = [
                    theatre
                    for theatre in theatres
                    if search in theatre.get("theatre_name", "").casefold()
                    or search in theatre.get("location", "").casefold()
                ]
            page = max(0, page)
            page_theatres = theatres[page * 6:(page + 1) * 6]
            draft = set(
                user.get("ui", {}).get("theatre_selection_draft", [])
            )
            draft.update(theatre["theatre_id"] for theatre in page_theatres)
            self.store.set_ui(user_id, "theatre_selection_draft", sorted(draft))
            self._open_favourites(chat_id, user_id, page)
        elif action == "favclear":
            self.store.set_ui(user_id, "theatre_selection_draft", [])
            self._open_favourites(chat_id, user_id, 0)
        elif action == "favsave":
            self._save_favourites(chat_id, user_id)
        elif action == "favsearch":
            self._send(
                chat_id,
                "Send `search <theatre name or location>` to filter the theatre "
                "list, or use /favourites to return to the list.",
            )
        elif action == "favsearchclear":
            self.store.set_ui(user_id, "theatre_search", "")
            self._open_favourites(chat_id, user_id, 0)
        elif action == "pfmovie":
            self._priority_movie(chat_id, user_id, parts[1])
        elif action == "pfdate":
            self._priority_date(chat_id, user_id, parts[1], parts[2])
        elif action == "pftheatre":
            self._priority_theatre(
                chat_id,
                user_id,
                parts[1],
                parts[2],
                parts[3],
                int(parts[4]) if len(parts) > 4 else 0,
            )
        elif action == "pfshow":
            self._priority_show(
                chat_id,
                user_id,
                parts[1],
                parts[2],
                parts[3],
                int(parts[4]),
            )
        else:
            self._send(
                chat_id,
                "This button is no longer available. Open the menu and try again.",
            )

    def handle_update(self, update):
        callback = update.get("callback_query")
        if callback:
            self._handle_callback(callback)
            return
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat", {})
        sender = message.get("from", {})
        chat_id = chat.get("id")
        user_id = sender.get("id")
        if chat_id is None or user_id is None:
            return
        self.store.ensure_user(user_id, chat_id)
        text = str(message.get("text", "")).strip()
        command = text.split(maxsplit=1)[0].split("@", 1)[0].casefold()
        normalized = text.casefold()
        if normalized.startswith("search theatres "):
            _log_interaction("command", "search_theatres")
        elif normalized.startswith("search "):
            _log_interaction("command", "search")
        else:
            _log_interaction("command", command or "empty")
        if command == "/start" or normalized == "start":
            self._main_menu(chat_id, user_id)
        elif command in ("/movies",) or normalized in (
            "show movies",
            "movie list",
        ):
            self._start_refresh(chat_id)
        elif command in ("/refresh", "refresh") or normalized == "refresh movies":
            user = self.store.user(user_id)
            if not user.get("theatre_preferences_saved"):
                self._start_refresh(chat_id, user_id, after="favourites")
            else:
                self._start_refresh(chat_id)
        elif command == "/priorities" or normalized in (
            "my priority movies",
            "my priorities",
        ):
            self._priorities(chat_id, user_id)
        elif command in ("/favourites", "/theatres") or normalized == (
            "my favourite theatres"
        ):
            self._open_favourites(
                chat_id, user_id, 0, reset_draft=True
            )
        elif command in ("/history", "event history"):
            self._history(chat_id, user_id, 0)
        elif normalized.startswith("search theatres "):
            self._search_favourites(chat_id, user_id, text[16:])
        elif normalized.startswith("search "):
            self._search_favourites(chat_id, user_id, text[7:])
        elif command in ("/help", "help"):
            self._send(
                chat_id,
                "Commands:\n/start — open the BMS_AGENT menu\n"
                "/refresh — refresh the catalogue\n"
                "/priorities — manage priority movies\n"
                "/favourites — choose favourite theatres\n"
                "/history — view recorded events\n\n"
                "Priority movie listings, dates, theatres, showtimes, history, "
                "and alerts are restricted to your favourite theatres.",
                [[self._button("🏠 Main Menu", "menu:home")]],
            )
        else:
            self._send(
                chat_id,
                "Use /start to browse bookable movies, /priorities to manage "
                "monitoring, or /help for commands.",
            )

    def run(self, stop_event):
        offset = int(self.store.read().get("bot_offset", 0))
        delay = 1
        while not stop_event.is_set():
            try:
                updates = self.client.get_updates(offset)
                delay = 1
            except TelegramConfigurationError:
                raise
            except RuntimeError as exc:
                print(f"Telegram polling error: {exc}")
                stop_event.wait(delay)
                delay = min(delay * 2, 60)
                continue
            for update in updates:
                try:
                    self.handle_update(update)
                except (
                    RuntimeError,
                    requests.RequestException,
                    ValueError,
                    IndexError,
                ) as exc:
                    print(f"Could not handle Telegram update: {exc}")
                    _log_interaction(
                        "update_failed", type(exc).__name__, str(exc)
                    )
                    callback = update.get("callback_query")
                    if callback:
                        try:
                            chat_id = (
                                callback.get("message", {})
                                .get("chat", {})
                                .get("id")
                            )
                            if chat_id is not None:
                                self._send(
                                    chat_id,
                                    "Could not complete that action. "
                                    "Please try again from the menu.",
                                )
                        except RuntimeError as answer_error:
                            _log_interaction(
                                "callback_error_response_failed",
                                type(answer_error).__name__,
                                str(answer_error),
                            )
                offset = max(offset, int(update.get("update_id", 0)) + 1)
                self.store.update(lambda state: state.__setitem__("bot_offset", offset))
