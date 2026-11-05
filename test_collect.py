import datetime as dt
import json
import os
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import collect
import collect_browser
import rss_reader


RSS_XML = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>Test RSS</title>
  <item><title>RSS title</title><link>https://example.test/a</link>
    <pubDate>Mon, 02 Nov 2026 10:00:00 GMT</pubDate>
    <description>&lt;p&gt;RSS summary&lt;/p&gt;</description></item>
  <item><title>No date</title><link>https://example.test/no-date</link></item>
</channel></rss>"""

ATOM_XML = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry><title>Atom title</title><link rel="alternate" href="https://example.test/b"/>
    <updated>2026-11-01T08:30:00Z</updated><summary>Atom summary</summary></entry>
</feed>"""

STATS_TABLES_HTML = """<!doctype html>
<html><head><title>Rankings</title></head><body>
  <table><thead><tr><th>Player</th><th>Score</th></tr></thead>
    <tbody><tr><td>Alpha</td><td>10</td></tr><tr><td>Beta</td><td>20</td></tr></tbody>
  </table>
  <table><tr><th>Team</th><th>Rate</th></tr><tr><td>Gamma</td><td>30%</td></tr></table>
</body></html>"""


class RSSReaderTests(unittest.TestCase):
    def test_rss_and_atom_parsing(self):
        rss_items = rss_reader.parse_feed_xml(RSS_XML, "RSS source")
        atom_items = rss_reader.parse_feed_xml(ATOM_XML, "Atom source")
        self.assertEqual(len(rss_items), 2)
        self.assertEqual(len(atom_items), 1)
        self.assertEqual(rss_items[0]["title"], "RSS title")
        self.assertEqual(rss_items[0]["link"], "https://example.test/a")
        self.assertEqual(rss_items[0]["summary"], "RSS summary")
        self.assertEqual(rss_items[1]["date"], None)
        self.assertEqual(atom_items[0]["title"], "Atom title")
        self.assertEqual(atom_items[0]["link"], "https://example.test/b")
        self.assertEqual(atom_items[0]["published"], "2026-11-01T08:30:00Z")

    def test_source_stats_distinguish_fetched_and_kept_after_global_limit(self):
        now = dt.datetime.now(dt.timezone.utc)
        feeds = [
            {"name": "Alpha", "url": "https://alpha.test/feed", "proxy": False},
            {"name": "Beta", "url": "https://beta.test/feed", "proxy": False},
        ]

        def row(source, suffix, age_hours):
            published = now + dt.timedelta(hours=age_hours)
            return {
                "source": source,
                "title": suffix,
                "link": f"https://{source.lower()}.test/{suffix}",
                "url": f"https://{source.lower()}.test/{suffix}",
                "published": published.isoformat(),
                "date": published.isoformat(),
            }

        rows = {
            "Alpha": [row("Alpha", "older", 24), row("Alpha", "newer", 12)],
            "Beta": [row("Beta", "newest", 48), row("Beta", "old", 6)],
        }

        def fetch(feed, timeout):
            return rows[feed["name"]], 0.01, None

        with (
            patch.object(rss_reader, "_load_feeds", return_value=feeds),
            patch.object(rss_reader, "_fetch_timed_feed", side_effect=fetch),
        ):
            payload = rss_reader.read_feeds(days=3, limit=2, overall_timeout=1)

        self.assertEqual(payload["stats"]["sources"]["Alpha"]["fetched"], 2)
        self.assertEqual(payload["stats"]["sources"]["Alpha"]["kept"], 1)
        self.assertEqual(payload["stats"]["sources"]["Beta"]["fetched"], 2)
        self.assertEqual(payload["stats"]["sources"]["Beta"]["kept"], 1)
        output = rss_reader._human_output(payload)
        self.assertIn("Alpha=2/1条", output)
        self.assertIn("Beta=2/1条", output)

    def test_per_source_limit_caps_kept_items(self):
        now = dt.datetime.now(dt.timezone.utc)
        feeds = [
            {"name": "Alpha", "url": "https://alpha.test/feed", "proxy": False},
            {"name": "Beta", "url": "https://beta.test/feed", "proxy": False},
        ]

        def row(source, index):
            published = now - dt.timedelta(minutes=index)
            return {
                "source": source,
                "title": f"{source}-{index}",
                "link": f"https://{source.lower()}.test/{index}",
                "published": published.isoformat(),
            }

        rows = {
            "Alpha": [row("Alpha", index) for index in range(4)],
            "Beta": [row("Beta", index) for index in range(4)],
        }

        def fetch(feed, timeout):
            return rows[feed["name"]], 0.01, None

        with (
            patch.object(rss_reader, "_load_feeds", return_value=feeds),
            patch.object(rss_reader, "_fetch_timed_feed", side_effect=fetch),
        ):
            payload = rss_reader.read_feeds(limit=8, per_source=2, overall_timeout=1)

        self.assertEqual(
            {source: stat["fetched"] for source, stat in payload["stats"]["sources"].items()},
            {"Alpha": 4, "Beta": 4},
        )
        self.assertEqual(
            {source: stat["kept"] for source, stat in payload["stats"]["sources"].items()},
            {"Alpha": 2, "Beta": 2},
        )

    def test_results_are_interleaved_round_robin_and_sorted_within_source(self):
        now = dt.datetime.now(dt.timezone.utc)
        feeds = [
            {"name": name, "url": f"https://{name.lower()}.test/feed", "proxy": False}
            for name in ("Alpha", "Beta", "Gamma")
        ]

        def row(source, index, minutes_ago):
            published = now - dt.timedelta(minutes=minutes_ago)
            return {
                "source": source,
                "title": f"{source}-{index}",
                "link": f"https://{source.lower()}.test/{index}",
                "published": published.isoformat(),
            }

        rows = {
            "Alpha": [row("Alpha", 1, 1), row("Alpha", 0, 0)],
            "Beta": [row("Beta", 1, 3), row("Beta", 0, 2)],
            "Gamma": [row("Gamma", 1, 5), row("Gamma", 0, 4)],
        }

        def fetch(feed, timeout):
            return rows[feed["name"]], 0.01, None

        with (
            patch.object(rss_reader, "_load_feeds", return_value=feeds),
            patch.object(rss_reader, "_fetch_timed_feed", side_effect=fetch),
        ):
            payload = rss_reader.read_feeds(limit=5, per_source=2, overall_timeout=1)

        self.assertEqual(
            [item["title"] for item in payload["items"]],
            ["Alpha-0", "Beta-0", "Gamma-0", "Alpha-1", "Beta-1"],
        )

    def test_deduplicate_by_link(self):
        items = rss_reader.filter_sort_dedupe(
            [
                {"title": "first", "link": "https://example.test/a", "published": None},
                {"title": "duplicate", "link": "https://example.test/a", "published": None},
            ],
            days=7,
            now=dt.datetime(2026, 11, 2, tzinfo=dt.timezone.utc),
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "first")


