"""Offline tests for article extraction and Bilibili metadata handling."""

from __future__ import annotations

import io
import json
import os
import subprocess
import textwrap
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import fetch_smart


class FetchSmartTests(unittest.TestCase):
    def setUp(self):
        # The real .env may enable the Notebook route; default these tests to the
        # plain HTTP/Tavily/OpenCLI chain unless a test opts in explicitly.
        notebook_off = patch.object(fetch_smart, "_notebook_base_url", return_value="")
        notebook_off.start()
        self.addCleanup(notebook_off.stop)

    def test_extracts_longest_article_container_and_removes_noise(self):
        html = """
        <html><head><title>Guide</title><script>script noise</script></head><body>
          <nav>navigation noise</nav><header>header noise</header>
          <article><p>short article</p></article>
          <main><p>real guide content with enough useful detail</p>
            <aside>aside noise</aside><div class="recommend">recommend noise</div>
            <div class="sidebar">sidebar noise</div>
            <form>form noise</form><footer>footer noise</footer>
            <p>second paragraph</p>
          </main>
          <div class="nav">class nav noise</div>
          <div class="title"><span class="spanlist_1">breadcrumb noise</span></div>
        </body></html>
        """

        title, text = fetch_smart._extract_html(html)

        self.assertEqual(title, "Guide")
        self.assertIn("real guide content", text)
        self.assertIn("second paragraph", text)
        for noise in ("navigation noise", "script noise", "aside noise", "recommend noise", "sidebar noise", "form noise", "footer noise", "class nav noise", "breadcrumb noise"):
            self.assertNotIn(noise, text)

    def test_falls_back_to_paragraphs_and_removes_base64_images(self):
        encoded = "A" * 300
        html = f"""
        <html><body><div>outside paragraph text</div>
          <p>Useful paragraph</p><p>data:image/png;base64,{encoded} tail text</p>
          <script><p>script paragraph noise</p></script>
        </body></html>
        """

        _title, text = fetch_smart._extract_html(html)

        self.assertNotIn("outside paragraph text", text)
        self.assertNotIn(encoded, text)
        self.assertNotIn("script paragraph noise", text)
        self.assertIn("Useful paragraph", text)
        self.assertIn("tail text", text)

    def test_quality_counts_letters_and_digits_and_marks_low_quality(self):
        self.assertEqual(fetch_smart._quality("攻略2026"), 1.0)
        result = fetch_smart._result("https://example.com", "http", "title", "导航 !!!")
        self.assertLess(result["quality"], 0.5)
        self.assertTrue(result["low_quality"])
        self.assertEqual(result["quality_reason"], "short")

    def test_link_dense_long_body_is_retained_and_marked_low(self):
        source = (
            "<html><head><title>Menu</title></head><body><main>"
            "<p><a href='https://example.com/menu'>" + "A" * 750 + "</a>"
            + "B" * 452 + "!" * 300 + "</p></main></body></html>"
        )
        title, text = fetch_smart._extract_html(source)
        result = fetch_smart._result("https://example.com/menu", "http", title, text)

        self.assertGreaterEqual(result["chars"], 1500)
        self.assertEqual(result["quality"], 0.8)
        self.assertAlmostEqual(result["noise_ratio"], 0.5, places=2)
        self.assertEqual(result["quality_reason"], "link_dense")
        self.assertTrue(result["low_quality"])
        self.assertTrue(result["method"].endswith(":low"))

    def test_base64_residue_is_diagnosed_before_text_cleanup(self):
        text = "Useful article content " * 50 + "data:image/png;base64," + "Ab9+/CdefGHIJKLMN0123" * 30

        result = fetch_smart._result("https://example.com/image", "http", "Article", text)

        self.assertEqual(result["quality_reason"], "base64_residue")
        self.assertTrue(result["low_quality"])
        self.assertTrue(result["method"].endswith(":low"))

    def test_repeated_navigation_terms_in_long_page_mark_template(self):
        terms = ["主站", "商城", "论坛", "登录", "注册", "首页"]
        lines = [f"{terms[index % len(terms)]} article section {index:02d} content" for index in range(45)]
        result = fetch_smart._result(
            "https://example.com/template", "http", "Template", "\n".join(lines),
        )

        self.assertGreaterEqual(result["chars"], 800)
        self.assertEqual(result["quality_reason"], "template")
        self.assertTrue(result["low_quality"])

    def test_tavily_markdown_is_limited_to_the_article(self):
        raw = textwrap.dedent("""
        * [navigation](https://example.com/nav)

        # 王者万象棋姜导阵容推荐

        来源：18183

        姜导阵容正文包含廉颇、芈月和西施。

        [免责声明：正文结束](https://example.com/legal)

        ## 热门视频
        推荐内容
        """)

        text = fetch_smart._tavily_body("王者万象棋姜导阵容推荐_18183专区", raw)

        self.assertTrue(text.startswith("# 王者万象棋姜导阵容推荐"))
        self.assertIn("姜导阵容正文", text)
        self.assertNotIn("navigation", text)
        self.assertNotIn("热门视频", text)

    def test_bilibili_metadata_includes_description_and_clean_subtitles(self):
        calls = []

        class FakeExecutable:
            def is_file(self):
                return True

            def __str__(self):
                return "yt-dlp.exe"

        def fake_run(command, **_kwargs):
            calls.append(command)
            if "--print" in command:
                return SimpleNamespace(stdout="Video description", stderr="", returncode=0)
            output_template = Path(command[command.index("-o") + 1])
            prefix = output_template.name.split("%", 1)[0]
            caption_path = output_template.parent / f"{prefix}BVtest.zh-Hans.vtt"
            caption_path.write_text(
                "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n字幕正文第一句\n\n"
                "00:00:02.000 --> 00:00:03.000\n字幕正文第二句\n",
                encoding="utf-8",
            )
            return SimpleNamespace(stdout="", stderr="", returncode=0)

        with patch.object(fetch_smart, "YT_DLP_PATH", FakeExecutable()), patch.object(fetch_smart, "YT_DLP_PROXY", "http://127.0.0.1:7897"), patch.object(
            fetch_smart.subprocess, "run", side_effect=fake_run
        ):
            result = fetch_smart.fetch_smart("https://www.bilibili.com/video/BVtest")

        self.assertEqual(result["method"], "bilibili-meta:low")
        self.assertEqual(result["quality_reason"], "short")
        self.assertIn("Video description", result["text"])
        self.assertIn("字幕正文第一句", result["text"])
        self.assertIn("字幕正文第二句", result["text"])
        self.assertNotIn("00:00:", result["text"])
        self.assertEqual(calls[0][calls[0].index("--proxy") + 1], "http://127.0.0.1:7897")
        self.assertIn("--write-auto-sub", calls[1])

    def test_bilibili_subtitle_failure_does_not_drop_description(self):
        calls = []

        class FakeExecutable:
            def is_file(self):
                return True

            def __str__(self):
                return "yt-dlp.exe"

        def fake_run(command, **kwargs):
            calls.append(command)
            if "--print" in command:
                return SimpleNamespace(stdout="Description remains available", stderr="", returncode=0)
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])

        with patch.object(fetch_smart, "YT_DLP_PATH", FakeExecutable()), patch.object(fetch_smart, "YT_DLP_PROXY", "http://127.0.0.1:7897"), patch.object(
            fetch_smart.subprocess, "run", side_effect=fake_run
        ):
            result = fetch_smart.fetch_smart("https://www.bilibili.com/video/BVtest")

        self.assertEqual(result["method"], "bilibili-meta:low")
        self.assertEqual(result["text"], "Description remains available")

    def test_youtube_metadata_includes_video_fields_and_clean_subtitles(self):
        calls = []

        class FakeExecutable:
            def is_file(self):
                return True

            def __str__(self):
                return "yt-dlp.exe"

        def fake_run(command, **_kwargs):
            calls.append(command)
            if "--print" in command:
                self.assertEqual(
                    command[command.index("--print") + 1],
                    "%(title)s\n%(uploader)s\n%(upload_date)s\n%(description)s",
                )
                return SimpleNamespace(
                    stdout="Sample video\nChannel name\n20261102\nUseful video description",
                    stderr="",
                    returncode=0,
                )
            output_template = Path(command[command.index("-o") + 1])
            prefix = output_template.name.split("%", 1)[0]
            caption_path = output_template.parent / f"{prefix}video.zh-Hans.vtt"
            caption_path.write_text(
                "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nRepeated subtitle line\n"
                "00:00:02.000 --> 00:00:03.000\nRepeated subtitle line\n"
                "00:00:03.000 --> 00:00:04.000\nA second subtitle line\n",
                encoding="utf-8",
            )
            return SimpleNamespace(stdout="", stderr="", returncode=0)

        with patch.object(fetch_smart, "YT_DLP_PATH", FakeExecutable()), patch.object(fetch_smart, "YT_DLP_PROXY", "http://127.0.0.1:7897"), patch.object(
            fetch_smart.subprocess, "run", side_effect=fake_run
        ):
            result = fetch_smart.fetch_smart(
                "https://www.youtube.com/watch?v=sample", min_chars=1
            )

        self.assertEqual(result["method"], "youtube-meta")
        self.assertEqual(result["title"], "Sample video")
        for expected in (
            "Sample video",
            "Channel name",
            "20261102",
            "Useful video description",
            "Repeated subtitle line",
            "A second subtitle line",
        ):
            self.assertIn(expected, result["text"])
        self.assertEqual(result["text"].count("Repeated subtitle line"), 1)
        self.assertNotIn("00:00:", result["text"])
        self.assertEqual(calls[0][calls[0].index("--proxy") + 1], "http://127.0.0.1:7897")
        self.assertEqual(calls[1][calls[1].index("--proxy") + 1], "http://127.0.0.1:7897")
        self.assertEqual(calls[1][calls[1].index("--sub-format") + 1], "vtt")

    def test_youtube_metadata_failure_returns_empty_without_page_fetch(self):
        class FakeExecutable:
            def is_file(self):
                return True

            def __str__(self):
                return "yt-dlp.exe"

        def failed_run(_command, **_kwargs):
            return SimpleNamespace(stdout="", stderr="restricted", returncode=1)

        with patch.object(fetch_smart, "YT_DLP_PATH", FakeExecutable()), patch.object(fetch_smart, "YT_DLP_PROXY", "http://127.0.0.1:7897"), patch.object(
            fetch_smart.subprocess, "run", side_effect=failed_run
        ), patch.object(fetch_smart, "_http_extract") as http_extract, patch.object(
            fetch_smart, "_tavily_extract"
        ) as tavily_extract, patch.object(fetch_smart, "_browser_extract") as browser_extract:
            result = fetch_smart.fetch_smart("https://youtu.be/sample")

        self.assertEqual(result["text"], "")
        self.assertEqual(result["chars"], 0)
        self.assertEqual(result["method"], "youtube-meta:failed")
        self.assertEqual(result["quality_reason"], "meta_unavailable")
        http_extract.assert_not_called()
        tavily_extract.assert_not_called()
        browser_extract.assert_not_called()

    def test_bilibili_metadata_failure_returns_empty_without_page_fetch(self):
        class FakeExecutable:
            def is_file(self):
                return True

            def __str__(self):
                return "yt-dlp.exe"

        def failed_run(_command, **_kwargs):
            return SimpleNamespace(stdout="", stderr="restricted", returncode=1)

        with patch.object(fetch_smart, "YT_DLP_PATH", FakeExecutable()), patch.object(fetch_smart, "YT_DLP_PROXY", "http://127.0.0.1:7897"), patch.object(
            fetch_smart.subprocess, "run", side_effect=failed_run
        ), patch.object(fetch_smart, "_http_extract") as http_extract:
            result = fetch_smart.fetch_smart(
                "https://www.bilibili.com/video/BVtest"
            )

        self.assertEqual(result["text"], "")
        self.assertEqual(result["method"], "bilibili-meta:failed")
        self.assertEqual(result["quality_reason"], "meta_unavailable")
        http_extract.assert_not_called()

    def test_non_video_url_keeps_regular_http_extraction(self):
        body = "Readable article text " * 60
        with patch.object(
            fetch_smart, "_http_extract", return_value=("Article", body)
        ) as http_extract, patch.object(fetch_smart, "_tavily_extract") as tavily_extract, patch.object(
            fetch_smart, "_browser_extract"
        ) as browser_extract:
            result = fetch_smart.fetch_smart("https://example.com/article")

        self.assertEqual(result["method"], "http")
        self.assertEqual(result["text"], body.strip())
        http_extract.assert_called_once()
        tavily_extract.assert_not_called()
        browser_extract.assert_not_called()

    def test_cli_logged_in_route_uses_opencli_and_reports_its_method(self):
        body = "已登录正文，v1.8.1 更新公告。" * 120
        completed = [
            SimpleNamespace(stdout="", stderr="", returncode=0),
            SimpleNamespace(stdout=json.dumps(body, ensure_ascii=False), stderr="", returncode=0),
        ]
        output = io.StringIO()
        with patch.object(fetch_smart.subprocess, "run", side_effect=completed) as run, patch.object(
            fetch_smart.time, "sleep"
        ), redirect_stdout(output):
            exit_code = fetch_smart.main(["https://example.com/article", "--route", "logged-in", "--json"])

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["method"], "opencli")
        self.assertEqual(payload["text"], body)
        self.assertEqual(run.call_count, 2)
        self.assertIn("browser", run.call_args_list[0].args[0])
        self.assertEqual(run.call_args_list[0].args[0][-2:], ["open", "https://example.com/article"])

    def test_auto_route_uses_opencli_for_short_login_wall_text(self):
        body = "登录后的完整公告 v1.8.1 更新公告。" * 100
        completed = [
            SimpleNamespace(stdout="", stderr="", returncode=0),
            SimpleNamespace(stdout=json.dumps(body, ensure_ascii=False), stderr="", returncode=0),
        ]
        with patch.object(fetch_smart, "_http_extract", return_value=("Login", "微信登录")), patch.object(
            fetch_smart, "_tavily_extract", side_effect=RuntimeError("no Tavily result")
        ), patch.object(fetch_smart, "_browser_extract", side_effect=RuntimeError("no browser result")), patch.object(
            fetch_smart.subprocess, "run", side_effect=completed
        ) as run, patch.object(fetch_smart.time, "sleep"):
            result = fetch_smart.fetch_smart("https://example.com/article", min_chars=800)

        self.assertEqual(result["method"], "opencli")
        self.assertFalse(result["login_wall"])
        self.assertEqual(result["text"], body)
        self.assertEqual(run.call_count, 2)

    def test_auto_route_skips_opencli_when_body_meets_minimum_without_login_marker(self):
        with patch.object(fetch_smart, "_http_extract", return_value=("Notice", "Short system maintenance notice.")), patch.object(
            fetch_smart, "_tavily_extract", side_effect=RuntimeError("no Tavily result")
        ), patch.object(fetch_smart, "_browser_extract", side_effect=RuntimeError("no browser result")), patch.object(
            fetch_smart.subprocess, "run"
        ) as run:
            result = fetch_smart.fetch_smart("https://example.com/notice", min_chars=10)

        self.assertEqual(result["method"], "http:low")
        self.assertFalse(result["login_wall"])
        run.assert_not_called()

    def test_auto_route_uses_opencli_when_all_standard_paths_return_no_usable_text(self):
        body = "正文由已登录会话读取。" * 100
        with patch.object(fetch_smart, "_http_extract", return_value=("TapTap", "")), patch.object(
            fetch_smart, "_tavily_extract", return_value=("TapTap", "")
        ), patch.object(fetch_smart, "_browser_extract", return_value=("TapTap", "")), patch.object(
            fetch_smart, "_opencli_extract", return_value=("TapTap 正文", body)
        ) as opencli:
            result = fetch_smart.fetch_smart("https://www.taptap.cn/moment/example", min_chars=800)

        opencli.assert_called_once()
        self.assertEqual(result["method"], "opencli")
        self.assertEqual(result["text"], body)
        self.assertFalse(result["login_wall"])

    def test_single_fetch_budget_is_at_most_30_seconds(self):
        self.assertLessEqual(fetch_smart.FETCH_BUDGET_SECONDS, 30)

    def test_budget_timeout_returns_best_result_and_records_reason(self):
        def slow_http(_url, _timeout):
            time.sleep(0.02)
            return "Notice", "Short result"

        with patch.object(fetch_smart, "FETCH_BUDGET_SECONDS", 0.01), patch.object(
            fetch_smart, "_http_extract", side_effect=slow_http
        ), patch.object(fetch_smart, "_tavily_extract") as tavily, patch.object(
            fetch_smart, "_browser_extract"
        ) as browser, patch.object(fetch_smart, "_opencli_extract") as opencli:
            result = fetch_smart.fetch_smart("https://example.com/slow", min_chars=800)

        self.assertEqual(result["text"], "Short result")
        self.assertTrue(any("fetch timed out after 0.01s" in reason for reason in result["reasons"]))
        tavily.assert_not_called()
        browser.assert_not_called()
        opencli.assert_not_called()

    def test_auto_route_preserves_best_result_and_reports_opencli_failure(self):
        with patch.object(fetch_smart, "_http_extract", return_value=("Login", "微信登录")), patch.object(
            fetch_smart, "_tavily_extract", side_effect=RuntimeError("no Tavily result")
        ), patch.object(fetch_smart, "_browser_extract", side_effect=RuntimeError("no browser result")), patch.object(
            fetch_smart.subprocess, "run", side_effect=OSError("opencli missing")
        ) as run, patch.object(fetch_smart.time, "sleep"):
            result = fetch_smart.fetch_smart("https://example.com/article", min_chars=800)

        self.assertEqual(result["method"], "http:low")
        self.assertEqual(result["text"], "微信登录")
        self.assertTrue(result["login_wall"])
        self.assertTrue(any("opencli open could not start" in reason for reason in result["reasons"]))
        run.assert_called_once()

    def test_logged_in_route_reports_opencli_lock_timeout(self):
        with patch.dict(os.environ, {"OPENCLI_LOCK_PATH": "C:/tmp/fetch_smart_opencli.lock"}), patch.object(
            fetch_smart._OpenCLILock, "acquire", return_value=False
        ), patch.object(fetch_smart.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "opencli_lock_timeout") as raised:
                fetch_smart.fetch_smart("https://example.com/article", route="logged-in")

        self.assertIn("当前检查结果:", str(raised.exception))
        self.assertIn("打开 Chrome 并确认已登录目标站点", str(raised.exception))
        self.assertIn("python fetch_smart.py", str(raised.exception))
        self.assertGreaterEqual(run.call_count, 1)

    def test_logged_in_route_failure_includes_session_checks_and_fallback_guidance(self):
        with patch.object(fetch_smart, "_http_extract") as http_extract, patch.object(
            fetch_smart, "_tavily_extract"
        ) as tavily_extract, patch.object(fetch_smart, "_browser_extract") as browser_extract, patch.object(
            fetch_smart.subprocess, "run", side_effect=OSError("opencli missing")
        ):
            with self.assertRaisesRegex(RuntimeError, "opencli") as raised:
                fetch_smart.fetch_smart("https://example.com/article", route="logged-in")

        message = str(raised.exception)
        self.assertIn("chrome_running=", message)
        self.assertIn("opencli_session=", message)
        self.assertIn("打开 Chrome 并确认已登录目标站点", message)
        self.assertIn("python fetch_smart.py", message)
        self.assertIn("--route auto --json", message)
        http_extract.assert_not_called()
        tavily_extract.assert_not_called()
        browser_extract.assert_not_called()


    def test_reader_preamble_is_stripped_from_bodies(self):
        raw = "\n".join([
            "Title: 示例",
            "URL Source: https://x.com/a",
            "Published Time: 2024-09-16T11:00:26+0800",
            "Markdown Content:",
            "X",
            "",
            "正文第一段。",
        ])
        result = fetch_smart._result("https://x.com/a", "http", "t", raw)
        self.assertEqual(result["text"], "正文第一段。")

    def test_normal_body_keeps_metadata_like_lines(self):
        raw = "\n".join(["正文开头", "Title: 不是头部"])
        result = fetch_smart._result("https://x.com/b", "http", "", raw)
        self.assertIn("正文开头", result["text"])
        self.assertIn("Title: 不是头部", result["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
