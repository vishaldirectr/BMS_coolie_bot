"""BookMyShow refresh, snapshot comparison, event history, and polling."""

import hashlib
import os
import threading
import time
from datetime import datetime, timezone

import requests

import bms_services as bms
from storage import utc_now
from theatre import format_date, theatre_identifier


STATUS_NAMES = {
    "0": "SOLD OUT",
    "1": "ALMOST FULL",
    "2": "FILLING FAST",
    "3": "AVAILABLE",
}
BOOKABLE = {"1", "2", "3"}


def _show_id(key):
    return "|".join(key.split("|")[:3])


def _event(
    event_type,
    event_code,
    movie,
    show_id,
    summary,
    previous,
    current,
    details=None,
):
    detected_at = utc_now()
    identity = "|".join(
        (
            event_type,
            event_code,
            show_id,
            repr(previous),
            repr(current),
            detected_at,
        )
    )
    details = details or {}
    return {
        "event_id": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
        "movie_id": event_code,
        "movie_name": movie.get("name", event_code),
        "show_id": show_id,
        "event_type": event_type,
        "summary": summary,
        "url": movie.get("url", ""),
        "theatre_id": theatre_identifier(
            details.get("venue_code"), details.get("venue")
        ),
        "theatre_name": details.get("venue", ""),
        "location": details.get("venue_location", ""),
        "date": details.get("date", ""),
        "time": details.get("time", ""),
        "screen": details.get("screen", ""),
        "category": details.get("category", ""),
        "price": details.get("price", ""),
        "status": STATUS_NAMES.get(
            str(details.get("status", "")),
            str(details.get("status", "")),
        ),
        "previous_value": previous,
        "current_value": current,
    }