class CollectTests(unittest.TestCase):
    def test_stats_missing_config_returns_a_diagnostic(self):
        missing = Path(__file__).resolve().parent / f"missing_stats_sources_{os.getpid()}_{id(self)}.json"
        with patch.object(collect, "STATS_SOURCES_PATH", missing):
            payload = collect.run_collection("ignored", platforms=("stats",))

        self.assertEqual(payload["items"], [])
        self.assertIn("stats_sources_missing", payload["errors"]["stats"])

    def test_stats_extracts_tables_and_ignores_query(self):
        fetched = {
            "text": "Ranking body " * 500,
            "method": "http",
            "chars": 6500,
            "login_wall": False,
        }
        response = type("Response", (), {"content": STATS_TABLES_HTML.encode(), "raise_for_status": lambda self: None})()
        sources = [{"name": "Configured board", "url": "https://example.test/rank", "proxy": False}]
        with (
            patch.object(collect, "_load_env"),
            patch.object(collect, "STATS_SOURCES_PATH") as config_path,
            patch.object(collect.fetch_smart, "fetch_smart", return_value=fetched) as smart_fetch,
            patch.object(collect.requests, "get", return_value=response),
        ):
            config_path.read_text.return_value = json.dumps(sources)
            payload = collect.run_collection("a query that must be ignored", platforms=("stats",))

        smart_fetch.assert_called_once_with("https://example.test/rank", min_chars=200)
        item = payload["items"][0]
        self.assertEqual(item["title"], "Configured board")
        self.assertEqual(item["body"], fetched["text"][:20000])
        self.assertEqual(item["method"], "http")
        self.assertEqual(item["chars"], 6500)
        self.assertIs(item["login_wall"], False)
        self.assertEqual(item["tables"], [
            {"index": 1, "headers": ["Player", "Score"], "rows": [["Alpha", "10"], ["Beta", "20"]]},
            {"index": 2, "headers": ["Team", "Rate"], "rows": [["Gamma", "30%"]]},
        ])

    def test_stats_table_rows_are_capped_at_200(self):
        table_html = "<table><tr><th>Value</th></tr>" + "".join(
            f"<tr><td>{index}</td></tr>" for index in range(205)
        ) + "</table>"
        tables = collect._extract_stats_tables(table_html)
        self.assertEqual(len(tables[0]["rows"]), 200)
        self.assertEqual(tables[0]["rows"][0], ["0"])
        self.assertEqual(tables[0]["rows"][-1], ["199"])

    def test_stats_payload_redaction_preserves_body_limit_and_empty_query(self):
        body = "Ranking row. " * 1500
        payload = collect._redact_payload({
            "query": "",
            "items": [{"platform": "stats", "body": body}],
        })

        self.assertEqual(payload["query"], "")
        self.assertEqual(payload["items"][0]["body"], body[:20000])
        self.assertGreater(len(payload["items"][0]["body"]), 1000)

    def test_stats_source_failure_does_not_hide_other_sources(self):
        sources = [
            {"name": "Broken", "url": "https://broken.test/rank", "proxy": False},
            {"name": "Working", "url": "https://working.test/rank", "proxy": False},
        ]
        fetched = {"text": "Working board " * 200, "method": "http", "chars": 2800, "login_wall": False}
        response = type("Response", (), {"content": STATS_TABLES_HTML.encode(), "raise_for_status": lambda self: None})()

        def fetch(url, min_chars):
            if "broken.test" in url:
                raise RuntimeError("source unavailable")
            return fetched

        with (
            patch.object(collect, "_load_env"),
            patch.object(collect, "STATS_SOURCES_PATH") as config_path,
            patch.object(collect.fetch_smart, "fetch_smart", side_effect=fetch),
            patch.object(collect.requests, "get", return_value=response),
        ):
            config_path.read_text.return_value = json.dumps(sources)
            payload = collect.run_collection("", platforms=("stats",))

        self.assertEqual([item["title"] for item in payload["items"]], ["Working"])
        self.assertIn("stats_error: Broken", payload["errors"]["stats"])

    def test_web_channel_preserves_unreliable_title_marker(self):
        payload = {
            "results": [
                {
                    "title": "suspected mojibake",
                    "url": "https://example.test/result",
                    "title_encoding_unreliable": True,
                }
            ],
            "errors": {},
        }
        with patch.object(collect.search_dual, "search_dual", return_value=payload):
            items = collect._search_web("query", 1).items

        self.assertIs(items[0]["title_encoding_unreliable"], True)

    def test_ytdlp_resolution_prefers_environment_then_known_path_then_path(self):
        configured = Path("C:/custom/yt-dlp.exe")
        with (
            patch.dict(os.environ, {"YTDLP_PATH": str(configured)}),
            patch.object(
                Path,
                "is_file",
                autospec=True,
                side_effect=lambda path: path == configured,
            ),
            patch.object(collect.shutil, "which") as which,
        ):
            self.assertEqual(collect._yt_dlp_executable(), str(configured))
            which.assert_not_called()

        with (
            patch.dict(os.environ, {"YTDLP_PATH": ""}),
            patch.object(
                Path,
                "is_file",
                autospec=True,
                side_effect=lambda path: path == collect.YTDLP_EXE,
            ),
            patch.object(collect.shutil, "which") as which,
        ):
            self.assertEqual(collect._yt_dlp_executable(), str(collect.YTDLP_EXE))
            which.assert_not_called()

        with (
            patch.dict(os.environ, {"YTDLP_PATH": ""}),
            patch.object(Path, "is_file", return_value=False),
            patch.object(collect.shutil, "which", return_value="yt-dlp") as which,
        ):
            self.assertEqual(collect._yt_dlp_executable(), "yt-dlp")
            which.assert_called_once_with("yt-dlp")

    def test_bili_executable_is_absolute_or_the_missing_sentinel(self):
        self.assertTrue(collect.BILI_EXE.is_absolute())
        if collect.BILI_EXE.name.lower() not in {"bili.exe", "bili"}:
            self.assertEqual(collect.BILI_EXE.name, "not-installed")

    def test_bili_failure_does_not_hide_other_channel_results(self):
        hn_item = {
            "platform": "hn",
            "title": "Other source survived",
            "url": "https://news.ycombinator.com/item?id=1",
            "published": "2026-11-02T00:00:00Z",
            "metrics": {"points": 3},
            "summary": "",
            "source_id": "hn-1",
        }
        completed = subprocess.CompletedProcess(
            args=["bili.exe"], returncode=7, stdout="", stderr="CLI unavailable"
        )
        with (
            patch.object(collect, "_bili_available", return_value=True),
            patch.object(collect.subprocess, "run", return_value=completed),
            patch.object(collect, "_collect_hn", return_value=[hn_item]),
            patch.object(collect, "_load_env"),
        ):
            payload = collect.run_collection("query", platforms=("bili", "hn"))
        self.assertEqual([item["platform"] for item in payload["items"]], ["hn"])
        self.assertIn("bili", payload["errors"])
        self.assertIn("code 7", payload["errors"]["bili"])

    def test_sort_by_date_and_leave_unknown_dates_last(self):
        items = [
            {"published": "2026-11-01T00:00:00Z", "metrics": {"points": 20}},
            {"published": None, "metrics": {"play": 100000}},
            {"published": "2026-11-02T00:00:00Z", "metrics": {"stars": 1}},
        ]
        sorted_items = collect.sort_items(items)
        self.assertEqual(
            [item["published"] for item in sorted_items],
            ["2026-11-02T00:00:00Z", "2026-11-01T00:00:00Z", None],
        )

    def test_same_day_uses_platform_heat(self):
        items = [
            {"published": "2026-11-02T08:00:00Z", "metrics": {"play": 10}},
            {"published": "2026-11-02T12:00:00Z", "metrics": {"points": 50}},
        ]
        self.assertEqual(collect.sort_items(items), [items[1], items[0]])

    def test_transcribe_flag_controls_fields_and_missing_groq_is_item_error(self):
        video = {
            "platform": "youtube",
            "title": "Video",
            "url": "https://youtube.test/watch?v=1",
            "published": None,
            "metrics": {},
            "summary": "",
            "source_id": "youtube-1",
            "duration_seconds": 60,
        }
        with (
            patch.object(collect, "_collect_youtube", return_value=[dict(video)]),
            patch.object(collect, "_load_env"),
        ):
            without = collect.run_collection("query", platforms=("youtube",), transcribe=False)
        self.assertNotIn("transcribed", without["items"][0])

        with (
            patch.object(collect, "_load_env"),
            patch.object(collect, "_yt_dlp_executable", return_value="yt-dlp"),
            patch.object(collect, "_download_subtitle", return_value=None),
            patch.dict(os.environ, {"GROQ_API_KEY": ""}),
        ):
            transcript, source, error = collect._transcribe_item(video, time.monotonic() + 10)
        self.assertIsNone(transcript)
        self.assertIsNone(source)
        self.assertEqual(error, "Groq API key unavailable")

        with (
            patch.object(collect, "_collect_youtube", return_value=[dict(video)]),
            patch.object(collect, "_load_env"),
            patch.object(collect, "_yt_dlp_executable", return_value="yt-dlp"),
            patch.object(collect, "_download_subtitle", return_value=None),
            patch.dict(os.environ, {"GROQ_API_KEY": ""}),
        ):
            with patch.object(collect, "_collect_channels", wraps=collect._collect_channels):
                enabled = collect.run_collection("query", platforms=("youtube",), transcribe=True)
        self.assertIs(enabled["items"][0]["transcribed"], False)
        self.assertEqual(enabled["items"][0]["transcribe_error"], "Groq API key unavailable")

    def test_transcribe_item_prefers_subtitle_and_marks_its_source(self):
        video = {
            "platform": "youtube",
            "title": "Captioned video",
            "url": "https://youtube.test/watch?v=captions",
            "duration_seconds": 900,
        }
        with (
            patch.object(collect, "_yt_dlp_executable", return_value="yt-dlp"),
            patch.object(collect, "_download_subtitle", return_value="Caption text"),
            patch.object(collect, "_groq_transcribe", side_effect=AssertionError("Groq must not run")),
            patch.dict(os.environ, {"GROQ_API_KEY": ""}),
        ):
            transcript, source, error = collect._transcribe_item(video, time.monotonic() + 10)

        self.assertEqual(transcript, "Caption text")
        self.assertEqual(source, "subtitle")
        self.assertIsNone(error)

    def test_transcription_duration_boundary_is_25_minutes(self):
        videos = [
            {
                "platform": "youtube",
                "title": f"Video {duration}",
                "url": f"https://youtube.test/watch?v={duration}",
                "published": None,
                "metrics": {},
                "summary": "Description",
                "source_id": f"youtube-{duration}",
                "duration_seconds": duration,
            }
            for duration in (7200, 7201, 900)
        ]
        transcribed = []

        def fake_transcribe(item, _deadline):
            transcribed.append(item["duration_seconds"])
            return "Transcript", "subtitle", None

        with (
            patch.object(
                collect,
                "_collect_channels",
                return_value=(videos, {"youtube": {"count": 0, "elapsed_seconds": 0.0, "status": "success"}}, {}),
            ),
            patch.object(collect, "_within_days", side_effect=lambda items, _days: items),
            patch.object(collect, "_load_env"),
            patch.object(collect, "_transcribe_item", side_effect=fake_transcribe),
        ):
            payload = collect.run_collection("query", platforms=("youtube",), transcribe=True)

        items = {item["duration_seconds"]: item for item in payload["items"]}
        self.assertEqual(transcribed, [7200, 7201, 900])
        self.assertIs(items[7200]["transcribed"], True)
        self.assertEqual(items[7200]["transcript_source"], "subtitle")
        self.assertIs(items[900]["transcribed"], True)
        self.assertIs(items[7201]["transcribed"], True)
        self.assertEqual(items[7201]["truncated"], "duration_limit")

    def test_transcribe_collection_budget_is_at_least_1200_seconds(self):
        self.assertGreaterEqual(collect._collection_timeout(True), 1200.0)
        self.assertEqual(collect._collection_timeout(False), collect.TOTAL_TIMEOUT)

    def test_download_audio_uses_48k_mono_16khz(self):
        with tempfile.TemporaryDirectory() as directory:
            basename = Path(directory) / "audio"
            command_seen = []

            def fake_run(command, **_kwargs):
                command_seen.append(command)
                Path(f"{basename}.audio.mp3").write_bytes(b"audio")
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch.object(collect.subprocess, "run", side_effect=fake_run):
                result = collect._download_audio("yt-dlp", "https://example.test/video", basename, 5.0)

        self.assertEqual(result.name, "audio.audio.mp3")
        command = command_seen[0]
        args = command[command.index("--postprocessor-args") + 1]
        self.assertIn("-b:a 48k", args)
        self.assertIn("-ac 1", args)
        self.assertIn("-ar 16000", args)

    def test_download_audio_retries_once_at_32k_when_oversize(self):
        sizes = [collect.MAX_AUDIO_BYTES + 1, collect.MAX_AUDIO_BYTES]
        with tempfile.TemporaryDirectory() as directory:
            basename = Path(directory) / "audio"
            commands = []

            def fake_run(command, **_kwargs):
                commands.append(command)
                path = Path(f"{basename}.audio.mp3")
                with path.open("wb") as audio_file:
                    audio_file.truncate(sizes.pop(0))
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch.object(collect.subprocess, "run", side_effect=fake_run):
                result = collect._download_audio("yt-dlp", "https://example.test/video", basename, 5.0)
                result_size = result.stat().st_size

        self.assertEqual(result_size, collect.MAX_AUDIO_BYTES)
        self.assertEqual(len(commands), 2)
        self.assertIn("-b:a 48k", commands[0][commands[0].index("--postprocessor-args") + 1])
        self.assertIn("-b:a 32k", commands[1][commands[1].index("--postprocessor-args") + 1])

    def test_download_audio_errors_when_32k_retry_still_exceeds_limit(self):
        sizes = [collect.MAX_AUDIO_BYTES + 1, collect.MAX_AUDIO_BYTES + 2]
        with tempfile.TemporaryDirectory() as directory:
            basename = Path(directory) / "audio"
            commands = []

            def fake_run(command, **_kwargs):
                commands.append(command)
                path = Path(f"{basename}.audio.mp3")
                with path.open("wb") as audio_file:
                    audio_file.truncate(sizes.pop(0))
                return subprocess.CompletedProcess(command, 0, "", "")

            with (
                patch.object(collect.subprocess, "run", side_effect=fake_run),
                self.assertRaisesRegex(RuntimeError, "audio exceeds Groq's 25 MB upload limit"),
            ):
                collect._download_audio("yt-dlp", "https://example.test/video", basename, 5.0)

        self.assertEqual(len(commands), 2)
        self.assertIn("-b:a 32k", commands[1][commands[1].index("--postprocessor-args") + 1])


