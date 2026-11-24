import os
import unittest
from unittest.mock import patch

import main


class MovieDiscoveryTests(unittest.TestCase):
    def test_extracts_and_deduplicates_movie_links(self):
        parser = main.MovieLinkParser()
        parser.feed(
            '<a href="/hyderabad/movies/example/ET00123456">Example Movie</a>'
            '<a href="https://in.bookmyshow.com/movies/example/ET00123456">'
            "Example Movie</a>"
            '<a href="/hyderabad/events/music/EV00123456">Concert</a>'
        )

        self.assertEqual(len(parser.movies), 1)
        movie = parser.movies["ET00123456"]
        self.assertEqual(movie["name"], "Example Movie")
        self.assertEqual(
            movie["url"],
            "https://in.bookmyshow.com/hyderabad/movies/example/ET00123456",
        )


class ChangeDetectionTests(unittest.TestCase):
    def setUp(self):
        self.new_state = {
            "movies": {
                "ET00123456": {
                    "name": "Example Movie",
                    "url": "https://in.bookmyshow.com/movies/example/ET00123456",
                    "dates": {"20261201": "AVAILABLE"},
                    "shows": {
                        "venue|session|20261201|Standard": {
                            "movie": "Example Movie",
                            "url": "https://in.bookmyshow.com/movies/example/ET00123456",
                            "venue": "Example Theatre",
                            "time": "7:00 PM",
                            "date": "20261201",
                            "category": "Standard",
                            "price": "250",
                            "status": "3",
                        }
                    },
                }
            }
        }

    def test_initial_scan_reports_available_shows(self):
        changes = main.detect_changes({}, self.new_state)

        self.assertEqual(len(changes), 1)
        self.assertIn("Currently bookable", changes[0])
        self.assertIn("Example Theatre", changes[0])
        self.assertIn("ET00123456", changes[0])

    def test_sold_out_show_and_closed_date_opening_are_reported(self):
        old_state = {
            "movies": {
                "ET00123456": {
                    "dates": {"20261201": "NOT_OPEN"},
                    "shows": {
                        "venue|session|20261201|Standard": {
                            "status": "0"
                        }
                    },
                }
            }
        }

        changes = main.detect_changes(old_state, self.new_state)

        self.assertEqual(len(changes), 1)
        self.assertIn("Newly open dates", changes[0])
        self.assertIn("Tickets now available", changes[0])

    def test_available_show_does_not_repeat_without_a_change(self):
        changes = main.detect_changes(self.new_state, self.new_state)

        self.assertEqual(changes, [])

    def test_new_available_date_is_reported_after_baseline(self):
        old_state = {"movies": {"ET00123456": {"dates": {}, "shows": {}}}}
        state_without_shows = {
            "movies": {
                "ET00123456": {
                    **self.new_state["movies"]["ET00123456"],
                    "shows": {},
                }
            }
        }

        changes = main.detect_changes(old_state, state_without_shows)

        self.assertEqual(len(changes), 1)
        self.assertIn("booking opened", changes[0])

    def test_initial_scan_summarizes_many_bookable_categories_once(self):
        movie = self.new_state["movies"]["ET00123456"]
        for index in range(2, 20):
            movie["shows"][f"venue|session{index}|20261201|Standard"] = {
                **movie["shows"]["venue|session|20261201|Standard"],
                "venue": f"Theatre {index}",
            }

        changes = main.detect_changes({}, self.new_state)

        self.assertEqual(len(changes), 1)
        self.assertIn("19 bookable ticket categories", changes[0])
        self.assertIn("16 additional showtimes", changes[0])


class TelegramTests(unittest.TestCase):
    @patch.dict(os.environ, {
        "TELEGRAM_BOT_TOKEN": "123456:example_token",
        "TELEGRAM_CHAT_ID": "987654321",
    })
    @patch("main.requests.post")
    def test_sends_message_to_telegram_chat(self, post):
        post.return_value.status_code = 200
        post.return_value.json.return_value = {"ok": True}

        main.send_telegram(["Tickets opened"])

        post.assert_called_once()
        args, kwargs = post.call_args
        self.assertEqual(
            args[0],
            "https://api.telegram.org/bot123456:example_token/sendMessage",
        )
        self.assertEqual(kwargs["json"]["chat_id"], "987654321")
        self.assertEqual(kwargs["json"]["text"], "Tickets opened")
        self.assertTrue(kwargs["json"]["disable_web_page_preview"])

    @patch.dict(os.environ, {}, clear=True)
    def test_requires_telegram_credentials_to_send(self):
        with self.assertRaisesRegex(RuntimeError, "TELEGRAM_BOT_TOKEN"):
            main.send_telegram(["Tickets opened"])

    @patch.dict(os.environ, {
        "TELEGRAM_BOT_TOKEN": "123456:example_token",
        "TELEGRAM_CHAT_ID": "987654321",
    })
    @patch("main.requests.post")
    def test_reports_telegram_api_rejection(self, post):
        post.return_value.status_code = 200
        post.return_value.json.return_value = {
            "ok": False,
            "description": "Chat not found",
        }

        with self.assertRaisesRegex(RuntimeError, "Chat not found"):
            main.send_telegram(["Tickets opened"])


if __name__ == "__main__":
    unittest.main()