def compare_snapshots(event_code, old_movie, new_movie, theatre_ids=None):
    """Create events only from differences between successful snapshots."""
    if not old_movie:
        return []

    def scoped_shows(snapshot):
        shows = snapshot.get("shows", {})
        if theatre_ids is None:
            return shows
        return {
            key: show
            for key, show in shows.items()
            if theatre_identifier(
                show.get("venue_code"), show.get("venue")
            ) in theatre_ids
        }

    old_shows = scoped_shows(old_movie)
    new_shows = scoped_shows(new_movie)
    events = []
    old_show_ids = {_show_id(key) for key in old_shows}
    new_show_ids = {_show_id(key) for key in new_shows}
    old_theatres = {
        theatre_identifier(show.get("venue_code"), show.get("venue")): show
        for show in old_shows.values()
        if theatre_identifier(show.get("venue_code"), show.get("venue"))
    }
    new_theatres = {
        theatre_identifier(show.get("venue_code"), show.get("venue")): show
        for show in new_shows.values()
        if theatre_identifier(show.get("venue_code"), show.get("venue"))
    }
    for theatre_id in sorted(new_theatres.keys() - old_theatres.keys()):
        theatre = new_theatres[theatre_id]
        events.append(
            _event(
                "NEW_THEATRE",
                event_code,
                new_movie,
                theatre_id,
                f"New theatre listed: {theatre.get('venue', 'Unknown')}",
                None,
                theatre,
                theatre,
            )
        )

    old_dates = {
        (
            theatre_identifier(show.get("venue_code"), show.get("venue")),
            show.get("date"),
        )
        for show in old_shows.values()
        if show.get("date")
    }
    new_dates = {}
    for show in new_shows.values():
        if show.get("date"):
            theatre_id = theatre_identifier(
                show.get("venue_code"), show.get("venue")
            )
            new_dates.setdefault((theatre_id, show["date"]), show)
    for (theatre_id, date_code), show in new_dates.items():
        if (theatre_id, date_code) not in old_dates:
            events.append(
                _event(
                    "NEW_DATE",
                    event_code,
                    new_movie,
                    f"{theatre_id}|{date_code}",
                    (
                        f"New date listed at {show.get('venue', 'Unknown')}: "
                        f"{format_date(date_code)}"
                    ),
                    None,
                    date_code,
                    show,
                )
            )

    for key, show in new_shows.items():
        previous = old_shows.get(key)
        show_id = _show_id(key)
        if previous is None:
            event_type = "NEW_CATEGORY" if show_id in old_show_ids else "NEW_SHOW"
            summary = (
                f"New ticket category: {show.get('category', 'Unknown')} "
                f"at {show.get('venue', 'Unknown')} on "
                f"{format_date(show.get('date'))} {show.get('time', '')}"
            )
            events.append(
                _event(
                    event_type,
                    event_code,
                    new_movie,
                    show_id,
                    summary,
                    None,
                    show,
                    show,
                )
            )
            continue

        old_price = previous.get("price")
        new_price = show.get("price")
        if old_price != new_price:
            events.append(
                _event(
                    "PRICE_CHANGED",
                    event_code,
                    new_movie,
                    show_id,
                    (
                        f"{show.get('category', 'Ticket')} price changed at "
                        f"{show.get('venue', 'Unknown')}: "
                        f"₹{old_price} → ₹{new_price}"
                    ),
                    old_price,
                    new_price,
                    show,
                )
            )

        old_status = str(previous.get("status", ""))
        new_status = str(show.get("status", ""))
        if old_status != new_status:
            event_type = (
                "TICKETS_OPENED"
                if old_status == "0" and new_status in BOOKABLE
                else "STATUS_CHANGED"
            )
            events.append(
                _event(
                    event_type,
                    event_code,
                    new_movie,
                    show_id,
                    (
                        f"{show.get('category', 'Ticket')} at "
                        f"{show.get('venue', 'Unknown')}: "
                        f"{STATUS_NAMES.get(old_status, 'UNKNOWN')} → "
                        f"{STATUS_NAMES.get(new_status, 'UNKNOWN')}"
                    ),
                    STATUS_NAMES.get(old_status, old_status or "UNKNOWN"),
                    STATUS_NAMES.get(new_status, new_status or "UNKNOWN"),
                    show,
                )
            )
        old_screen = previous.get("screen")
        new_screen = show.get("screen")
        if old_screen != new_screen and (old_screen or new_screen):
            events.append(
                _event(
                    "SHOW_DETAILS_CHANGED",
                    event_code,
                    new_movie,
                    show_id,
                    (
                        f"Screen/format details changed at "
                        f"{show.get('venue', 'Unknown')}: "
                        f"{old_screen or 'not listed'} → "
                        f"{new_screen or 'not listed'}"
                    ),
                    old_screen,
                    new_screen,
                    show,
                )
            )

    for key, show in old_shows.items():
        if key in new_shows:
            continue
        events.append(
            _event(
                "SHOW_REMOVED",
                event_code,
                new_movie,
                _show_id(key),
                (
                    f"Show/category no longer listed at "
                    f"{show.get('venue', 'Unknown')} on "
                    f"{format_date(show.get('date'))} {show.get('time', '')}; "
                    "cancellation is not confirmed"
                ),
                show,
                None,
                show,
            )
        )

    return events


def _has_bookable_show(snapshot):
    return any(
        str(show.get("status", "")) in BOOKABLE
        for show in snapshot.get("shows", {}).values()
    )


def _movie_summary(event_code, snapshot):
    shows = [
        show
        for show in snapshot.get("shows", {}).values()
        if str(show.get("status", "")) in BOOKABLE
    ]
    theatres = {show.get("venue", "") for show in shows if show.get("venue")}
    showtime_ids = {
        "|".join(
            (
                str(show.get("venue_code") or show.get("venue", "")),
                str(show.get("session_id") or show.get("time_code", "")),
                str(show.get("date", "")),
                str(show.get("time_code") or show.get("time", "")),
            )
        )
        for show in shows
    }
    dates = sorted(show.get("date", "") for show in shows if show.get("date"))
    return {
        "event_code": event_code,
        "name": snapshot.get("name", event_code),
        "url": snapshot.get("url", ""),
        "bookable_count": len(shows),
        "showtime_count": len(showtime_ids),
        "theatre_count": len(theatres),
        "earliest_date": dates[0] if dates else "",
        "latest_date": dates[-1] if dates else "",
        "language": snapshot.get("language", ""),
    }