class BrowserCollectTests(unittest.TestCase):
    def test_opencli_json_is_parsed_for_all_three_platforms(self):
        responses = {
            ".note-item": [
                {"href": "/explore/xhs1", "title": "小红书标题", "author": "作者", "published": "昨天", "likes": "13"}
            ],
            ".SearchResult-Card": [
                {"href": "//zhuanlan.zhihu.com/p/2", "title": "知乎标题"},
                {"href": "/question/3", "title": "知乎问题"},
            ],
            'a[href*="/r/"]': [
                {"href": "/r/test/comments/4/post/", "title": "Reddit post"},
                {"href": "/r/test/comments/4/post/", "title": "Reddit duplicate"},
            ],
        }

        def fake_run(command, **kwargs):
            if command[-2] == "open":
                return subprocess.CompletedProcess(command, 0, "", "")
            script = command[-1]
            rows = next(value for selector, value in responses.items() if selector in script)
            return subprocess.CompletedProcess(command, 0, json.dumps(rows, ensure_ascii=False), "")

        with (
            patch.object(collect_browser.subprocess, "run", side_effect=fake_run),
            patch.object(collect_browser.time, "sleep"),
        ):
            payload = collect_browser.run_collection("压力容器", limit=5)

        self.assertEqual(
            [item["platform"] for item in payload["items"]],
            ["xhs", "zhihu", "zhihu", "reddit"],
        )
        self.assertEqual(payload["items"][0]["url"], "https://www.xiaohongshu.com/explore/xhs1")
        self.assertEqual(payload["items"][0]["metrics"], {"likes": "13"})
        self.assertEqual(payload["items"][1]["url"], "https://zhuanlan.zhihu.com/p/2")
        self.assertEqual(payload["items"][2]["url"], "https://www.zhihu.com/question/3")
        self.assertEqual(payload["items"][3]["url"], "https://www.reddit.com/r/test/comments/4/post/")
        self.assertEqual(payload["items"][3]["metrics"], {})
        no_count = collect_browser._parse_rows(
            "xhs", [{"href": "/explore/xhs2", "title": "无计数", "likes": "赞"}], 1
        )
        self.assertEqual(no_count[0]["metrics"], {})
        self.assertEqual(payload["errors"], {})

    def test_open_failure_isolated_from_other_platforms(self):
        def fake_run(command, **kwargs):
            if command[-2] == "open":
                if "xiaohongshu.com" in command[-1]:
                    return subprocess.CompletedProcess(command, 1, "", "browser unavailable")
                return subprocess.CompletedProcess(command, 0, "", "")
            rows = [{"href": "/question/7", "title": "知乎仍可用"}]
            return subprocess.CompletedProcess(command, 0, json.dumps(rows, ensure_ascii=False), "")

        with (
            patch.object(collect_browser.subprocess, "run", side_effect=fake_run),
            patch.object(collect_browser.time, "sleep"),
        ):
            payload = collect_browser.run_collection("query", platforms=("xhs", "zhihu"), limit=3)

        self.assertIn("open failed", payload["errors"]["xhs"])
        self.assertEqual([item["title"] for item in payload["items"]], ["知乎仍可用"])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class GitHubIssueChannelTests(unittest.TestCase):
    def test_issue_search_appends_issue_qualifier_and_maps_fields(self):
        payload = {"items": [{
            "title": "Crash on startup",
            "html_url": "https://github.com/acme/app/issues/7",
            "updated_at": "2026-11-04T10:00:00Z",
            "body": "steps to reproduce",
            "comments": 3,
            "state": "open",
            "reactions": {"total_count": 12},
            "repository_url": "https://api.github.com/repos/acme/app",
        }]}
        with patch.object(collect, "_request_json", return_value=payload) as request:
            items = collect._collect_github_issues("crash", 1)
        params = request.call_args[0][1]
        self.assertEqual(params["q"], "crash is:issue")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["repo"], "acme/app")
        self.assertEqual(items[0]["metrics"]["reactions"], 12)

    def test_token_is_sent_only_when_configured(self):
        with patch.object(collect.config, "get", return_value="ghp_abc"):
            self.assertEqual(collect._github_headers()["Authorization"], "Bearer ghp_abc")
        with patch.object(collect.config, "get", return_value=""):
            self.assertNotIn("Authorization", collect._github_headers())

    def test_discussions_require_token_with_actionable_message(self):
        with patch.object(collect.config, "get", return_value=""):
            with self.assertRaises(RuntimeError) as caught:
                collect._collect_github_discussions("mcp", 1)
        self.assertIn("GITHUB_TOKEN", str(caught.exception))
        self.assertIn("config set", str(caught.exception))


