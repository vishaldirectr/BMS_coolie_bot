import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bot import (
    INTERACTION_LOG,
    TelegramBot,
    TelegramClient,
    configure_interaction_logging,
)
from monitor import MonitoringService, compare_snapshots
from storage import PersistentStore
from theatre import format_date, theatre_identifier


class PersistentStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.state_path = Path(self.temp_dir.name) / "bot-state.json"
        self.store = PersistentStore(
            self.state_path,
            Path(self.temp_dir.name) / "missing-legacy.json",
        )

    def test_priority_and_preferences_persist_across_restart(self):
        self.store.ensure_user(123, 456)
        movie = {
            "event_code": "ET00123456",
            "name": "Example Movie",
            "url": "https://example.test/movie",
        }
        self.store.set_priority(123, movie)
        self.store.set_priority_option(
            123, movie["event_code"], "monitoring_enabled", False
        )

        restarted = PersistentStore(
            self.state_path,
            Path(self.temp_dir.name) / "missing-legacy.json",
        )

        priority = restarted.user(123)["priorities"]["ET00123456"]
        self.assertEqual(priority["name"], "Example Movie")
        self.assertFalse(priority["monitoring_enabled"])

    def test_favourite_theatres_persist_by_exact_code_and_name(self):
        snapshot = {
            "shows": {
                "V1|S1|20261201|Standard": {
                    "venue_code": "V1",
                    "venue": "Cinema",
                    "venue_location": "Central",
                },
                "V2|S1|20261201|Standard": {
                    "venue_code": "V2",
                    "venue": "Cinema",
                    "venue_location": "West",
                },
            }
        }
        self.store.ensure_user(123, 456)
        self.store.record_scan("ET00123456", snapshot, [])
        first_id = theatre_identifier("V1", "Cinema")
        second_id = theatre_identifier("V2", "Cinema")
        self.store.save_favourite_theatres(123, [first_id])

        restarted = PersistentStore(
            self.state_path,
            Path(self.temp_dir.name) / "missing-legacy.json",
        )

        favourites = restarted.user(123)["favourite_theatres"]
        self.assertIn(first_id, favourites)
        self.assertNotIn(second_id, favourites)
        self.assertEqual(favourites[first_id]["location"], "Central")

    def test_saving_resolves_theatres_from_snapshots_when_catalogue_cache_is_empty(self):
        theatre_id = theatre_identifier("V1", "Cinema")
        self.store.ensure_user(123, 456)
        self.store.record_scan(
            "ET00123456",
            {
                "shows": {
                    "V1|S1|20261201|Standard": {
                        "venue_code": "V1",
                        "venue": "Cinema",
                        "venue_location": "Central",
                    }
                }
            },
            [],
        )
        self.store.update(lambda state: state.update({"theatres": []}))

        self.store.save_favourite_theatres(123, [theatre_id])
        restarted = PersistentStore(
            self.state_path,
            Path(self.temp_dir.name) / "missing-legacy.json",
        )

        self.assertIn(theatre_id, restarted.user(123)["favourite_theatres"])
        self.assertEqual(
            restarted.user(123)["favourite_theatres"][theatre_id]["location"],
            "Central",
        )

    def test_save_rejects_unknown_theatre_instead_of_silently_dropping_it(self):
        self.store.ensure_user(123, 456)

        with self.assertRaisesRegex(ValueError, "no longer in the available"):
            self.store.save_favourite_theatres(123, ["unknown-theatre"])

        self.assertFalse(self.store.user(123).get("favourite_theatres"))

    def test_scan_refreshes_saved_theatre_location_when_source_provides_it(self):
        theatre_id = theatre_identifier("V1", "Cinema")
        self.store.ensure_user(123, 456)
        self.store.record_scan(
            "ET00123456",
            {
                "shows": {
                    "V1|S1|20261201|Standard": {
                        "venue_code": "V1",
                        "venue": "Cinema",
                        "venue_location": "",
                    }
                }
            },
            [],
        )
        self.store.save_favourite_theatres(123, [theatre_id])
        self.store.record_scan(
            "ET00123456",
            {
                "shows": {
                    "V1|S1|20261201|Standard": {
                        "venue_code": "V1",
                        "venue": "Cinema",
                        "venue_location": "Central",
                    }
                }
            },
            [],
        )

        self.assertEqual(
            self.store.user(123)["favourite_theatres"][theatre_id]["location"],
            "Central",
        )

    def test_corrupt_store_fails_instead_of_resetting_user_data(self):
        self.state_path.write_text("{", encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "invalid JSON"):
            PersistentStore(
                self.state_path,
                Path(self.temp_dir.name) / "missing-legacy.json",
            )

    def test_existing_checker_snapshot_is_used_as_initial_baseline(self):
        legacy_path = Path(self.temp_dir.name) / "legacy.json"
        legacy_snapshot = {
            "movies": {
                "ET00123456": {
                    "name": "Example Movie",
                    "url": "https://example.test/movie",
                    "shows": {},
                    "dates": {},
                }
            }
        }
        legacy_path.write_text(json.dumps(legacy_snapshot), encoding="utf-8")

        store = PersistentStore(self.state_path, legacy_path)

        self.assertEqual(
            store.read()["snapshots"],
            legacy_snapshot["movies"],
        )

    def test_failed_write_rolls_memory_back(self):
        before = self.store.read()
        self.store._save = Mock(side_effect=RuntimeError("disk error"))

        with self.assertRaisesRegex(RuntimeError, "disk error"):
            self.store.ensure_user(123, 456)

        self.assertEqual(self.store.read(), before)