class MonitoringService:
    def __init__(self, store, notify=None, interval=None):
        self.store = store
        self.notify = notify
        self.interval = max(
            60,
            int(interval or os.getenv("BMS_POLL_INTERVAL_SECONDS", "300")),
        )
        self._scan_lock = threading.Lock()
        self._wake_event = threading.Event()

    def trigger(self):
        self._wake_event.set()

    def _scan(self, movie):
        region = bms.resolve_region(bms.CITY_SLUG)
        responses = bms.scan_movie(movie, region)
        state = bms.build_state(
            [movie], {movie["event_code"]: responses}
        )["movies"][movie["event_code"]]
        if not isinstance(state.get("shows"), dict):
            raise RuntimeError(f"Incomplete BookMyShow scan for {movie['name']}.")
        return state

    @staticmethod
    def _active_favourite_ids(user):
        return set(user.get("favourite_theatres", {}))

    @classmethod
    def _monitoring_theatres(cls, state, event_code):
        theatre_ids = set()
        for user in state["users"].values():
            priority = user.get("priorities", {}).get(event_code)
            if (
                priority
                and priority.get("active", True)
                and priority.get("monitoring_enabled", True)
            ):
                theatre_ids.update(cls._active_favourite_ids(user))
        return theatre_ids

    def refresh_movie(self, event_code, movie):
        with self._scan_lock:
            current_state = self.store.read()
            previous = current_state["snapshots"].get(event_code)
            theatre_ids = self._monitoring_theatres(
                current_state, event_code
            )
            snapshot = self._scan(movie)
            events = compare_snapshots(
                event_code,
                previous,
                snapshot,
                theatre_ids=theatre_ids,
            )
            self.store.record_scan(event_code, snapshot, events)
        self.deliver_pending(event_code)
        return events

    def check_priority_movie(self, event_code, movie):
        return self.refresh_movie(event_code, movie)

    def refresh_catalog(self):
        with self._scan_lock:
            listed_movies = bms.discover_movies()
            new_snapshots = {}
            for movie in listed_movies:
                new_snapshots[movie["event_code"]] = self._scan(movie)

            old_state = self.store.read()
            priority_codes = {
                code
                for user in old_state["users"].values()
                for code, priority in user.get("priorities", {}).items()
                if priority.get("active", True)
                and priority.get("monitoring_enabled", True)
                and user.get("favourite_theatres")
            }
            events = []
            for code in priority_codes:
                previous = old_state["snapshots"].get(code)
                current = new_snapshots.get(code)
                if current is not None:
                    events.extend(
                        compare_snapshots(
                            code,
                            previous,
                            current,
                            theatre_ids=self._monitoring_theatres(
                                old_state, code
                            ),
                        )
                    )

            catalogue = [
                _movie_summary(code, snapshot)
                for code, snapshot in new_snapshots.items()
                if _has_bookable_show(snapshot)
            ]
            catalogue.sort(key=lambda movie: movie["name"].casefold())
            self.store.update_catalog(catalogue, new_snapshots, events)

        self.deliver_pending()
        return catalogue

    def _priority_movies(self):
        state = self.store.read()
        movies = {}
        for user in state["users"].values():
            for code, priority in user.get("priorities", {}).items():
                if priority.get("active", True) and priority.get(
                    "monitoring_enabled", True
                ) and user.get("favourite_theatres"):
                    movies[code] = {
                        "event_code": code,
                        "name": priority["name"],
                        "url": priority["url"],
                        "retry_after": priority.get("retry_after"),
                    }
        return movies

    def deliver_pending(self, event_code=None):
        if self.notify is None:
            return
        state = self.store.read()
        for user_id, user in state["users"].items():
            for code, priority in user.get("priorities", {}).items():
                if event_code and code != event_code:
                    continue
                movie_events = [
                    event
                    for event in state["events"]
                    if event.get("movie_id") == code
                ]
                pending = []
                for event in movie_events:
                    delivery = event.get("deliveries", {}).get(user_id, {})
                    if delivery.get("status") in ("sent", "suppressed"):
                        continue
                    theatre_id = event.get("theatre_id")
                    favourite = user.get("favourite_theatres", {}).get(
                        theatre_id
                    )
                    if favourite is None:
                        continue
                    if (
                        not priority.get("notification_enabled", True)
                        or event.get("detected_at", "")
                        < favourite.get("selected_at", "")
                    ):
                        self.store.record_delivery(
                            event["event_id"],
                            user_id,
                            "suppressed",
                            attempts=0,
                        )
                        continue
                    attempts = int(delivery.get("attempts", 0))
                    if attempts >= 5:
                        continue
                    retry_at = delivery.get("next_retry_at")
                    if retry_at:
                        try:
                            if datetime.fromisoformat(retry_at) > datetime.now(
                                timezone.utc
                            ):
                                continue
                        except ValueError:
                            print(
                                f"Invalid retry timestamp for event "
                                f"{event.get('event_id')}; retrying now."
                            )
                    pending.append((event, attempts + 1))

                chunks = []
                current = []
                current_length = 0
                for event, attempts in pending:
                    formatted = self.format_event(event)
                    extra_length = len(formatted) + (2 if current else 0)
                    if current and current_length + extra_length > 3500:
                        chunks.append(current)
                        current = []
                        current_length = 0
                        extra_length = len(formatted)
                    current.append((event, attempts, formatted))
                    current_length += extra_length
                if current:
                    chunks.append(current)

                for chunk in chunks:
                    message = "\n\n".join(item[2] for item in chunk)
                    try:
                        self.notify(user["chat_id"], message)
                    except (RuntimeError, requests.RequestException) as exc:
                        for event, attempts, _ in chunk:
                            self.store.record_delivery(
                                event["event_id"],
                                user_id,
                                "failed",
                                error=exc,
                                attempts=attempts,
                            )
                    else:
                        for event, attempts, _ in chunk:
                            self.store.record_delivery(
                                event["event_id"],
                                user_id,
                                "sent",
                                attempts=attempts,
                            )

    @staticmethod
    def format_event(event):
        location = event.get("location") or "Theatre information unavailable"
        lines = [
            f"🔔 {event['event_type'].replace('_', ' ')}",
            "",
            f"🎬 {event['movie_name']}",
            f"🏛️ {event.get('theatre_name') or 'Theatre information unavailable'}",
            f"📍 {location}",
        ]
        if event.get("date"):
            lines.append(f"📅 {format_date(event['date'])}")
        if event.get("time"):
            lines.append(f"🕒 {event['time']}")
        if event.get("screen"):
            lines.append(f"🖥 {event['screen']}")
        if event.get("category"):
            lines.append(f"🎟 {event['category']}")
        if event.get("price"):
            lines.append(f"💰 ₹{event['price']}")
        if event.get("status"):
            lines.append(f"Status: {event['status']}")
        lines.extend(("", event.get("summary", ""), event.get("url", "")))
        return (
            "\n".join(line for line in lines if line is not None)
        )

    def run(self, stop_event):
        while not stop_event.is_set():
            self._wake_event.clear()
            for event_code, movie in self._priority_movies().items():
                if stop_event.is_set():
                    break
                retry_after = movie.get("retry_after")
                if retry_after:
                    try:
                        if datetime.fromisoformat(retry_after) > datetime.now(
                            timezone.utc
                        ):
                            continue
                    except ValueError:
                        print(
                            f"Invalid retry timestamp for {event_code}; "
                            "checking now."
                        )
                try:
                    self.check_priority_movie(event_code, movie)
                except (RuntimeError, requests.RequestException) as exc:
                    self.store.record_scan_error(event_code, exc)
                    print(f"Monitor check failed for {event_code}: {exc}")
            self.deliver_pending()
            deadline = time.monotonic() + self.interval
            while not stop_event.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                if self._wake_event.wait(min(remaining, 1)):
                    break