class ForumChannelTests(unittest.TestCase):
    def test_v2ex_maps_topic_fields_and_sorts_newest_first(self):
        payload = {"hits": [
            {"_source": {"id": 1, "title": "old", "created": "2020-01-01T00:00:00", "replies": 2}},
            {"_source": {"id": 2, "title": "new", "created": "2026-01-01T00:00:00", "replies": 5}},
        ]}
        with patch.object(collect, "_request_json", return_value=payload):
            items = collect._collect_v2ex("clash", 2)
        self.assertEqual([item["title"] for item in items], ["new", "old"])
        self.assertEqual(items[0]["url"], "https://www.v2ex.com/t/2")
        self.assertEqual(items[0]["metrics"]["replies"], 5)

    def test_stackoverflow_maps_epoch_and_metrics(self):
        payload = {"items": [{
            "title": "Asyncio cancellation",
            "link": "https://stackoverflow.com/questions/1",
            "creation_date": 1557379719,
            "score": 31,
            "answer_count": 2,
            "is_answered": True,
            "tags": ["python", "asyncio"],
        }]}
        with patch.object(collect, "_request_json", return_value=payload) as request:
            items = collect._collect_stackoverflow("asyncio", 1)
        self.assertEqual(request.call_args[0][1]["site"], "stackoverflow")
        self.assertEqual(items[0]["metrics"]["answers"], 2)
        self.assertTrue(items[0]["published"].startswith("2019-05-09"))

    def test_new_forum_channels_are_registered(self):
        self.assertIn("v2ex", collect.SUPPORTED_PLATFORMS)
        self.assertIn("so", collect.SUPPORTED_PLATFORMS)
