"""Atomic JSON persistence for bot users, snapshots, events, and deliveries."""

import copy
import json
import os
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from theatre import theatre_identifier


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PersistentStore:
    def __init__(self, path=None, legacy_path=None):
        project_dir = Path(__file__).resolve().parent
        self.path = Path(
            path or os.getenv("BMS_DATA_FILE", project_dir / "bms_bot_data.json")
        )
        self.legacy_path = Path(
            legacy_path or project_dir / "bms_state.json"
        )
        self._lock = threading.RLock()
        self._state = self._load()

    @staticmethod
    def _empty_state():
        return {
            "version": 1,
            "users": {},
            "snapshots": {},
            "events": [],
            "catalog": [],
            "theatres": [],
            "catalog_updated_at": None,
            "bot_offset": 0,
        }

    def _load(self):
        try:
            with self.path.open(encoding="utf-8") as file:
                state = json.load(file)
        except FileNotFoundError:
            state = self._empty_state()
            self._seed_legacy_snapshot(state)
            return state
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"{self.path} contains invalid JSON; fix it before starting."
            ) from exc

        if not isinstance(state, dict) or state.get("version") != 1:
            raise RuntimeError(
                f"{self.path} has an unsupported or invalid state format."
            )
        for key, default in self._empty_state().items():
            state.setdefault(key, default)
        if not all(
            isinstance(state[key], expected)
            for key, expected in (
                ("users", dict),
                ("snapshots", dict),
                ("events", list),
                ("catalog", list),
                ("theatres", list),
            )
        ):
            raise RuntimeError(f"{self.path} contains invalid state sections.")
        return state

    def _seed_legacy_snapshot(self, state):
        try:
            with self.legacy_path.open(encoding="utf-8") as file:
                legacy = json.load(file)
        except FileNotFoundError:
            return
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"{self.legacy_path} contains invalid JSON; fix it before "
                "starting the bot."
            ) from exc
        movies = legacy.get("movies") if isinstance(legacy, dict) else None
        if isinstance(movies, dict):
            state["snapshots"] = movies
            state["theatres"] = self._theatre_records(movies)

    @staticmethod
    def _theatre_records(snapshots):
        venues = {}
        for snapshot in snapshots.values():
            for show in snapshot.get("shows", {}).values():
                theatre_id = theatre_identifier(
                    show.get("venue_code"), show.get("venue")
                )
                if not theatre_id:
                    continue
                current = venues.get(theatre_id, {})
                location = show.get("venue_location") or current.get(
                    "location", ""
                )
                venues[theatre_id] = {
                    "theatre_id": theatre_id,
                    "venue_code": str(show.get("venue_code") or ""),
                    "theatre_name": str(show.get("venue") or ""),
                    "location": location,
                }
        return sorted(
            venues.values(),
            key=lambda theatre: (
                theatre["theatre_name"].casefold(),
                theatre["location"].casefold(),
                theatre["theatre_id"],
            ),
        )

    @staticmethod
    def _refresh_user_theatres(state):
        current = {
            theatre["theatre_id"]: theatre
            for theatre in state.get("theatres", [])
        }
        for user in state["users"].values():
            favourites = user.get("favourite_theatres", {})
            for theatre_id, favourite in favourites.items():
                theatre = current.get(theatre_id)
                if theatre is None:
                    continue
                favourite["venue_code"] = theatre["venue_code"]
                favourite["theatre_name"] = theatre["theatre_name"]
                favourite["location"] = (
                    theatre.get("location")
                    or favourite.get("location")
                    or ""
                )

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as file:
                temp_path = Path(file.name)
                json.dump(self._state, file, indent=2, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_path, self.path)
        except OSError as exc:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise RuntimeError(f"Could not save local bot state to {self.path}.") from exc

    def read(self):
        with self._lock:
            return copy.deepcopy(self._state)

    def update(self, callback):
        with self._lock:
            before = copy.deepcopy(self._state)
            try:
                callback(self._state)
                self._save()
            except Exception:
                self._state = before
                raise

    def ensure_user(self, user_id, chat_id):
        user_key = str(user_id)

        def mutate(state):
            user = state["users"].setdefault(
                user_key,
                {
                    "chat_id": str(chat_id),
                    "priorities": {},
                    "ui": {},
                    "created_at": utc_now(),
                },
            )
            user["chat_id"] = str(chat_id)

        self.update(mutate)
        return self.user(user_key)

    def user(self, user_id):
        return self.read()["users"].get(str(user_id), {})

    def set_priority(self, user_id, movie, enabled=True):
        user_key = str(user_id)
        event_code = movie["event_code"]

        def mutate(state):
            user = state["users"].setdefault(
                user_key,
                {"chat_id": user_key, "priorities": {}, "ui": {}},
            )
            priorities = user.setdefault("priorities", {})
            if enabled:
                existing = priorities.get(event_code, {})
                priorities[event_code] = {
                    "name": movie["name"],
                    "url": movie["url"],
                    "active": True,
                    "monitoring_enabled": existing.get(
                        "monitoring_enabled", True
                    ),
                    "notification_enabled": existing.get(
                        "notification_enabled", True
                    ),
                    "added_at": existing.get("added_at", utc_now()),
                }
            else:
                priorities.pop(event_code, None)

        self.update(mutate)

    def set_priority_option(self, user_id, event_code, option, value):
        if option not in ("monitoring_enabled", "notification_enabled"):
            raise ValueError(f"Unsupported priority option: {option}")

        def mutate(state):
            user = state["users"].get(str(user_id), {})
            priority = user.get("priorities", {}).get(event_code)
            if priority is None:
                return
            priority[option] = bool(value)

        self.update(mutate)

    def set_ui(self, user_id, key, value):
        def mutate(state):
            user = state["users"].setdefault(
                str(user_id), {"priorities": {}, "ui": {}}
            )
            user.setdefault("ui", {})[key] = value

        self.update(mutate)

    def save_favourite_theatres(self, user_id, theatre_ids):
        requested = {str(theatre_id) for theatre_id in theatre_ids}
        now = utc_now()

        def mutate(state):
            user = state["users"].setdefault(
                str(user_id), {"priorities": {}, "ui": {}}
            )
            previous = user.get("favourite_theatres", {})
            available = {
                theatre["theatre_id"]: theatre
                for theatre in state.get("theatres", [])
            }
            for theatre in self._theatre_records(state["snapshots"]):
                existing = available.get(theatre["theatre_id"], {})
                if not theatre.get("location"):
                    theatre["location"] = existing.get("location", "")
                available[theatre["theatre_id"]] = theatre
            unresolved = requested - available.keys() - previous.keys()
            if unresolved:
                raise ValueError(
                    "One or more selected theatres are no longer in the "
                    "available theatre list. Refresh the list and select again."
                )
            selected = {}
            for theatre_id in requested:
                theatre = available.get(theatre_id) or previous.get(theatre_id)
                old = previous.get(theatre_id, {})
                selected[theatre_id] = {
                    "theatre_id": theatre_id,
                    "venue_code": theatre["venue_code"],
                    "theatre_name": theatre["theatre_name"],
                    "location": theatre.get("location") or "",
                    "selected_at": old.get("selected_at", now),
                }
            user["favourite_theatres"] = selected
            user["theatre_preferences_saved"] = True
            ui = user.setdefault("ui", {})
            ui.pop("theatre_selection_draft", None)
            ui.pop("theatre_search", None)

        self.update(mutate)

    def update_theatres(self, theatres):
        def mutate(state):
            state["theatres"] = theatres
            self._refresh_user_theatres(state)

        self.update(mutate)

    def record_scan(self, event_code, snapshot, events, checked_at=None):
        checked_at = checked_at or utc_now()

        def mutate(state):
            state["snapshots"][event_code] = snapshot
            state["theatres"] = self._theatre_records(state["snapshots"])
            self._refresh_user_theatres(state)
            existing_ids = {event["event_id"] for event in state["events"]}
            for event in events:
                if event["event_id"] not in existing_ids:
                    event["detected_at"] = checked_at
                    event.setdefault("deliveries", {})
                    state["events"].append(event)
                    existing_ids.add(event["event_id"])
            for user in state["users"].values():
                priority = user.get("priorities", {}).get(event_code)
                if priority is not None:
                    priority["last_successful_check"] = checked_at
                    priority["last_error"] = None
                    priority["failure_count"] = 0
                    priority["retry_after"] = None

        self.update(mutate)

    def record_scan_error(self, event_code, error):
        def mutate(state):
            for user in state["users"].values():
                priority = user.get("priorities", {}).get(event_code)
                if priority is not None:
                    priority["last_error"] = str(error)[:300]
                    failures = int(priority.get("failure_count", 0)) + 1
                    priority["failure_count"] = failures
                    delay = min(60 * (2 ** max(0, failures - 1)), 3600)
                    priority["retry_after"] = (
                        datetime.now(timezone.utc)
                        + timedelta(seconds=delay)
                    ).isoformat(timespec="seconds")

        self.update(mutate)

    def record_delivery(
        self, event_id, user_id, status, error=None, attempts=1
    ):
        def mutate(state):
            for event in state["events"]:
                if event.get("event_id") == event_id:
                    delivery = {
                        "status": status,
                        "updated_at": utc_now(),
                        "error": str(error)[:300] if error else None,
                        "attempts": attempts,
                    }
                    if status == "failed":
                        delay = min(60 * (2 ** max(0, attempts - 1)), 1800)
                        delivery["next_retry_at"] = (
                            datetime.now(timezone.utc)
                            + timedelta(seconds=delay)
                        ).isoformat(timespec="seconds")
                    event.setdefault("deliveries", {})[str(user_id)] = delivery
                    return

        self.update(mutate)

    def update_catalog(self, movies, snapshots, events, theatres=None):
        checked_at = utc_now()

        def mutate(state):
            state["catalog"] = movies
            state["catalog_updated_at"] = checked_at
            if theatres is not None:
                state["theatres"] = theatres
            else:
                state["theatres"] = self._theatre_records(snapshots)
            existing_ids = {event["event_id"] for event in state["events"]}
            for event_code, snapshot in snapshots.items():
                state["snapshots"][event_code] = snapshot
            self._refresh_user_theatres(state)
            for event in events:
                if event["event_id"] not in existing_ids:
                    event["detected_at"] = checked_at
                    event.setdefault("deliveries", {})
                    state["events"].append(event)
                    existing_ids.add(event["event_id"])
            for user in state["users"].values():
                for code in user.get("priorities", {}):
                    if code in snapshots:
                        priority = user["priorities"][code]
                        priority["last_successful_check"] = checked_at
                        priority["last_error"] = None
                        priority["failure_count"] = 0
                        priority["retry_after"] = None

        self.update(mutate)
