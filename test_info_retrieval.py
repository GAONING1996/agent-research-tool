import io
import json
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

import fetch_smart
import search_dual


class SearchDualTests(unittest.TestCase):
    def test_repair_gbk_mojibake(self):
        mojibake = "20269ߣȫƽ̨ͨϸҪ_ϷӲԿ-йش"

        repaired = search_dual._repair_text(mojibake)

        self.assertEqual(repaired, mojibake)
        results = search_dual.merge_results(
            [{"url": "https://example.com/garbled", "title": mojibake}], []
        )
        self.assertEqual(results[0]["title"], mojibake)
        self.assertIs(results[0]["title_encoding_unreliable"], True)

    def test_repair_accepts_clean_latin1_gbk_candidate(self):
        self.assertEqual(search_dual._repair_text("ÖÐ¹ú"), "中国")

    def test_repair_preserves_normal_english_title(self):
        title = "The best game lineup for 2026"

        self.assertEqual(search_dual._repair_text(title), title)
        result = search_dual.merge_results(
            [{"url": "https://example.com/english", "title": title}], []
        )[0]
        self.assertNotIn("title_encoding_unreliable", result)

    def test_merge_repairs_title_and_snippet_for_both_sources(self):
        mojibake = "ÖÐ¹ú"
        results = search_dual.merge_results(
            [
                {
                    "url": "https://tavily.example/repair",
                    "title": mojibake,
                    "content": mojibake,
                }
            ],
            [
                {
                    "url": "https://searx.example/repair",
                    "title": mojibake,
                    "content": mojibake,
                }
            ],
        )

        self.assertEqual(len(results), 2)
        for result in results:
            self.assertEqual(result["title"], "中国")
            self.assertEqual(result["snippet"], "中国")

    def test_merge_uses_gbk_page_title_when_provider_title_is_lossy(self):
        mojibake = "20269ߣȫƽ̨ͨϸҪ_ϷӲԿ-йش"

        class Response:
            status_code = 200
            encoding = "gbk"
            content = (
                "<html><head><title>王者万象棋2026年9月上线，全平台互通并公布详细配置要求"
                "_游戏硬件显卡-中关村在线</title></head></html>"
            ).encode("gbk")

        with patch.object(search_dual.requests, "get", return_value=Response()):
            results = search_dual.merge_results(
                [{"url": "https://example.com/page", "title": mojibake}], []
            )

        self.assertEqual(
            results[0]["title"],
            "王者万象棋2026年9月上线，全平台互通并公布详细配置要求_游戏硬件显卡-中关村在线",
        )
        self.assertNotIn("title_encoding_unreliable", results[0])

    def test_url_normalization_ignores_tracking_and_http_scheme(self):
        first = search_dual.normalize_url(
            "https://Example.com/a/?utm_source=x&b=2"
        )
        second = search_dual.normalize_url("http://example.com/a?b=2")
        self.assertEqual(first, second)
        self.assertEqual(first, "https://example.com/a?b=2")

    def test_merge_deduplicates_and_prioritizes_shared_results(self):
        tavily = [
            {
                "url": "https://example.com/shared/?utm_source=x",
                "title": "Shared",
                "content": "short",
            },
            {"url": "https://tavily.example/only", "title": "T only", "content": "T"},
        ]
        searxng = [
            {
                "url": "http://example.com/shared",
                "title": "Shared result",
                "content": "a longer shared snippet",
            },
            {"url": "https://searx.example/only", "title": "S only", "content": "S"},
        ]

        results = search_dual.merge_results(tavily, searxng)

        self.assertEqual(len(results), 3)
        self.assertEqual(results[0]["sources"], ["tavily", "searxng"])
        self.assertEqual(results[0]["snippet"], "a longer shared snippet")
        self.assertEqual(results[1]["sources"], ["tavily"])
        self.assertEqual(results[2]["sources"], ["searxng"])

    def test_searx_failure_returns_tavily_results_as_degraded(self):
        rows = [{"url": "https://example.com", "title": "Result", "content": "text"}]
        with (
            patch.object(search_dual, "_search_tavily", return_value=rows),
            patch.object(search_dual, "_search_searxng", side_effect=RuntimeError("offline")),
        ):
            payload = search_dual.search_dual("query")

        self.assertTrue(payload["degraded"])
        self.assertEqual(len(payload["results"]), 1)
        self.assertIn("offline", payload["errors"]["searxng"])

    def test_declined_tavily_is_skipped_without_warning(self):
        rows = [{"url": "https://example.com", "title": "Result", "content": "text"}]
        with (
            patch.object(search_dual.capabilities, "declined", return_value={"TAVILY_API_KEY"}),
            patch.object(search_dual.config, "get", return_value=""),
            patch.object(search_dual, "_search_tavily") as tavily,
            patch.object(search_dual, "_search_searxng", return_value=rows),
        ):
            payload = search_dual.search_dual("query")

        tavily.assert_not_called()
        self.assertEqual(payload["errors"], {})
        self.assertFalse(payload["degraded"])
        self.assertEqual(len(payload["results"]), 1)

    def test_both_sources_fail_and_cli_returns_three(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.object(search_dual, "_search_tavily", side_effect=RuntimeError("T offline")),
            patch.object(search_dual, "_search_searxng", side_effect=RuntimeError("S offline")),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = search_dual.main(["query", "--json"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 3)
        self.assertEqual(payload["results"], [])
        self.assertTrue(payload["degraded"])
        self.assertIn("T offline", payload["errors"]["tavily"])
        self.assertIn("S offline", payload["errors"]["searxng"])


class FetchSmartTests(unittest.TestCase):
    def test_short_tavily_and_http_escalate_to_browser_dom(self):
        dom = (
            "<html><head><title>Rendered title</title></head><body>"
            "<nav>navigation</nav><main>"
            + ("Rendered article content. " * 40)
            + "</main></body></html>"
        )
        completed = subprocess.CompletedProcess(
            args=["chrome"], returncode=0, stdout=dom, stderr=""
        )
        with (
            patch.object(fetch_smart, "_tavily_extract", return_value=("T", "t" * 100)),
            patch.object(fetch_smart, "_http_extract", return_value=("H", "h" * 120)),
            patch.object(fetch_smart, "CHROME_PATH") as chrome_path,
            patch.object(fetch_smart.subprocess, "run", return_value=completed) as run,
        ):
            chrome_path.is_file.return_value = True
            payload = fetch_smart.fetch_smart("https://example.com", min_chars=500)

        self.assertEqual(payload["method"], "browser")
        self.assertGreaterEqual(payload["chars"], 500)
        self.assertEqual(payload["title"], "Rendered title")
        self.assertNotIn("navigation", payload["text"])
        self.assertLessEqual(run.call_args.kwargs["timeout"], 30)


if __name__ == "__main__":
    unittest.main()