class SnapshotComparisonTests(unittest.TestCase):
    def setUp(self):
        self.old = {
            "name": "Example Movie",
            "url": "https://example.test/movie",
            "dates": {"20261201": "AVAILABLE"},
            "shows": {
                "venue1|session1|20261201|Standard": {
                    "venue_code": "V1",
                    "venue": "Example Theatre",
                    "venue_location": "Central",
                    "date": "20261201",
                    "time": "7:00 PM",
                    "category": "Standard",
                    "price": "250",
                    "status": "0",
                }
            },
        }
        self.new = {
            "name": "Example Movie",
            "url": "https://example.test/movie",
            "dates": {
                "20261201": "AVAILABLE",
                "20261202": "BOOKABLE",
            },
            "shows": {
                "venue1|session1|20261201|Standard": {
                    "venue_code": "V1",
                    "venue": "Example Theatre",
                    "venue_location": "Central",
                    "date": "20261201",
                    "time": "7:00 PM",
                    "category": "Standard",
                    "price": "275",
                    "status": "3",
                },
                "venue1|session2|20261202|Recliner": {
                    "venue_code": "V1",
                    "venue": "Example Theatre",
                    "venue_location": "Central",
                    "date": "20261202",
                    "time": "8:00 PM",
                    "category": "Recliner",
                    "price": "400",
                    "status": "3",
                },
            },
        }

    def test_detects_category_date_price_and_availability_changes(self):
        events = compare_snapshots("ET00123456", self.old, self.new)
        event_types = [event["event_type"] for event in events]

        self.assertIn("NEW_DATE", event_types)
        self.assertIn("TICKETS_OPENED", event_types)
        self.assertIn("PRICE_CHANGED", event_types)
        self.assertIn("NEW_SHOW", event_types)

    def test_monitoring_event_dates_use_day_month_year(self):
        events = compare_snapshots("ET00123456", self.old, self.new)
        new_show = next(
            event for event in events if event["event_type"] == "NEW_SHOW"
        )

        notification = MonitoringService.format_event(new_show)

        self.assertIn("02-12-2026", notification)
        self.assertNotIn("20261202", notification)
        self.assertIn("02-12-2026", new_show["summary"])

    def test_unchanged_snapshot_emits_no_duplicate_events(self):
        self.assertEqual(
            compare_snapshots("ET00123456", self.old, self.old),
            [],
        )

    def test_initial_snapshot_is_baseline_only(self):
        self.assertEqual(
            compare_snapshots("ET00123456", None, self.new),
            [],
        )

    def test_comparison_can_be_restricted_to_exact_favourite_venue(self):
        old = {
            **self.old,
            "shows": {
                "V1|S1|20261201|Standard": {
                    **self.old["shows"]["venue1|session1|20261201|Standard"],
                    "venue_code": "V1",
                    "venue": "Cinema",
                    "venue_location": "Central",
                },
                "V2|S1|20261201|Standard": {
                    **self.old["shows"]["venue1|session1|20261201|Standard"],
                    "venue_code": "V2",
                    "venue": "Cinema",
                    "venue_location": "West",
                },
            },
        }
        new = {
            **self.new,
            "shows": {
                key: {
                    **show,
                    "venue_code": code,
                    "venue": "Cinema",
                    "venue_location": location,
                }
                for code, key, show, location in (
                    (
                        "V1",
                        "V1|S1|20261201|Standard",
                        list(self.new["shows"].values())[0],
                        "Central",
                    ),
                    (
                        "V2",
                        "V2|S1|20261201|Standard",
                        list(self.new["shows"].values())[0],
                        "West",
                    ),
                )
            },
        }
        favourite_id = theatre_identifier("V1", "Cinema")

        events = compare_snapshots(
            "ET00123456",
            old,
            new,
            theatre_ids={favourite_id},
        )

        self.assertTrue(events)
        self.assertTrue(
            all(event["theatre_id"] == favourite_id for event in events)
        )
        self.assertTrue(
            all(event["theatre_name"] == "Cinema" for event in events)
        )

    def test_new_date_is_detected_for_each_favourite_theatre(self):
        old = {
            **self.old,
            "shows": {
                "V1|S1|20261201|Standard": {
                    **self.old["shows"]["venue1|session1|20261201|Standard"],
                    "venue_code": "V1",
                    "venue": "Cinema",
                    "date": "20261201",
                }
            },
        }
        new = {
            **old,
            "shows": {
                **old["shows"],
                "V2|S1|20261201|Standard": {
                    **old["shows"]["V1|S1|20261201|Standard"],
                    "venue_code": "V2",
                    "date": "20261201",
                },
            },
        }

        events = compare_snapshots("ET00123456", old, new)
        new_date = next(
            event for event in events if event["event_type"] == "NEW_DATE"
        )

        self.assertEqual(
            new_date["theatre_id"],
            theatre_identifier("V2", "Cinema"),
        )

    def test_failed_scan_does_not_replace_last_successful_snapshot(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        store = PersistentStore(
            Path(temp_dir.name) / "bot-state.json",
            Path(temp_dir.name) / "missing.json",
        )
        store.record_scan("ET00123456", self.old, [])
        service = MonitoringService(store)
        service._scan = Mock(side_effect=RuntimeError("source unavailable"))

        with self.assertRaisesRegex(RuntimeError, "source unavailable"):
            service.refresh_movie(
                "ET00123456",
                {
                    "event_code": "ET00123456",
                    "name": "Example Movie",
                    "url": "https://example.test/movie",
                },
            )

        self.assertEqual(store.read()["snapshots"]["ET00123456"], self.old)

    def test_events_are_persisted_before_delivery_and_not_sent_twice(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        store = PersistentStore(
            Path(temp_dir.name) / "bot-state.json",
            Path(temp_dir.name) / "missing.json",
        )
        store.ensure_user(123, 456)
        store.set_priority(
            123,
            {
                "event_code": "ET00123456",
                "name": "Example Movie",
                "url": "https://example.test/movie",
            },
        )
        store.record_scan("ET00123456", self.old, [])
        store.save_favourite_theatres(
            123, [theatre_identifier("V1", "Example Theatre")]
        )
        delivered = []
        service = MonitoringService(
            store,
            notify=lambda chat_id, text: delivered.append((chat_id, text)),
        )
        service._scan = Mock(return_value=self.new)

        service.refresh_movie(
            "ET00123456",
            {
                "event_code": "ET00123456",
                "name": "Example Movie",
                "url": "https://example.test/movie",
            },
        )
        first_delivery_count = len(delivered)
        persisted_events = store.read()["events"]
        self.assertGreater(first_delivery_count, 0)
        self.assertTrue(persisted_events)
        self.assertTrue(
            all(
                event["deliveries"]["123"]["status"] == "sent"
                for event in persisted_events
            )
        )

        service.refresh_movie(
            "ET00123456",
            {
                "event_code": "ET00123456",
                "name": "Example Movie",
                "url": "https://example.test/movie",
            },
        )

        self.assertEqual(len(delivered), first_delivery_count)

    def test_failed_notification_is_backed_off_then_retried(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        store = PersistentStore(
            Path(temp_dir.name) / "bot-state.json",
            Path(temp_dir.name) / "missing.json",
        )
        store.ensure_user(123, 456)
        store.set_priority(
            123,
            {
                "event_code": "ET00123456",
                "name": "Example Movie",
                "url": "https://example.test/movie",
            },
        )
        store.record_scan("ET00123456", self.old, [])
        store.save_favourite_theatres(
            123, [theatre_identifier("V1", "Example Theatre")]
        )
        notify = Mock(side_effect=RuntimeError("Telegram unavailable"))
        service = MonitoringService(store, notify=notify)
        service._scan = Mock(return_value=self.new)

        service.refresh_movie(
            "ET00123456",
            {
                "event_code": "ET00123456",
                "name": "Example Movie",
                "url": "https://example.test/movie",
            },
        )
        failed_events = store.read()["events"]
        self.assertTrue(failed_events)
        self.assertTrue(
            all(
                event["deliveries"]["123"]["status"] == "failed"
                for event in failed_events
            )
        )
        failed_call_count = notify.call_count

        service.deliver_pending("ET00123456")
        self.assertEqual(notify.call_count, failed_call_count)

        def make_retry_due(state):
            for event in state["events"]:
                event["deliveries"]["123"]["next_retry_at"] = (
                    "2000-01-01T00:00:00+00:00"
                )

        store.update(make_retry_due)
        notify.side_effect = None
        service.deliver_pending("ET00123456")

        self.assertGreater(notify.call_count, failed_call_count)
        self.assertTrue(
            all(
                event["deliveries"]["123"]["status"] == "sent"
                for event in store.read()["events"]
            )
        )

    def test_monitoring_and_alerts_are_limited_to_current_favourites(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        store = PersistentStore(
            Path(temp_dir.name) / "bot-state.json",
            Path(temp_dir.name) / "missing.json",
        )
        old_snapshot = {
            "name": "Example Movie",
            "url": "https://example.test/movie",
            "dates": {"20261201": "AVAILABLE"},
            "shows": {},
        }
        new_snapshot = {
            **old_snapshot,
            "shows": {},
        }
        for venue_code, location in (("V1", "Central"), ("V2", "West")):
            key = f"{venue_code}|S1|20261201|Standard"
            old_snapshot["shows"][key] = {
                "venue_code": venue_code,
                "venue": "Cinema",
                "venue_location": location,
                "date": "20261201",
                "time": "7:30 PM",
                "screen": "Screen 1",
                "category": "Standard",
                "price": "250",
                "status": "0",
            }
            new_snapshot["shows"][key] = {
                **old_snapshot["shows"][key],
                "status": "3",
            }
        store.ensure_user(123, 456)
        store.record_scan("ET00123456", old_snapshot, [])
        store.set_priority(
            123,
            {
                "event_code": "ET00123456",
                "name": "Example Movie",
                "url": "https://example.test/movie",
            },
        )
        favourite_id = theatre_identifier("V1", "Cinema")
        store.save_favourite_theatres(123, [favourite_id])
        messages = []
        service = MonitoringService(
            store,
            notify=lambda chat_id, text: messages.append(text),
        )
        service._scan = Mock(return_value=new_snapshot)

        events = service.refresh_movie(
            "ET00123456",
            {
                "event_code": "ET00123456",
                "name": "Example Movie",
                "url": "https://example.test/movie",
            },
        )

        self.assertTrue(events)
        self.assertTrue(
            all(event["theatre_id"] == favourite_id for event in events)
        )
        self.assertTrue(messages)
        self.assertTrue(all("Central" in message for message in messages))
        self.assertTrue(all("West" not in message for message in messages))
        self.assertTrue(all("Screen 1" in message for message in messages))

        store.save_favourite_theatres(
            123, [theatre_identifier("V2", "Cinema")]
        )
        service.notify = Mock()
        service.deliver_pending("ET00123456")
        service.notify.assert_not_called()


class BotInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.store = PersistentStore(
            Path(self.temp_dir.name) / "bot-state.json",
            Path(self.temp_dir.name) / "missing.json",
        )
        self.client = Mock()
        self.monitor = Mock()
        self.bot = TelegramBot(self.store, self.monitor, self.client)
        self.movie = {
            "event_code": "ET00123456",
            "name": "Example Movie",
            "url": "https://example.test/movie",
            "dates": {},
            "shows": {
                "venue|session1|20261201|Standard": {
                    "movie": "Example Movie",
                    "url": "https://example.test/movie",
                    "venue": "Example Theatre",
                    "venue_code": "V1",
                    "venue_location": "Central",
                    "date": "20261201",
                    "time": "7:00 PM",
                    "time_code": "1900",
                    "category": "Standard",
                    "price": "250",
                    "status": "3",
                },
                "venue|session2|20261202|Standard": {
                    "movie": "Example Movie",
                    "url": "https://example.test/movie",
                    "venue": "Example Theatre",
                    "venue_code": "V1",
                    "venue_location": "Central",
                    "date": "20261202",
                    "time": "8:00 PM",
                    "time_code": "2000",
                    "category": "Standard",
                    "price": "300",
                    "status": "0",
                },
                "other|session3|20261203|Recliner": {
                    "movie": "Example Movie",
                    "url": "https://example.test/movie",
                    "venue": "Example Theatre",
                    "venue_code": "V2",
                    "venue_location": "West",
                    "date": "20261203",
                    "time": "9:00 PM",
                    "time_code": "2100",
                    "category": "Recliner",
                    "price": "450",
                    "status": "3",
                },
            },
        }
        self.store.ensure_user(100, 200)
        self.store.record_scan("ET00123456", self.movie, [])
        self.favorite_id = theatre_identifier("V1", "Example Theatre")
        self.other_id = theatre_identifier("V2", "Example Theatre")
        self.store.save_favourite_theatres(100, [self.favorite_id])

    def test_show_movies_command_starts_a_fresh_catalogue_scan(self):
        self.store.update(
            lambda state: state.update(
                {
                    "catalog": [
                        {
                            "event_code": "ET00123456",
                            "name": "Example Movie",
                            "url": "https://example.test/movie",
                            "bookable_count": 1,
                            "theatre_count": 1,
                        }
                    ],
                    "catalog_updated_at": "2026-01-01T00:00:00+00:00",
                }
            )
        )
        self.bot._start_refresh = Mock()

        self.bot.handle_update(
            {
                "message": {
                    "chat": {"id": 200},
                    "from": {"id": 100},
                    "text": "SHOW MOVIES",
                }
            }
        )

        self.bot._start_refresh.assert_called_once_with(200)

    def test_start_requires_favourite_theatre_selection_before_menu(self):
        self.store.update(
            lambda state: state["users"]["100"].pop(
                "theatre_preferences_saved"
            )
        )
        self.store.update(
            lambda state: state["users"]["100"].pop("favourite_theatres")
        )

        self.bot.handle_update(
            {
                "message": {
                    "chat": {"id": 200},
                    "from": {"id": 100},
                    "text": "/start",
                }
            }
        )

        text = self.client.send_message.call_args.args[1]
        self.assertIn("SELECT FAVOURITE THEATRES", text)
        self.assertNotIn("BMS_AGENT MENU", text)

    def test_theatre_search_toggle_and_save_persist_selection(self):
        self.bot._search_favourites(200, 100, "West")
        text = self.client.send_message.call_args.args[1]
        self.assertIn("West", text)
        self.assertNotIn("Central", text)

        self.bot._handle_callback(
            {
                "id": "callback-venue",
                "from": {"id": 100},
                "message": {"chat": {"id": 200}},
                "data": "favtoggle:0:0",
            }
        )
        draft = set(
            self.store.user(100)["ui"]["theatre_selection_draft"]
        )
        self.assertEqual(draft, {self.favorite_id, self.other_id})
        self.store.update(lambda state: state.update({"theatres": []}))

        self.bot._save_favourites(200, 100)

        self.assertEqual(
            set(self.store.user(100)["favourite_theatres"]),
            {self.favorite_id, self.other_id},
        )
        restarted = PersistentStore(
            self.store.path,
            Path(self.temp_dir.name) / "missing.json",
        )
        self.assertEqual(
            set(restarted.user(100)["favourite_theatres"]),
            {self.favorite_id, self.other_id},
        )
        self.monitor.trigger.assert_called_once_with()

    def test_unknown_button_action_returns_clear_message(self):
        self.bot.handle_update(
            {
                "callback_query": {
                    "id": "callback-unknown",
                    "from": {"id": 100},
                    "message": {"chat": {"id": 200}},
                    "data": "unknown-action",
                }
            }
        )

        self.client.answer_callback.assert_called_once_with("callback-unknown")
        response = self.client.send_message.call_args.args[1]
        self.assertIn("button is no longer available", response)

    def test_interaction_logging_captures_action_and_response_text(self):
        log_path = Path(self.temp_dir.name) / "logs" / "interaction.log"
        with patch.dict("os.environ", {"BMS_LOG_FILE": str(log_path)}):
            configure_interaction_logging(self.store.path)
        try:
            self.bot.handle_update(
                {
                    "callback_query": {
                        "id": "callback-log",
                        "from": {"id": 100},
                        "message": {"chat": {"id": 200}},
                        "data": "favsave",
                    }
                }
            )
            telegram = TelegramClient(token="123:token")
            telegram._request = Mock()
            telegram.send_message(200, "Captured response text.")
            for handler in INTERACTION_LOG.handlers:
                handler.flush()

            contents = log_path.read_text(encoding="utf-8")
            self.assertIn('"event": "button_click"', contents)
            self.assertIn('"action": "favsave"', contents)
            self.assertIn('"event": "response_sent"', contents)
            self.assertIn("Captured response text.", contents)
        finally:
            for handler in list(INTERACTION_LOG.handlers):
                INTERACTION_LOG.removeHandler(handler)
                handler.close()

    def test_default_interaction_log_is_created_in_logs_folder(self):
        with patch.dict("os.environ", {"BMS_LOG_FILE": ""}):
            log_path = configure_interaction_logging(self.store.path)
        try:
            self.assertEqual(
                log_path,
                self.store.path.parent / "logs" / "bms_bot.log",
            )
            self.assertTrue(log_path.parent.is_dir())
            self.assertTrue(log_path.exists())
        finally:
            for handler in list(INTERACTION_LOG.handlers):
                INTERACTION_LOG.removeHandler(handler)
                handler.close()

    def test_priority_callback_persists_movie_selection(self):
        self.store.update(
            lambda state: state.update(
                {
                    "catalog": [
                        {
                            "event_code": "ET00123456",
                            "name": "Example Movie",
                            "url": "https://example.test/movie",
                            "bookable_count": 1,
                            "theatre_count": 1,
                        }
                    ]
                }
            )
        )

        self.bot.handle_update(
            {
                "callback_query": {
                    "id": "callback-1",
                    "from": {"id": 100},
                    "message": {"chat": {"id": 200}},
                    "data": "priority:add:ET00123456",
                }
            }
        )

        self.assertIn(
            "ET00123456",
            self.store.user(100)["priorities"],
        )

    def test_movie_filter_is_applied_and_details_use_same_filtered_list(self):
        self.store.set_ui(
            100,
            "filters",
            {"ET00123456": {"date": "20261202"}},
        )

        self.bot._movie_page(200, 100, "ET00123456")

        text = self.client.send_message.call_args.args[1]
        self.assertIn("02-12-2026", text)
        self.assertNotIn("20261202", text)
        self.assertNotIn("20261201", text)
        detail_button = self.client.send_message.call_args.args[2][
            "inline_keyboard"
        ][0][0]["text"]
        self.assertIn("8:00 PM", detail_button)
        self.assertIn("Standard", detail_button)
        self.client.reset_mock()

        self.bot._show_detail(200, 100, "ET00123456", 0)

        detail = self.client.send_message.call_args.args[1]
        self.assertIn("02-12-2026", detail)
        self.assertIn("₹300", detail)

    def test_date_formatter_displays_yyyymmdd_as_ddmmyyyy(self):
        self.assertEqual(format_date("20261220"), "20-12-2026")
        self.assertEqual(self.bot._format_date("20261220"), "20-12-2026")

    def test_priority_navigation_excludes_same_named_non_favourite_venue(self):
        self.store.set_priority(
            100,
            {
                "event_code": "ET00123456",
                "name": "Example Movie",
                "url": "https://example.test/movie",
            },
        )
        self.client.reset_mock()

        self.bot._priorities(200, 100)
        dashboard = self.client.send_message.call_args.args[1]
        self.assertIn("1 upcoming shows", dashboard)
        self.assertIn("01-12-2026 · 7:00 PM", dashboard)
        self.assertNotIn("West", dashboard)
        self.assertNotIn("03-12-2026", dashboard)
        markup = self.client.send_message.call_args.args[2]
        self.assertEqual(markup["inline_keyboard"][0][0]["text"], "📅 Show dates")
        self.client.reset_mock()

        self.bot._priority_movie(200, 100, "ET00123456")
        dates = self.client.send_message.call_args.args[1]
        self.assertIn("01-12-2026", dates)
        self.assertNotIn("03-12-2026", dates)
        self.assertNotIn("02-12-2026", dates)
        self.client.reset_mock()

        self.bot._priority_date(200, 100, "ET00123456", "20261201")
        theatres = self.client.send_message.call_args.args[1]
        self.assertIn("Central", theatres)
        self.assertNotIn("West", theatres)
        self.client.reset_mock()

        self.bot._priority_theatre(
            200, 100, "ET00123456", "20261201", self.favorite_id
        )
        detail_button = self.client.send_message.call_args.args[2][
            "inline_keyboard"
        ][0][0]["text"]
        self.assertIn("7:00 PM", detail_button)
        self.assertIn("Standard", detail_button)

    def test_movie_catalogue_button_uses_movie_name_not_generic_view_number(self):
        self.store.update(
            lambda state: state.update(
                {
                    "catalog": [
                        {
                            "event_code": "ET00123456",
                            "name": "Example Movie",
                            "url": "https://example.test/movie",
                        }
                    ]
                }
            )
        )

        self.bot._send_catalog(200, 0)

        button_text = self.client.send_message.call_args.args[2][
            "inline_keyboard"
        ][0][0]["text"]
        self.assertEqual(button_text, "🎟 Example Movie")

    def test_priority_show_detail_never_falls_back_after_theatre_removed(self):
        self.store.set_priority(
            100,
            {
                "event_code": "ET00123456",
                "name": "Example Movie",
                "url": "https://example.test/movie",
            },
        )
        self.store.save_favourite_theatres(100, [self.other_id])
        self.client.reset_mock()

        self.bot._priority_theatre(
            200, 100, "ET00123456", "20261201", self.favorite_id
        )

        text = self.client.send_message.call_args.args[1]
        self.assertIn("no longer in your favourite selection", text)
        self.assertNotIn("Central", text)

    def test_history_includes_only_events_for_current_favourite_theatres(self):
        self.store.set_priority(
            100,
            {
                "event_code": "ET00123456",
                "name": "Example Movie",
                "url": "https://example.test/movie",
            },
        )
        for theatre_id, name, location in (
            (self.favorite_id, "Example Theatre", "Central"),
            (self.other_id, "Example Theatre", "West"),
        ):
            self.store.update(
                lambda state, theatre_id=theatre_id, name=name, location=location:
                state["events"].append(
                    {
                        "event_id": theatre_id,
                        "movie_id": "ET00123456",
                        "movie_name": "Example Movie",
                        "theatre_id": theatre_id,
                        "theatre_name": name,
                        "location": location,
                        "event_type": "TICKETS_OPENED",
                        "summary": f"Tickets opened at {location}",
                        "detected_at": "2026-11-24T00:00:00+00:00",
                    }
                )
            )
        self.client.reset_mock()

        self.bot._history(200, 100)

        text = self.client.send_message.call_args.args[1]
        self.assertIn("Central", text)
        self.assertNotIn("West", text)


if __name__ == "__main__":
    unittest.main()
