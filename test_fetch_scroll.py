import json
import os
import unittest
from unittest import mock

import fetch_smart


class ScrollScreensTest(unittest.TestCase):
    def test_default_is_zero(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(fetch_smart._scroll_screens(), 0)

    def test_env_value(self):
        with mock.patch.dict(os.environ, {"FETCH_SCROLL": "3"}, clear=True):
            self.assertEqual(fetch_smart._scroll_screens(), 3)

    def test_explicit_wins_over_env(self):
        with mock.patch.dict(os.environ, {"FETCH_SCROLL": "3"}, clear=True):
            self.assertEqual(fetch_smart._scroll_screens(7), 7)

    def test_clamped_and_invalid(self):
        self.assertEqual(fetch_smart._scroll_screens(99), 10)
        self.assertEqual(fetch_smart._scroll_screens(-4), 0)
        with mock.patch.dict(os.environ, {"FETCH_SCROLL": "abc"}, clear=True):
            self.assertEqual(fetch_smart._scroll_screens(), 0)


class ScrolledExtractTest(unittest.TestCase):
    def _runner(self, states, final_text="第一段\n第二段"):
        calls = []

        def run(args, timeout, stage):
            calls.append(list(args))
            if args[2] == "open":
                return ""
            if args[2] == "eval":
                script = args[3]
                if script == fetch_smart.OPENCLI_MORE_JS:
                    return "1"
                if script == fetch_smart.OPENCLI_SCROLL_JS:
                    state = states[min(len([c for c in calls if c[3:4] == [fetch_smart.OPENCLI_SCROLL_JS]]), len(states)) - 1]
                    return json.dumps(state)
                return final_text
            raise AssertionError(args)

        return run, calls

    def test_scrolls_requested_screens(self):
        states = [
            {"before": 0, "top": 600, "chars": 1000, "height": 2000},
            {"before": 600, "top": 1200, "chars": 2000, "height": 3000},
        ]
        run, calls = self._runner(states)
        with mock.patch.object(fetch_smart, "_run_opencli", side_effect=run), \
                mock.patch.object(fetch_smart.time, "sleep", return_value=None):
            title, text, meta = fetch_smart._opencli_extract_scrolled("https://example.com", 60, 2)
        self.assertEqual(text, "第一段\n第二段")
        self.assertEqual(meta["screens"], 2)
        self.assertEqual(meta["stopped"], "limit")
        self.assertEqual(meta["chars_before"], 1000)
        self.assertEqual(meta["chars_after"], len("第一段\n第二段"))
        self.assertEqual(sum(1 for c in calls if c[3:4] == [fetch_smart.OPENCLI_SCROLL_JS]), 2)

    def test_stops_when_nothing_moves(self):
        states = [
            {"before": 0, "top": 600, "chars": 1000, "height": 2000},
            {"before": 600, "top": 600, "chars": 1000, "height": 2000},
            {"before": 600, "top": 600, "chars": 1000, "height": 2000},
            {"before": 600, "top": 600, "chars": 1000, "height": 2000},
        ]
        run, calls = self._runner(states)
        with mock.patch.object(fetch_smart, "_run_opencli", side_effect=run), \
                mock.patch.object(fetch_smart.time, "sleep", return_value=None):
            _, _, meta = fetch_smart._opencli_extract_scrolled("https://example.com", 60, 4)
        self.assertEqual(meta["stopped"], "no_more_content")
        self.assertLess(meta["screens"], 4)

    def test_zero_screens_uses_plain_extract(self):
        with mock.patch.object(fetch_smart, "_opencli_extract", return_value=("t", "body")) as plain, \
                mock.patch.object(fetch_smart, "_opencli_extract_scrolled") as scrolled:
            title, text, meta = fetch_smart._logged_in_extract("https://example.com", 10, 0)
        self.assertEqual((title, text, meta), ("t", "body", {}))
        plain.assert_called_once()
        scrolled.assert_not_called()


if __name__ == "__main__":
    unittest.main()
