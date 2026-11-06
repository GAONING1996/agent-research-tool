"""Offline tests for the tree-based research runner."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import patch

import research


def completed(payload: dict, returncode: int = 0):
    return SimpleNamespace(
        stdout=json.dumps(payload, ensure_ascii=False),
        stderr="",
        returncode=returncode,
    )


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.state_path = Path(__file__).resolve().parent / f".research_test_{os.getpid()}_{id(self)}.json"

    def test_current_directory_dotenv_precedes_user_config(self):
        config = research.config
        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory(prefix="research-config-") as tmp:
            project_dir = Path(tmp)
            (project_dir / ".env").write_text("RESEARCH_TEST_PROJECT_ENV=project\n", encoding="utf-8")
            user_config = project_dir / "config.env"
            user_config.write_text("RESEARCH_TEST_PROJECT_ENV=user\n", encoding="utf-8")

            with mock.patch.object(config, "config_file", return_value=user_config), mock.patch.dict(
                os.environ, {"RESEARCH_TEST_PROJECT_ENV": ""}
            ):
                config.refresh()
                try:
                    os.chdir(project_dir)
                    self.assertEqual(config.find_env_file(), project_dir / ".env")
                    self.assertEqual(config.get("RESEARCH_TEST_PROJECT_ENV"), "project")
                finally:
                    os.chdir(original_cwd)
                    config.refresh()

    def tearDown(self):
        for path in self.state_path.parent.glob(self.state_path.name + "*"):
            if path.is_file():
                path.unlink(missing_ok=True)

    def create_state(
        self, question="root question", max_depth=3, platforms=("web",), platform_options=None, channels=None,
    ):
        return research.init_state(question, self.state_path, max_depth, platforms, platform_options, channels)

    def write_state(self, state):
        with research.FileLock(research._state_lock_path(self.state_path)):
            research._write_state_unlocked(self.state_path, state)

    def prepare_evidence_state(self, root_titles=("Root source one", "Root source two")):
        self.create_state("Root question")
        state = research.read_state(self.state_path)
        root = state["nodes"][0]
        root["status"] = "awaiting_review"
        root["evidence_ids"] = []
        state["items"] = []
        for index, title in enumerate(root_titles, 1):
            item_id = f"i{index}"
            root["evidence_ids"].append(item_id)
            state["items"].append({
                "id": item_id,
                "url": f"https://example.com/{item_id}",
                "title": title,
                "body": f"Body for {title}.",
                "node_ids": ["n0"],
                "node_refs": [{"node_id": "n0", "query_kind": "support"}],
            })
        child = {
            "id": "n1", "parent": "n0", "question": "Child question",
            "context": "", "acceptance": [], "scope_delta": "Adds independent evidence.",
            "depth": 1, "status": "awaiting_review", "channels": [], "queries": [],
            "queries_configured": False, "counter_queries": [], "evidence_ids": [],
            "dropped": 0, "dropped_spam": 0, "result": "",
            "review": {"verdict": "", "rationale": "", "unresolved": [], "reviewed_at": None},
        }
        child_ids = []
        for index in range(3, 6):
            item_id = f"i{index}"
            child_ids.append(item_id)
            state["items"].append({
                "id": item_id,
                "url": f"https://other.example/{item_id}",
                "title": f"Child source {index}",
                "body": f"Body for child {index}.",
                "node_ids": ["n1"],
                "node_refs": [{"node_id": "n1", "query_kind": "support"}],
            })
        child["evidence_ids"] = child_ids
        state["nodes"].append(child)
        state["edges"].append({"from": "n0", "to": "n1", "kind": "split"})
        self.write_state(state)

    def set_acceptance(self, acceptance):
        state = research.read_state(self.state_path)
        state["nodes"][0]["acceptance"] = acceptance
        with research.FileLock(research._state_lock_path(self.state_path)):
            research._write_state_unlocked(self.state_path, state)

    def set_prompt_data(self, items):
        state = research.read_state(self.state_path)
        node = state["nodes"][0]
        node["question"] = "How does the example work?"
        node["context"] = "Compare the documented behavior."
        node["acceptance"] = ["Use direct evidence", "Identify limitations"]
        node["evidence_ids"] = [item["id"] for item in items]
        state["items"] = items
        with research.FileLock(research._state_lock_path(self.state_path)):
            research._write_state_unlocked(self.state_path, state)

    def prepare_review_at_depth(self, depth):
        self.create_state(max_depth=max(3, depth + 1))
        state = research.read_state(self.state_path)
        parent = state["nodes"][0]
        for current_depth in range(1, depth + 1):
            parent["status"] = "partial"
            parent["review"]["verdict"] = "partial"
            node_id = f"n{current_depth}"
            node = {
                "id": node_id, "parent": parent["id"], "question": f"Question at depth {current_depth}",
                "context": "", "acceptance": [], "scope_delta": "Adds one independent check.",
                "depth": current_depth, "status": "awaiting_review" if current_depth == depth else "open",
                "channels": [], "queries": [], "queries_configured": False, "counter_queries": [],
                "evidence_ids": [], "dropped": 0, "dropped_spam": 0, "result": "",
                "review": {
                    "verdict": "", "rationale": "", "unresolved": [], "early_close": False,
                    "early_close_reason": None, "reviewed_at": None,
                },
            }
            state["nodes"].append(node)
            state["edges"].append({"from": parent["id"], "to": node_id, "kind": "split"})
            parent = node
        if depth == 0:
            parent["status"] = "awaiting_review"
        self.write_state(state)
        return state

    def mock_collectors(self, payload_factory):
        def fake_run(command, **kwargs):
            script = Path(command[1]).name
            query = command[2]
            payload = payload_factory(script, query, command)
            return completed(payload)

        return patch.object(research.subprocess, "run", side_effect=fake_run)

    def test_canonical_url_deduplicates_tracking_variants(self):
        first = research.canonicalize_url("http://Example.COM/a/?utm_source=x&fbclid=z#part")
        second = research.canonicalize_url("https://example.com/a")
        self.assertEqual(first, second)
        self.assertEqual(first, "https://example.com/a")

    def test_init_run_ready_records_queries_and_deduplicates_evidence(self):
        self.create_state("Find the current lineup")

        def payload(script, _query, _command):
            if script == "search_dual.py":
                return {"results": [
                    {"url": "https://example.com/story?utm_source=one", "title": "Story", "snippet": "short"},
                    {"url": "http://example.com/story/#fragment", "title": "Duplicate", "snippet": "copy"},
                ], "errors": {}}
            return {"items": [], "errors": {}}

        with self.mock_collectors(payload):
            state = research.run_ready(self.state_path)

        root = state["nodes"][0]
        self.assertEqual(root["status"], "awaiting_review")
        self.assertEqual(root["queries"], ["Find the current lineup"])
        self.assertEqual(root["evidence_ids"], ["i1"])
        self.assertEqual(state["platform_options"], {"detail": 0, "images": 0, "vision": 0})
        self.assertEqual(len(state["items"]), 1)
        self.assertEqual(state["items"][0]["node_ids"], ["n0"])

    def test_zero_result_retries_web_once_and_records_valid_retry_rows(self):
        self.create_state("Rare topic", channels=("hn",))
        commands = []

        def payload(script, _query, command):
            commands.append(command)
            if script == "collect.py":
                return {"items": [], "errors": {}}
            return {"results": [
                {"url": "https://example.com/one", "title": "First result", "snippet": "One"},
                {"url": "https://example.com/two", "title": "Second result", "snippet": "Two"},
            ], "errors": {}}

        with self.mock_collectors(payload):
            state = research.run_nodes(self.state_path, ["n0"], fetch=0)

        root = research.read_state(self.state_path)["nodes"][0]
        self.assertEqual([command[command.index("--platforms") + 1] for command in commands if Path(command[1]).name == "collect.py"], ["hn"])
        self.assertTrue(root["zero_result_retry"])
        self.assertEqual(root["retry_channels"], ["web"])
        self.assertEqual(root["retry_rows"], 2)
        self.assertEqual(len(root["evidence_ids"]), 2)
        self.assertEqual(state["channels_used"], ["hn", "web"])
        self.assertEqual(state["pending_review"], ["n0"])
        self.assertIn("零结果重试: 是（web，得到 2 条）", research.report_text(state))

    def test_no_retry_disables_zero_result_web_fallback(self):
        self.create_state("Rare topic", channels=("hn",))
        commands = []

        def fake_run(command, **_kwargs):
            commands.append(command)
            return completed({"items": [], "errors": {}})

        output = StringIO()
        with patch.object(research.subprocess, "run", side_effect=fake_run), redirect_stdout(output):
            self.assertEqual(research.main([
                "run", "--state", str(self.state_path), "--node", "n0", "--no-retry", "--fetch", "0", "--json",
            ]), 0)

        payload = json.loads(output.getvalue())["data"]
        root = research.read_state(self.state_path)["nodes"][0]
        self.assertEqual([Path(command[1]).name for command in commands], ["collect.py"])
        self.assertFalse(root["zero_result_retry"])
        self.assertEqual(root["retry_channels"], [])
        self.assertEqual(root["retry_rows"], 0)
        self.assertEqual(payload["channels_used"], ["hn"])

    def test_status_json_reports_layer_counts_pending_split_and_status_lists(self):
        self.prepare_review_at_depth(1)
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(research.main(["status", "--state", str(self.state_path), "--json"]), 0)

        payload = json.loads(output.getvalue())
        data = payload["data"]
        self.assertEqual(payload["command"], "status")
        self.assertTrue({
            "layers", "unmet_reasons", "pending_split", "pending_split_nodes", "open_nodes",
            "pending_review", "next_step", "suggested_commands", "next_command",
        }.issubset(data))
        self.assertEqual(data["layers"], {
            "current": 2, "required": 4, "met": False,
            "counts": {"1": 1, "2": 1, "3": 0, "4": 0}, "missing": 2,
        })
        self.assertFalse(data["gap_review_recorded"])
        self.assertIn("gap_review_not_recorded", [reason["code"] for reason in data["unmet_reasons"]])
        self.assertEqual(data["pending_split"], [])
        self.assertEqual(data["pending_split_nodes"], [])
        self.assertEqual(data["open_nodes"], [])
        self.assertEqual(data["pending_review"], ["n1"])
        self.assertTrue(any(cmd.startswith("python research.py") for cmd in data["suggested_commands"]))
        self.assertIn("layers_below_minimum", [reason["code"] for reason in data["unmet_reasons"]])

    def test_stats_channel_collects_and_preserves_tables_on_merged_item(self):
        self.create_state("Collect structured rankings", channels=("stats",))
        tables = [{"index": 1, "headers": ["Player"], "rows": [["Alpha"]]}]
        commands = []

        def payload(script, query, command):
            commands.append(command)
            if script == "collect.py":
                return {"items": [{
                    "title": "Ranking board",
                    "url": "https://example.com/rank",
                    "platform": "stats",
                    "body": "Structured ranking body",
                    "tables": tables,
                    "method": "http",
                    "chars": 2400,
                    "login_wall": False,
                }]}
            return {"items": [], "errors": {}}

        with self.mock_collectors(payload):
            state = research.run_nodes(self.state_path, ["n0"], fetch=0)

        collect_command = next(command for command in commands if Path(command[1]).name == "collect.py")
        self.assertEqual(collect_command[collect_command.index("--platforms") + 1], "stats")
        item = state["items"][0]
        self.assertEqual(item["tables"], tables)
        self.assertEqual(item["chars"], 2400)
        self.assertIs(item["login_wall"], False)

    def test_stats_tables_fill_an_existing_canonical_item(self):
        self.create_state("Collect structured rankings", channels=("stats",))
        state = research.read_state(self.state_path)
        state["items"] = [{
            "id": "i1",
            "url": "https://example.com/rank",
            "title": "Earlier result",
            "platform": "web",
            "body": "Short body",
            "node_ids": ["n0"],
        }]
        state["nodes"][0]["evidence_ids"] = ["i1"]
        self.write_state(state)
        tables = [{"index": 1, "headers": ["Player"], "rows": [["Alpha"]]}]

        def payload(script, _query, _command):
            if script == "collect.py":
                return {"items": [{
                    "title": "Ranking board",
                    "url": "https://example.com/rank",
                    "platform": "stats",
                    "body": "Structured ranking body",
                    "tables": tables,
                }]}
            return {"items": [], "errors": {}}

        with self.mock_collectors(payload):
            state = research.run_nodes(self.state_path, ["n0"], fetch=0)

        self.assertEqual(len(state["items"]), 1)
        self.assertEqual(state["items"][0]["tables"], tables)

    def test_spam_titles_and_domains_are_dropped_but_guides_remain(self):
        self.create_state("alpha beta")
        title_markers = ["黑料", "吃瓜", "成人", "短剧", "同人", "小说", "裸", "约炮", "彩票", "返利", "私服", "外挂"]
        domain_markers = ["mingxhal1", "57duanju", "juvmzfmte", "37e0", "51漫画", "hhl", "八卦"]
        rows = [
            {"url": f"https://example.com/title-spam-{index}", "title": f"内容{marker}合集"}
            for index, marker in enumerate(title_markers)
        ]
        rows.extend(
            {
                "url": f"https://source.{marker}.example/guide",
                "title": "Official strategy guide",
            }
            for marker in domain_markers
        )
        rows.extend([
            {"url": "https://gameguides.example/guide", "title": "攻略站导航大全 Wiki Guide"},
            {"url": "https://example.com/manus", "title": "Manus product overview"},
        ])

        def payload(script, _query, _command):
            if script == "search_dual.py":
                return {"results": rows, "errors": {}}
            return {"items": [], "errors": {}}

        with self.mock_collectors(payload):
            state = research.run_nodes(self.state_path, ["n0"], fetch=0)

        self.assertEqual(
            [item["title"] for item in state["items"]],
            ["攻略站导航大全 Wiki Guide", "Manus product overview"],
        )
        self.assertEqual(state["nodes"][0]["evidence_ids"], ["i1", "i2"])
        self.assertEqual(state["nodes"][0]["dropped_spam"], len(title_markers) + len(domain_markers))
        self.assertEqual(state["nodes"][0]["dropped"], 0)

    def test_item_contract_fills_defaults_preserves_ocr_and_counts_invalid_rows(self):
        self.create_state("contract check")

        def payload(script, _query, _command):
            if script == "search_dual.py":
                return {"results": [
                    {
                        "url": "https://example.com/valid", "title": "Valid result",
                        "ocr_text": "Recognized text", "vision_text": "Visual description",
                        "vision_source": "agnes",
                    },
                    {"url": "https://example.com/bad-title", "title": 12},
                    {"url": "not a URL", "title": "Bad URL"},
                    {"url": "https://[", "title": "Malformed URL"},
                    {"url": "https://example.com/blank", "title": "   "},
                    {"url": "https://example.com/bad-sources", "title": "Bad sources", "sources": "tavily"},
                ], "errors": {}}
            return {"items": [], "errors": {}}

        with self.mock_collectors(payload):
            state = research.run_nodes(self.state_path, ["n0"], fetch=0)

        item = state["items"][0]
        required = {
            "url", "canonical", "title", "platform", "published_raw", "published", "summary",
            "sources", "body", "body_chars", "method", "fetched_at", "node_ids",
        }
        self.assertTrue(required.issubset(item))
        self.assertFalse({"eligible", "low_quality", "quality_reason"} & item.keys())
        self.assertEqual(item["ocr_text"], "Recognized text")
        self.assertEqual(item["vision_text"], "Visual description")
        self.assertEqual(item["vision_source"], "agnes")
        self.assertEqual(state["nodes"][0]["dropped"], 5)

    def test_zero_title_hits_are_fetched_after_matching_titles(self):
        self.create_state("alpha beta")

        def payload(script, _query, _command):
            if script == "search_dual.py":
                return {"results": [
                    {"url": "https://example.com/unrelated", "title": "Celebrity gossip roundup"},
                    {"url": "https://example.com/matched", "title": "alpha beta verified"},
                ], "errors": {}}
            return {"items": [], "errors": {}}

        with self.mock_collectors(payload) as child:
            state = research.run_nodes(self.state_path, ["n0"], fetch=2)

        commands = [call.args[0] for call in child.call_args_list]
        fetched = [command[2] for command in commands if Path(command[1]).name == "fetch_smart.py"]
        self.assertEqual(fetched, ["https://example.com/matched", "https://example.com/unrelated"])
        self.assertEqual(state["nodes"][0]["status"], "awaiting_review")

    def test_fetch_candidates_sort_only_by_title_overlap_then_evidence_order(self):
        self.create_state("alpha beta")
        rows = [
            {"url": "https://www.bilibili.com/video/BV1abc123", "title": "alpha"},
            {"url": "https://example.com/published", "title": "alpha beta", "published": "2026-11-01"},
            {"url": "https://example.com/old", "title": "alpha beta"},
            {"url": "https://www.3dmgame.com/one", "title": "other"},
        ]
        with self.mock_collectors(lambda *_: {"results": rows, "errors": {}}):
            state = research.run_nodes(self.state_path, ["n0"], fetch=0)

        self.assertEqual(
            [item["id"] for item in research._fetch_candidates(state, state["nodes"][0], 20)],
            ["i2", "i3", "i1", "i4"],
        )

    def test_zero_title_hits_rank_after_matching_titles(self):
        self.create_state("alpha beta")
        rows = [
            {"url": "https://www.3dmgame.com/unrelated", "title": "Other content"},
            {"url": "https://example.com/matched", "title": "alpha beta guide"},
        ]
        with self.mock_collectors(lambda *_: {"results": rows, "errors": {}}):
            state = research.run_nodes(self.state_path, ["n0"], fetch=0)

        self.assertEqual(
            [item["url"] for item in research._fetch_candidates(state, state["nodes"][0], 2)],
            ["https://example.com/matched", "https://www.3dmgame.com/unrelated"],
        )

    def test_non_list_search_results_are_counted_as_dropped(self):
        self.create_state("malformed collector response")

        def payload(script, _query, _command):
            if script == "search_dual.py":
                return {"results": "not-a-list", "errors": {}}
            return {"items": [], "errors": {}}

        with self.mock_collectors(payload):
            state = research.run_nodes(self.state_path, ["n0"], fetch=0)

        self.assertEqual(state["nodes"][0]["dropped"], 1)
        self.assertEqual(state["nodes"][0]["dropped_spam"], 0)
        self.assertEqual(state["items"], [])

    def test_fetch_uses_requested_count_without_quality_short_circuit(self):
        self.create_state("alpha beta")
        rows = [
            {"url": f"https://{host}/guide", "title": "alpha beta guide"}
            for host in ("3dmgame.com", "18183.com", "ali213.net", "gamersky.com")
        ]
        fetched_urls = []
        body = "Useful walkthrough content. " * 45

        def fake_run(command, **_kwargs):
            if Path(command[1]).name == "search_dual.py":
                return completed({"results": rows, "errors": {}})
            url = command[2]
            fetched_urls.append(url)
            return completed({
                "url": url, "method": "http", "text": body, "quality": 0.9,
                "noise_ratio": 0.1, "quality_reason": "ok", "low_quality": False,
            })

        with patch.object(research.subprocess, "run", side_effect=fake_run):
            state = research.run_nodes(self.state_path, ["n0"], fetch=5)

        self.assertEqual(len(fetched_urls), 4)
        self.assertEqual(
            state["nodes"][0]["fetch_stats"],
            {"tried": 4, "ok": 4, "failed": 0, "chars": [len(body)] * 4},
        )

    def test_quality_metrics_are_facts_and_never_gate_body_use(self):
        self.create_state("alpha beta")
        rows = [
            {"url": "https://example.com/short", "title": "alpha beta short"},
            {"url": "https://example.com/long", "title": "alpha beta long"},
        ]
        bodies = {"https://example.com/short": "tiny", "https://example.com/long": "text" * 300}

        def fake_run(command, **_kwargs):
            if Path(command[1]).name == "search_dual.py":
                return completed({"results": rows, "errors": {}})
            url = command[2]
            return completed({
                "text": bodies[url], "method": "http", "quality": 0.2,
                "noise_ratio": 0.9, "quality_reason": "short", "low_quality": True,
            })

        with patch.object(research.subprocess, "run", side_effect=fake_run):
            state = research.run_nodes(self.state_path, ["n0"], fetch=2)

        items = {item["url"]: item for item in state["items"]}
        self.assertEqual(items["https://example.com/short"]["body"], "tiny")
        self.assertEqual(items["https://example.com/short"]["body_chars"], 4)
        self.assertEqual(items["https://example.com/short"]["quality"], 0.2)
        self.assertEqual(items["https://example.com/short"]["noise_ratio"], 0.9)
        self.assertFalse({"eligible", "low_quality", "quality_reason"} & items["https://example.com/short"].keys())
        self.assertEqual(research._fetch_candidates(state, state["nodes"][0], 10), [])
        self.assertEqual(
            state["nodes"][0]["fetch_stats"],
            {"tried": 2, "ok": 2, "failed": 0, "chars": [4, len(bodies["https://example.com/long"]) ]},
        )

    def test_acceptance_is_display_only_and_run_always_awaits_review(self):
        self.create_state("alpha beta")
        acceptance = ["至少两个独立来源", "核实发布日期和版本时间"]
        self.set_acceptance(acceptance)

        def payload(script, _query, _command):
            if script == "search_dual.py":
                return {"results": [{
                    "url": "https://example.com/article", "title": "alpha beta article",
                    "sources": ["tavily", "searxng"], "published": None,
                }], "errors": {}}
            return {"items": [], "errors": {}}

        with self.mock_collectors(payload):
            state = research.run_nodes(self.state_path, ["n0"], fetch=0)

        node = state["nodes"][0]
        self.assertEqual(state["items"][0]["sources"], ["tavily", "searxng"])
        self.assertEqual(node["acceptance"], acceptance)
        self.assertEqual(node["status"], "awaiting_review")
        self.assertEqual(node["evidence_ids"], ["i1"])
        self.assertFalse({"eligible", "hosts_eligible", "evidence"} & node.keys())

    def test_init_platform_options_are_forwarded_to_browser_collector(self):
        with redirect_stdout(StringIO()):
            result = research.main([
                "init", "browser query", "--state", str(self.state_path), "--platforms", "xhs",
                "--channels", "browser",
                "--detail", "2", "--images", "3", "--vision", "1",
            ])
        self.assertEqual(result, 0)
        self.assertEqual(
            research.read_state(self.state_path)["platform_options"],
            {"detail": 2, "images": 3, "vision": 1},
        )

        with patch.object(
            research.subprocess, "run", return_value=completed({"items": [], "errors": {}})
        ) as child, redirect_stdout(StringIO()):
            result = research.main(["run", "--state", str(self.state_path), "--node", "n0", "--fetch", "0"])

        self.assertEqual(result, 0)
        command = child.call_args_list[0].args[0]
        self.assertEqual(Path(command[1]).name, "collect_browser.py")
        self.assertEqual(command[command.index("--detail") + 1], "2")
        self.assertEqual(command[command.index("--images") + 1], "3")
        self.assertEqual(command[command.index("--vision") + 1], "1")
        self.assertEqual(Path(child.call_args_list[1].args[0][1]).name, "search_dual.py")
        self.assertTrue(research.read_state(self.state_path)["nodes"][0]["zero_result_retry"])

    def test_cli_fetch_count_includes_missing_bodies_even_when_existing_body_is_present(self):
        self.create_state("alpha beta gamma delta")
        existing_text = "Existing article content. " * 40
        rows = [
            {
                "url": "https://example.com/existing", "title": "alpha beta gamma delta source",
                "body": existing_text, "quality": 0.9, "low_quality": False,
            },
            {"url": "https://example.com/one", "title": "alpha beta source"},
            {"url": "https://example.com/two", "title": "alpha gamma source"},
            {"url": "https://example.com/three", "title": "beta delta source"},
        ]
        fetched_urls = []
        fetched_text = "Fetched article text. " * 50

        def fake_run(command, **kwargs):
            script = Path(command[1]).name
            if script == "search_dual.py":
                return completed({"results": rows, "errors": {}})
            if script == "fetch_smart.py":
                fetched_urls.append(command[2])
                self.assertEqual(kwargs["timeout"], 45)
                return completed({
                    "url": command[2], "method": "http", "text": fetched_text,
                    "quality": 0.9, "low_quality": False,
                })
            self.fail(f"unexpected subprocess: {script}")

        with patch.object(research.subprocess, "run", side_effect=fake_run), redirect_stdout(StringIO()):
            result = research.main([
                "run", "--state", str(self.state_path), "--node", "n0", "--fetch", "2",
            ])

        state = research.read_state(self.state_path)
        items = {item["url"]: item for item in state["items"]}
        self.assertEqual(result, 0)
        self.assertEqual(len(fetched_urls), 2)
        self.assertNotIn("https://example.com/existing", fetched_urls)
        self.assertEqual(items["https://example.com/existing"]["body_chars"], len(existing_text))
        self.assertTrue(all(items[url]["body"] == fetched_text for url in fetched_urls))
        self.assertTrue(all(items[url]["method"] == "http" for url in fetched_urls))
        self.assertEqual(
            state["nodes"][0]["fetch_stats"],
            {"tried": 2, "ok": 2, "failed": 0, "chars": [len(fetched_text)] * 2},
        )
        self.assertEqual(state["nodes"][0]["status"], "awaiting_review")

    def test_default_fetch_count_is_six(self):
        self.create_state("alpha beta")
        rows = [
            {"url": f"https://example.com/{index}", "title": "alpha beta item"}
            for index in range(7)
        ]
        fetched_urls = []

        def fake_run(command, **_kwargs):
            if Path(command[1]).name == "search_dual.py":
                return completed({"results": rows, "errors": {}})
            fetched_urls.append(command[2])
            return completed({"text": "body", "method": "http"})

        with patch.object(research.subprocess, "run", side_effect=fake_run):
            state = research.run_nodes(self.state_path, ["n0"])

        self.assertEqual(len(fetched_urls), 6)
        self.assertEqual(state["nodes"][0]["fetch_stats"]["tried"], 6)

    def test_fetch_stats_and_report_show_only_fetch_facts(self):
        self.create_state("alpha beta")
        rows = [
            {"url": "https://example.com/bad", "title": "alpha beta guide"},
            {"url": "https://example.com/good", "title": "alpha beta article"},
        ]
        fetched_urls = []
        good_text = "Useful article content. " * 45

        def fake_run(command, **_kwargs):
            script = Path(command[1]).name
            if script == "search_dual.py":
                return completed({"results": rows, "errors": {}})
            if script == "fetch_smart.py":
                url = command[2]
                fetched_urls.append(url)
                if url.endswith("/bad"):
                    return completed({
                        "url": url, "method": "http", "text": "navigation " * 100,
                        "quality": 0.2, "quality_reason": "short", "low_quality": True,
                    })
                return completed({
                    "url": url, "method": "http", "text": good_text,
                    "quality": 0.9, "low_quality": False,
                })
            self.fail(f"unexpected subprocess: {script}")

        with patch.object(research.subprocess, "run", side_effect=fake_run):
            state = research.run_nodes(self.state_path, ["n0"], fetch=2)

        items = {item["url"]: item for item in state["items"]}
        self.assertEqual(fetched_urls, ["https://example.com/bad", "https://example.com/good"])
        self.assertEqual(items["https://example.com/bad"]["quality"], 0.2)
        self.assertEqual(items["https://example.com/bad"]["body_chars"], len("navigation " * 100))
        self.assertFalse({"eligible", "low_quality", "quality_reason"} & items["https://example.com/bad"].keys())
        self.assertEqual(
            state["nodes"][0]["fetch_stats"],
            {"tried": 2, "ok": 2, "failed": 0, "chars": [len("navigation " * 100), len(good_text)]},
        )
        self.assertEqual(research._fetch_candidates(state, state["nodes"][0], 3), [])
        report = research.report_text(state)
        self.assertIn("fetch tried=2 ok=2 failed=0", report)
        self.assertIn("证据 2，已抓正文 2（字数", report)

    def test_fetch_failure_is_recorded_without_blocking_review(self):
        self.create_state("alpha beta")

        def fake_run(command, **_kwargs):
            if Path(command[1]).name == "search_dual.py":
                return completed({"results": [{
                    "url": "https://example.com/alpha",
                    "title": "alpha verified result",
                }], "errors": {}})
            return SimpleNamespace(stdout="", stderr="network unavailable", returncode=3)

        with patch.object(research.subprocess, "run", side_effect=fake_run):
            state = research.run_nodes(self.state_path, ["n0"], fetch=1)

        self.assertEqual(state["nodes"][0]["status"], "awaiting_review")
        self.assertEqual(len(state["nodes"][0]["fetch_errors"]), 1)
        self.assertIn("network unavailable", state["nodes"][0]["fetch_errors"][0])
        self.assertEqual(
            state["nodes"][0]["fetch_stats"],
            {"tried": 1, "ok": 0, "failed": 1, "chars": []},
        )

    def test_any_nonempty_body_skips_fetch_regardless_of_quality_fields(self):
        self.assertTrue(research._item_has_body({"body": "x", "quality": 0.0, "low_quality": True}))
        self.assertFalse(research._item_has_body({"body": ""}))

    def test_existing_body_is_not_refetched_or_cleared(self):
        self.create_state("alpha beta")

        def fake_run(command, **_kwargs):
            if Path(command[1]).name == "search_dual.py":
                return completed({"results": [{
                    "url": "https://example.com/legacy",
                    "title": "alpha beta guide",
                    "body": "old navigation " * 100,
                }], "errors": {}})
            self.fail("existing body should not be fetched again")

        with patch.object(research.subprocess, "run", side_effect=fake_run):
            state = research.run_nodes(self.state_path, ["n0"], fetch=1)

        item = state["items"][0]
        self.assertEqual(item["body"], "old navigation " * 100)
        self.assertNotIn("low_quality", item)
        self.assertEqual(
            state["nodes"][0]["fetch_stats"],
            {"tried": 0, "ok": 0, "failed": 0, "chars": []},
        )

    def test_reset_reopens_requested_nodes(self):
        self.create_state()
        with self.mock_collectors(lambda *_: {"results": [], "errors": {}}):
            state = research.run_nodes(self.state_path, ["n0"], fetch=0)
        self.assertEqual(state["nodes"][0]["status"], "awaiting_review")

        state = research.reset_nodes(self.state_path, ["n0"])

        self.assertEqual(state["nodes"][0]["status"], "open")

    def test_two_nodes_share_one_canonical_item_and_both_reference_it(self):
        self.create_state("Root topic")

        def payload(_script, _query, _command):
            return {"results": [{"url": "https://example.com/shared", "title": "Shared"}], "errors": {}}

        with self.mock_collectors(payload):
            research.run_nodes(self.state_path, ["n0"])
        research.apply_reviews(self.state_path, {
            "node_id": "n0",
            "verdict": "partial",
            "result": "One part is supported.",
            "children": [{
                "question": "Independently verify the second part of Root topic",
                "scope_delta": "Adds independent verification.",
            }],
        })
        with self.mock_collectors(payload):
            state = research.run_nodes(self.state_path, ["n1"])

        self.assertEqual(len(state["items"]), 1)
        self.assertEqual(state["items"][0]["node_ids"], ["n0", "n1"])
        self.assertEqual(state["nodes"][1]["evidence_ids"], ["i1"])

    def test_answered_node_is_sealed(self):
        self.create_state()
        with self.mock_collectors(lambda *_: {"results": [], "errors": {}}):
            research.run_nodes(self.state_path, ["n0"])
        reviewed = research.apply_reviews(self.state_path, {
            "node_id": "n0", "verdict": "answered", "result": "Verified answer.",
        })
        with patch.object(research.subprocess, "run") as child:
            state = research.run_nodes(self.state_path, ["n0"])
        child.assert_not_called()
        self.assertEqual(reviewed["nodes"][0]["status"], "answered")
        self.assertEqual(state["nodes"][0]["result"], "Verified answer.")

    def test_partial_creates_child_and_max_depth_closes_unresolved(self):
        self.create_state(max_depth=1)
        with self.mock_collectors(lambda *_: {"results": [], "errors": {}}):
            research.run_nodes(self.state_path, ["n0"])
        state = research.apply_reviews(self.state_path, {
            "node_id": "n0",
            "verdict": "partial",
            "unresolved": ["Need a version-specific source."],
            "children": [{
                "question": "Find a version-specific source for the root topic",
                "scope_delta": "Adds version-specific source validation.",
            }],
        })
        self.assertEqual(state["nodes"][1]["parent"], "n0")
        self.assertEqual(state["edges"], [{"from": "n0", "to": "n1", "kind": "split"}])
        with self.mock_collectors(lambda *_: {"results": [], "errors": {}}):
            research.run_nodes(self.state_path, ["n1"])
        state = research.apply_reviews(self.state_path, {
            "node_id": "n1", "verdict": "partial", "unresolved": ["Need one more check."],
            "early_close_reason": "The configured depth limit leaves no smaller independent check.",
        }, allow_early_close=True)
        self.assertEqual(state["nodes"][1]["status"], "closed_unresolved")
        self.assertEqual(state["nodes"][1]["review"]["verdict"], "partial")
        self.assertTrue(state["nodes"][1]["review"]["early_close"])

    def test_not_found_at_depth_limit_remains_not_found(self):
        self.create_state(max_depth=0)
        with self.mock_collectors(lambda *_: {"results": [], "errors": {}}):
            research.run_nodes(self.state_path, ["n0"], fetch=0)
        state = research.apply_reviews(self.state_path, {
            "node_id": "n0", "verdict": "not_found", "unresolved": ["No source found."],
            "early_close_reason": "No independent source path remains within the configured depth.",
        }, allow_early_close=True)
        self.assertEqual(state["nodes"][0]["status"], "not_found")

    def test_apply_review_requires_children_before_four_layers_and_allows_explicit_early_close(self):
        self.prepare_review_at_depth(1)
        review_path = Path(str(self.state_path) + ".review.json")
        review_path.write_text(json.dumps({
            "node_id": "n1",
            "verdict": "partial",
            "result": "One fact remains unresolved.",
            "early_close_reason": "The remaining evidence path is unavailable.",
        }), encoding="utf-8")
        error = StringIO()
        with redirect_stderr(error):
            exit_code = research.main([
                "apply-review", "--state", str(self.state_path), "--file", str(review_path),
            ])
        self.assertEqual(exit_code, 2)
        self.assertIn("research: 未达到 4 层，请继续拆或显式放弃", error.getvalue())
        self.assertIn("当前第 2 层", error.getvalue())
        self.assertIn("prompt --state", error.getvalue())
        self.assertIn("--allow-early-close", error.getvalue())
        self.assertEqual(research.read_state(self.state_path)["nodes"][-1]["status"], "awaiting_review")

        with self.assertRaisesRegex(ValueError, "requires early_close_reason"):
            research.apply_reviews(self.state_path, {
                "node_id": "n1", "verdict": "partial",
            }, allow_early_close=True)

        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(research.main([
                "apply-review", "--state", str(self.state_path), "--file", str(review_path),
                "--allow-early-close", "--json",
            ]), 0)
        stored = research.read_state(self.state_path)["nodes"][-1]["review"]
        self.assertTrue(stored["early_close"])
        self.assertEqual(stored["early_close_reason"], "The remaining evidence path is unavailable.")
        self.assertIn("层数: 2/4（未达标）", research.report_text(research.read_state(self.state_path)))

    def test_partial_without_children_is_rejected_before_fourth_layer(self):
        for depth in (0, 1, 2):
            with self.subTest(depth=depth):
                self.state_path.unlink(missing_ok=True)
                self.prepare_review_at_depth(depth)
                with self.assertRaisesRegex(ValueError, "未达到 4 层"):
                    research.apply_reviews(self.state_path, {
                        "node_id": f"n{depth}", "verdict": "partial", "unresolved": ["One fact remains."],
                    })

    def test_partial_without_children_is_allowed_at_fourth_layer(self):
        self.prepare_review_at_depth(3)
        state = research.apply_reviews(self.state_path, {
            "node_id": "n3", "verdict": "partial", "unresolved": ["One fact remains."],
        })
        self.assertEqual(state["nodes"][-1]["status"], "partial")
        self.assertFalse(state["nodes"][-1]["review"]["early_close"])
        self.assertIn("层数: 4/4（已达标）", research.report_text(state))

    def test_answered_can_close_at_root_without_children(self):
        self.prepare_review_at_depth(0)
        state = research.apply_reviews(self.state_path, {
            "node_id": "n0", "verdict": "answered", "result": "Direct evidence answers the question.",
        })
        self.assertEqual(state["nodes"][0]["status"], "answered")
        self.assertEqual(len(state["nodes"]), 1)

    def test_init_defaults_to_three_and_warns_when_max_depth_is_smaller(self):
        with redirect_stdout(StringIO()):
            self.assertEqual(research.main([
                "init", "Default depth", "--state", str(self.state_path),
            ]), 0)
        self.assertEqual(research.read_state(self.state_path)["max_depth"], 3)

        small_state_path = Path(str(self.state_path) + ".small")
        warning = StringIO()
        with redirect_stdout(StringIO()), redirect_stderr(warning):
            self.assertEqual(research.main([
                "init", "Smaller depth", "--state", str(small_state_path), "--max-depth", "2",
            ]), 0)
        self.assertTrue(small_state_path.exists())
        self.assertIn("警告：--max-depth=2 小于 3", warning.getvalue())

    def test_legacy_automatic_status_and_fields_are_removed_on_read(self):
        self.create_state()
        stored = json.loads(self.state_path.read_text(encoding="utf-8"))
        stored["nodes"][0]["status"] = "insufficient"
        stored["nodes"][0]["eligible"] = 0
        stored["nodes"][0]["hosts_eligible"] = 0
        stored["nodes"][0]["evidence_ids"] = ["i1"]
        stored["items"] = [{
            "id": "i1", "url": "https://example.com/empty", "canonical": "https://example.com/empty",
            "title": "Empty result", "body": "", "body_chars": 0, "quality": 0.0,
            "noise_ratio": 0.0, "quality_reason": "short", "low_quality": True, "eligible": False,
        }]
        self.state_path.write_text(json.dumps(stored), encoding="utf-8")

        state = research.read_state(self.state_path)

        self.assertEqual(state["nodes"][0]["status"], "awaiting_review")
        self.assertFalse({"eligible", "hosts_eligible"} & state["nodes"][0].keys())
        self.assertFalse({"quality", "noise_ratio", "quality_reason", "low_quality", "eligible"} & state["items"][0].keys())

    def test_more_than_three_children_is_rejected(self):
        self.create_state()
        with self.mock_collectors(lambda *_: {"results": [], "errors": {}}):
            research.run_nodes(self.state_path, ["n0"])
        children = [
            {"question": f"Independent subquestion {index}", "scope_delta": f"Scope {index}"}
            for index in range(4)
        ]
        with self.assertRaisesRegex(ValueError, "more than 3"):
            research.apply_reviews(self.state_path, {
                "node_id": "n0", "verdict": "partial", "children": children,
            })
        state = research.read_state(self.state_path)
        self.assertEqual(len(state["nodes"]), 1)
        self.assertEqual(state["nodes"][0]["status"], "awaiting_review")

    def test_ancestor_duplicate_question_is_rejected(self):
        self.create_state("Which lineup is strongest in the current version?")
        with self.mock_collectors(lambda *_: {"results": [], "errors": {}}):
            research.run_nodes(self.state_path, ["n0"])
        with self.assertRaisesRegex(ValueError, "与祖先重复"):
            research.apply_reviews(self.state_path, {
                "node_id": "n0",
                "verdict": "partial",
                "children": [{
                    "question": "Which lineup is strongest in the current version",
                    "scope_delta": "Rephrased only.",
                }],
            })
        state = research.read_state(self.state_path)
        self.assertEqual(len(state["nodes"]), 1)
        self.assertEqual(state["nodes"][0]["status"], "awaiting_review")

    def test_browser_lock_contention_still_leaves_node_for_review(self):
        self.create_state(platforms=("xhs",), channels=("browser",))
        original_acquire = research.FileLock.acquire

        def simulate_contention(lock, timeout=None, poll=0.05):
            if lock.path == research._browser_lock_path(self.state_path):
                return False
            return original_acquire(lock, timeout=timeout, poll=poll)

        with patch.object(research, "BROWSER_LOCK_TIMEOUT", 0.01), \
                patch.object(research.FileLock, "acquire", simulate_contention), \
                patch.object(research.subprocess, "run") as child:
            state = research.run_nodes(self.state_path, ["n0"], timeout=1)

        child.assert_called_once()
        self.assertEqual(Path(child.call_args.args[0][1]).name, "search_dual.py")
        self.assertEqual(state["nodes"][0]["status"], "awaiting_review")
        self.assertNotEqual(state["nodes"][0]["status"], "not_found")
        self.assertEqual(state["nodes"][0]["queries"], [])
        self.assertTrue(state["nodes"][0]["zero_result_retry"])

    def test_sequential_apply_reviews_preserve_both_nodes(self):
        self.create_state()
        with self.mock_collectors(lambda *_: {"results": [], "errors": {}}):
            research.run_nodes(self.state_path, ["n0"])
        research.apply_reviews(self.state_path, {
            "node_id": "n0",
            "verdict": "partial",
            "children": [
                {"question": "Verify the first independent claim", "scope_delta": "First claim."},
                {"question": "Verify the second independent claim", "scope_delta": "Second claim."},
            ],
        })
        with self.mock_collectors(lambda *_: {"results": [], "errors": {}}):
            research.run_ready(self.state_path)
        research.apply_reviews(self.state_path, {
            "node_id": "n1", "verdict": "answered", "result": "First claim confirmed.",
        })
        state = research.apply_reviews(self.state_path, {
            "node_id": "n2", "verdict": "partial", "result": "Second claim is incomplete.",
            "unresolved": ["Need another source."],
            "early_close_reason": "No independent verification path remains.",
        }, allow_early_close=True)

        self.assertEqual(len(state["nodes"]), 3)
        self.assertEqual(state["nodes"][1]["result"], "First claim confirmed.")
        self.assertEqual(state["nodes"][1]["status"], "answered")
        self.assertEqual(state["nodes"][2]["result"], "Second claim is incomplete.")
        self.assertEqual(state["nodes"][2]["review"]["unresolved"], ["Need another source."])

    def test_report_shows_tree_statuses_and_unresolved_items(self):
        self.create_state()
        with self.mock_collectors(lambda *_: {"results": [], "errors": {}}):
            research.run_nodes(self.state_path, ["n0"])
        research.apply_reviews(self.state_path, {
            "node_id": "n0", "verdict": "partial", "unresolved": ["one missing fact"],
            "children": [{"question": "Verify an independent claim", "scope_delta": "Adds independent evidence."}],
        })
        report = research.report_text(research.read_state(self.state_path))
        self.assertIn("◐ root question（partial） | 证据 0，已抓正文 0", report)
        self.assertIn("——未解：one missing fact", report)
        self.assertIn("└─ ○ Verify an independent claim（open） | 证据 0，已抓正文 0", report)
        self.assertIn(
            "层数: 2/4（未达标）｜待拆节点: -｜可滚节点: -｜下一步: 运行 run 采集 open 节点",
            report,
        )

    def test_show_prints_node_evidence_and_json_without_state_changes(self):
        self.create_state("alpha beta")
        body = "First paragraph.\nSecond paragraph."

        def payload(script, _query, _command):
            if script == "search_dual.py":
                return {"results": [{
                    "url": "https://example.com/article", "title": "Alpha beta article",
                    "published": "2026-11-01", "body": body, "method": "http",
                    "quality": 0.75, "noise_ratio": 0.2,
                }], "errors": {}}
            return {"items": [], "errors": {}}

        with self.mock_collectors(payload):
            before = research.run_nodes(self.state_path, ["n0"], fetch=0)
        state_contents = self.state_path.read_text(encoding="utf-8")
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(research.main([
                "show", "--state", str(self.state_path), "--node", "n0", "--chars", "12",
            ]), 0)

        shown = output.getvalue()
        self.assertIn("1. Alpha beta article", shown)
        self.assertIn("域名：example.com", shown)
        self.assertIn("发布时间：2026-11-01", shown)
        self.assertIn("chars=34", shown)
        self.assertIn("method=http", shown)
        self.assertIn("版本时效：未设置版本日期", shown)
        self.assertIn("1. Alpha beta article [截断 34→12]", shown)
        self.assertIn("正文：First paragr…", shown)
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), state_contents)
        self.assertEqual(research.read_state(self.state_path)["nodes"][0]["status"], before["nodes"][0]["status"])

        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(research.main([
                "show", "--state", str(self.state_path), "--node", "n0", "--chars", "5", "--json",
            ]), 0)
        data = json.loads(output.getvalue())
        self.assertTrue(data["ok"])
        self.assertEqual(data["command"], "show")
        self.assertEqual(data["data"]["evidence"][0]["body_preview"], "First")
        self.assertTrue(data["data"]["evidence"][0]["body_truncated"])

    def test_version_dates_render_relative_age_in_prompt_show_and_report(self):
        with redirect_stdout(StringIO()):
            self.assertEqual(research.main([
                "init", "Versioned question", "--state", str(self.state_path),
                "--version-date", "2026-10-29", "--version-date", "2026-09-24",
                "--version-label", "v1.8.1",
            ]), 0)
        state = research.read_state(self.state_path)
        self.assertEqual(state["version_dates"], ["2026-09-24", "2026-10-29"])
        self.set_prompt_data([
            {
                "id": "i1", "url": "https://example.com/old", "title": "Old version evidence",
                "published": "2026-10-17", "body": "Evidence body.", "method": "http",
                "node_ids": ["n0"],
            },
            {
                "id": "i2", "url": "https://example.com/undated", "title": "Undated evidence",
                "published": None, "body": "Undated body.", "method": "http",
                "node_ids": ["n0"],
            },
        ])
        state = research.read_state(self.state_path)

        prompt = research.prompt_text(state, "n0")
        shown = research.show_text(research.show_data(state, "n0"))
        report = research.report_text(state)
        for rendered in (prompt, shown, report):
            self.assertIn("早于版本 12 天", rendered)
            self.assertIn("晚于版本 23 天", rendered)
            self.assertIn("无日期", rendered)
            self.assertIn("v1.8.1", rendered)

    def test_prompt_renders_node_and_evidence_in_task_brief(self):
        self.create_state()
        self.set_prompt_data([
            {
                "id": "i1", "url": "https://example.com/one", "title": "First source",
                "published": "2026-11-01", "body": "First source body.", "method": "http",
                "node_ids": ["n0"],
            },
            {
                "id": "i2", "url": "https://other.example/two", "title": "Second source",
                "published": None, "body": "Second source body.", "method": "browser",
                "node_ids": ["n0"],
            },
        ])
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(research.main([
                "prompt", "--state", str(self.state_path), "--node", "n0",
            ]), 0)

        rendered = output.getvalue()
        self.assertIn("## 问题\nHow does the example work?", rendered)
        self.assertIn("## 背景\nCompare the documented behavior.", rendered)
        self.assertIn("- Use direct evidence\n- Identify limitations", rendered)
        self.assertIn("## 证据（共 2 条，正文优先、标题相关度其次）", rendered)
        self.assertIn("[E1] Second source [正文 19 字]", rendered)
        self.assertIn(
            "域名：example.com｜时间：2026-11-01｜版本时效：未设置版本日期｜字数：18｜抓取方式：http",
            rendered,
        )
        self.assertIn("[E2] First source [正文 18 字]", rendered)
        self.assertIn('"node_id": "n0"', rendered)
        self.assertIn('"verdict": "answered | partial | not_found"', rendered)
        self.assertIn("## 输出要求（只输出 JSON，不要解释）", rendered)

    def test_prompt_truncates_body_at_requested_character_count(self):
        self.create_state()
        self.set_prompt_data([{
            "id": "i1", "url": "https://example.com/long", "title": "Long source",
            "body": "x" * 100, "method": "http", "node_ids": ["n0"],
        }])
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(research.main([
                "prompt", "--state", str(self.state_path), "--node", "n0", "--chars", "50",
            ]), 0)

        self.assertIn("x" * 50 + "…（已截断，全文 100 字）", output.getvalue())
        self.assertIn("[E1] Long source [截断 100→50]", output.getvalue())

    def test_display_sorts_body_first_and_marks_body_status(self):
        self.create_state()
        self.set_prompt_data([
            {
                "id": "i1", "url": "https://example.com/empty", "title": "How does example",
                "body": "", "node_ids": ["n0"],
            },
            {
                "id": "i2", "url": "https://example.com/short", "title": "How example",
                "body": "xy", "node_ids": ["n0"],
            },
            {
                "id": "i3", "url": "https://example.com/first", "title": "How does example",
                "body": "123456", "node_ids": ["n0"],
            },
            {
                "id": "i4", "url": "https://example.com/second", "title": "How does example",
                "body": "abc", "node_ids": ["n0"],
            },
        ])
        before = self.state_path.read_text(encoding="utf-8")

        data = research.show_data(research.read_state(self.state_path), "n0", chars=2)
        brief = research.prompt_text(research.read_state(self.state_path), "n0", chars=2)
        shown = research.show_text(data)

        self.assertEqual([item["id"] for item in data["evidence"]], ["i3", "i4", "i2", "i1"])
        self.assertIn("[E1] How does example [截断 6→2]", brief)
        self.assertIn("[E2] How does example [截断 3→2]", brief)
        self.assertIn("[E3] How example [正文 2 字]", brief)
        self.assertIn("[E4] How does example [无正文]", brief)
        self.assertIn("1. How does example [截断 6→2]", shown)
        self.assertIn("4. How does example [无正文]", shown)
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), before)

    def test_prompt_without_evidence_instructs_not_found(self):
        self.create_state("Question without evidence")
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(research.main([
                "prompt", "--state", str(self.state_path), "--node", "n0",
            ]), 0)

        self.assertIn("## 证据（共 0 条，正文优先、标题相关度其次）", output.getvalue())
        self.assertIn("（无证据，先报告 not_found 并给出 1-3 个可执行的子问题）", output.getvalue())

    def test_prompt_out_file_matches_stdout_rendering(self):
        self.create_state()
        self.set_prompt_data([{
            "id": "i1", "url": "https://example.com/one", "title": "First source",
            "body": "First source body.", "method": "http", "node_ids": ["n0"],
        }])
        stdout = StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(research.main([
                "prompt", "--state", str(self.state_path), "--node", "n0",
            ]), 0)

        out_path = self.state_path.with_name(self.state_path.stem + "_prompt.md")
        try:
            with redirect_stdout(StringIO()):
                self.assertEqual(research.main([
                    "prompt", "--state", str(self.state_path), "--node", "n0", "--out", str(out_path),
                ]), 0)
            self.assertEqual(out_path.read_text(encoding="utf-8"), stdout.getvalue())
        finally:
            out_path.unlink(missing_ok=True)

    def test_emit_tasks_isolates_node_evidence_and_suggests_bounded_concurrency(self):
        self.create_state("Root task")
        state = research.read_state(self.state_path)
        state["nodes"][0]["evidence_ids"] = ["i1"]
        child = dict(state["nodes"][0])
        child.update({
            "id": "n1", "parent": "n0", "question": "Child task", "depth": 1,
            "status": "open", "evidence_ids": ["i2"], "queries": [], "counter_queries": [],
        })
        closed = dict(child)
        closed.update({"id": "n2", "parent": "n0", "question": "Closed task", "status": "answered"})
        other_open = []
        for node_id in ("n3", "n4", "n5"):
            row = dict(child)
            row.update({"id": node_id, "question": f"Task {node_id}", "evidence_ids": []})
            other_open.append(row)
        state["nodes"].extend([child, closed, *other_open])
        state["items"] = [
            {"id": "i1", "url": "https://root.example/source", "title": "Root only evidence", "body": "root body", "node_ids": ["n0"]},
            {"id": "i2", "url": "https://child.example/source", "title": "Child only evidence", "body": "child body", "node_ids": ["n1"]},
        ]
        self.write_state(state)
        out_dir = self.state_path.with_suffix(".tasks")
        output = StringIO()
        try:
            with redirect_stdout(output):
                self.assertEqual(research.main([
                    "emit-tasks", "--state", str(self.state_path), "--out", str(out_dir),
                ]), 0)
            emitted = json.loads(output.getvalue())
            self.assertEqual([task["node_id"] for task in emitted["tasks"]], ["n0", "n1", "n3", "n4", "n5"])
            self.assertEqual([task["evidence_count"] for task in emitted["tasks"][:2]], [1, 1])
            self.assertEqual(emitted["suggested_concurrency"], 4)
            root_task = (out_dir / "n0.md").read_text(encoding="utf-8")
            child_task = (out_dir / "n1.md").read_text(encoding="utf-8")
            self.assertIn("Parent: (root)\nDepth: 0", root_task)
            self.assertIn("E1 → i1", root_task)
            self.assertIn("Root only evidence", root_task)
            self.assertNotIn("Child only evidence", root_task)
            self.assertIn("Parent: n0\nDepth: 1", child_task)
            self.assertIn("E1 → i2", child_task)
            self.assertIn("Child only evidence", child_task)
            self.assertNotIn("Root only evidence", child_task)
        finally:
            for path in out_dir.glob("*") if out_dir.exists() else []:
                path.unlink(missing_ok=True)
            out_dir.rmdir() if out_dir.exists() else None

    def test_emit_task_writes_self_contained_collect_only_document(self):
        self.create_state("Root task")
        state = research.read_state(self.state_path)
        state["nodes"][0]["question"] = "What does the official document establish?"
        state["nodes"][0]["context"] = "Compare the current documented behavior."
        state["nodes"][0]["acceptance"] = ["Use direct evidence", "State limitations"]
        state["nodes"][0]["evidence_ids"] = ["i1"]
        state["items"] = [{
            "id": "i1", "url": "https://example.com/source", "title": "Official document",
            "body": "The official document gives the behavior.", "method": "http", "node_ids": ["n0"],
        }]
        self.write_state(state)
        out_file = Path(str(self.state_path) + ".single-task.md")
        original = self.state_path.read_bytes()
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(research.main([
                "emit-task", "--state", str(self.state_path), "--node", "n0", "--out", str(out_file),
            ]), 0)
        emitted = json.loads(output.getvalue())
        document = out_file.read_text(encoding="utf-8")
        self.assertEqual(emitted["mode"], "collect_only")
        self.assertEqual(emitted["node_id"], "n0")
        self.assertEqual(emitted["state_path"], str(self.state_path.resolve()))
        self.assertIn(str(self.state_path.resolve()), document)
        self.assertIn(emitted["task_id"], document)
        self.assertIn("node_id: n0", document)
        self.assertIn("What does the official document establish?", document)
        self.assertIn("Compare the current documented behavior.", document)
        self.assertIn("Use direct evidence", document)
        self.assertIn("run --state", document)
        self.assertIn("show --state", document)
        self.assertIn("report --state", document)
        self.assertIn("Forbidden: `apply-review`, `reset`, `claim`, `init`, `set-node`", document)
        self.assertIn("Only cite the [E#] identifiers listed below", document)
        self.assertIn("never treat a title as a conclusion", document)
        self.assertIn('"queries_run": []', document)
        self.assertIn('"sources": [{"title": "", "url": "", "body_chars": 0, "method": ""}]', document)
        self.assertIn('"bodies": 0', document)
        self.assertIn('"diagnostics": []', document)
        self.assertNotIn('"verdict"', document)
        self.assertIn(f"OPENCLI_LOCK_PATH={self.state_path.resolve()}.browser.lock", document)
        self.assertEqual(self.state_path.read_bytes(), original)
        with self.assertRaisesRegex(ValueError, "must not overwrite the state file"):
            research.emit_task(self.state_path, "n0", self.state_path)
        self.assertEqual(self.state_path.read_bytes(), original)

    def test_task_run_records_mechanical_counts_and_replaces_duplicate_task_id(self):
        self.create_state("Root task")
        state_before = research.read_state(self.state_path)
        task_path = Path(str(self.state_path) + ".return.json")
        task_payload = {
            "task_id": "task-123", "node_id": "n0", "queries_run": ["q1"],
            "sources": [
                {"title": "A", "url": "https://a.example", "body_chars": 2652, "method": "http"},
                {"title": "B", "url": "https://b.example", "body_chars": 0, "method": "search"},
                {"title": "C", "url": "https://c.example", "body_chars": 0, "method": "browser"},
            ],
            "bodies": 1, "diagnostics": ["one page was unavailable"],
        }
        task_path.write_text(json.dumps(task_payload), encoding="utf-8")
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(research.main([
                "task-run", "--state", str(self.state_path), "--task", str(task_path),
                "--worker", "agent-a", "--note", "Collected one usable body.",
            ]), 0)
        record = json.loads(output.getvalue())
        self.assertEqual(record["source_count"], 3)
        self.assertEqual(record["body_count"], 1)
        self.assertEqual(record["body_chars_total"], 2652)
        self.assertEqual(record["diagnostics"], ["one page was unavailable"])
        state = research.read_state(self.state_path)
        self.assertEqual(state["nodes"][0]["status"], state_before["nodes"][0]["status"])
        report = research.report_text(state)
        self.assertIn("返回=returned | 来源=3 | 正文=1/3 | 字数=2652 | 备注=Collected one usable body.", report)
        self.assertIn("层数: 1/4（未达标）", report)

        task_payload["sources"] = [{"title": "Updated", "url": "https://updated.example", "body_chars": 7, "method": "http"}]
        task_payload["diagnostics"] = []
        task_path.write_text(json.dumps(task_payload), encoding="utf-8")
        with redirect_stdout(StringIO()):
            self.assertEqual(research.main([
                "task-run", "--state", str(self.state_path), "--task", str(task_path), "--note", "Updated return.",
            ]), 0)
        state = research.read_state(self.state_path)
        self.assertEqual(len(state["task_runs"]), 1)
        self.assertEqual(state["task_runs"][0]["source_count"], 1)
        self.assertEqual(state["task_runs"][0]["body_count"], 1)
        self.assertEqual(state["task_runs"][0]["body_chars_total"], 7)
        self.assertIn("来源=1 | 正文=1/1 | 字数=7 | 备注=Updated return.", research.report_text(state))

    def test_node_queries_and_channels_override_defaults_and_tag_counter_sources(self):
        with redirect_stdout(StringIO()):
            self.assertEqual(research.main([
                "init", "Automatic question", "--state", str(self.state_path),
                "--platforms", "web", "--channels", "github",
            ]), 0)
        with redirect_stdout(StringIO()):
            self.assertEqual(research.main([
                "set-node", "--state", str(self.state_path), "--node", "n0",
                "--channels", "web,rss", "--queries", "support A", "support B",
                "--counter-queries", "counter A",
            ]), 0)
        calls = []

        def fake_run(command, **_kwargs):
            script = Path(command[1]).name
            query = command[2]
            calls.append((script, query, command))
            row = {"url": "https://example.com/shared", "title": "Shared result"}
            if script == "search_dual.py":
                return completed({"results": [row], "errors": {}})
            if script == "collect.py":
                return completed({"items": [row], "errors": {}})
            self.fail(f"unexpected subprocess: {script}")

        with patch.object(research.subprocess, "run", side_effect=fake_run):
            state = research.run_nodes(self.state_path, ["n0"], fetch=0)

        expected_queries = ["support A", "support B", "Automatic question", "counter A"]
        self.assertEqual([query for _script, query, _command in calls], [q for q in expected_queries for _ in range(2)])
        self.assertEqual(
            [(Path(command[1]).name, command[command.index("--platforms") + 1])
             for _script, _query, command in calls if "--platforms" in command],
            [("collect.py", "rss")] * len(expected_queries),
        )
        item = state["items"][0]
        self.assertEqual(item["node_refs"], [
            {"node_id": "n0", "query_kind": "support"},
            {"node_id": "n0", "query_kind": "counter"},
        ])
        self.assertEqual(state["nodes"][0]["queries"], ["support A", "support B"])

    def test_empty_node_overrides_fall_back_to_global_channels_and_question(self):
        self.create_state("Fallback question", platforms=("web",), channels=("rss",))
        research.set_node(self.state_path, "n0", channels=(), queries=[], counter_queries=[])
        calls = []

        def fake_run(command, **_kwargs):
            calls.append(command)
            return completed({"items": [], "errors": {}})

        with patch.object(research.subprocess, "run", side_effect=fake_run):
            state = research.run_nodes(self.state_path, ["n0"], fetch=0)

        self.assertEqual(len(calls), 2)
        self.assertEqual(Path(calls[0][1]).name, "collect.py")
        self.assertEqual(calls[0][2], "Fallback question")
        self.assertEqual(calls[0][calls[0].index("--platforms") + 1], "rss")
        self.assertEqual(Path(calls[1][1]).name, "search_dual.py")
        self.assertEqual(state["nodes"][0]["status"], "awaiting_review")
        self.assertEqual(state["channels_used"], ["rss", "web"])
        self.assertFalse(state["fallback_default"])

    def test_init_defaults_to_web_and_run_reports_the_resolved_channel(self):
        with redirect_stdout(StringIO()):
            self.assertEqual(research.main([
                "init", "Default channel", "--state", str(self.state_path), "--platforms", "xhs", "--json",
            ]), 0)
        state = research.read_state(self.state_path)
        self.assertEqual(state["channels"], ["web"])
        self.assertEqual(state["nodes"][0]["channels"], [])

        def fake_run(command, **_kwargs):
            self.assertEqual(Path(command[1]).name, "search_dual.py")
            return completed({"results": [{
                "url": "https://example.com/default", "title": "Default channel result",
                "body": "Evidence body.", "method": "http",
            }], "errors": {}})

        output = StringIO()
        with patch.object(research.subprocess, "run", side_effect=fake_run), redirect_stdout(output):
            self.assertEqual(research.main([
                "run", "--state", str(self.state_path), "--ready", "--fetch", "3", "--json",
            ]), 0)
        envelope = json.loads(output.getvalue())
        self.assertTrue(envelope["ok"])
        self.assertEqual(envelope["data"]["channels_used"], ["web"])
        self.assertEqual(envelope["data"]["channels_by_node"], {"n0": ["web"]})
        self.assertFalse(envelope["data"]["fallback_default"])
        self.assertEqual(envelope["data"]["errors"], [])
        self.assertEqual(envelope["data"]["nodes"][0]["status"], "awaiting_review")

    def test_empty_channels_fall_back_to_web_and_collection_errors_are_reported(self):
        self.create_state("Fallback to web", channels=("rss",))
        state = research.read_state(self.state_path)
        state["channels"] = []
        state["platforms"] = []
        self.write_state(state)

        def failed_web_search(command, **_kwargs):
            self.assertEqual(Path(command[1]).name, "search_dual.py")
            return completed({"results": [], "errors": {"provider": "offline"}})

        output = StringIO()
        with patch.object(research.subprocess, "run", side_effect=failed_web_search), redirect_stdout(output):
            self.assertEqual(research.main([
                "run", "--state", str(self.state_path), "--node", "n0", "--fetch", "0", "--json",
            ]), 4)
        envelope = json.loads(output.getvalue())
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error"]["channels_used"], ["web"])
        self.assertTrue(envelope["error"]["fallback_default"])
        self.assertTrue(any("offline" in error for error in envelope["error"]["errors"]))
        self.assertEqual(research.read_state(self.state_path)["nodes"][0]["status"], "awaiting_review")

        rows, issued, errors = research._collect_node(
            {"question": "No channels"}, self.state_path, (), research.time.monotonic() + 1,
        )
        self.assertEqual(rows, [])
        self.assertFalse(issued)
        self.assertIn("no collection channels configured", errors[0])

    def test_review_citations_extract_validate_and_report_uncited_evidence(self):
        self.prepare_evidence_state()
        state = research.apply_reviews(self.state_path, {
            "node_id": "n0", "verdict": "partial", "result": "Supported by [E1].",
            "citations": ["E1"],
            "children": [{
                "question": "Check the follow-up [E999] independently",
                "scope_delta": "This follows from [E1].",
            }],
        })
        self.assertEqual(state["nodes"][0]["review"]["citations"], ["E1"])
        self.assertEqual(state["uncited"], [{"node_id": "n0", "refs": ["E2"]}])
        self.assertEqual(research.read_state(self.state_path)["nodes"][0]["status"], "partial")

    def test_review_rejects_missing_and_cross_node_citations_without_writing(self):
        self.prepare_evidence_state(("Root source one",))
        with self.assertRaisesRegex(ValueError, 'bad_refs:.*"ref": "E99".*"reason": "missing"'):
            research.apply_reviews(self.state_path, {
                "node_id": "n0", "verdict": "answered", "citations": ["E99"],
            })
        with self.assertRaisesRegex(ValueError, 'bad_refs:.*"ref": "E3".*"reason": "belongs_to_other_node"'):
            research.apply_reviews(self.state_path, {
                "node_id": "n0", "verdict": "answered", "rationale": "Check [E3].",
            })
        state = research.read_state(self.state_path)
        self.assertEqual(state["nodes"][0]["status"], "awaiting_review")
        self.assertEqual(state["nodes"][0]["result"], "")

    def test_legacy_review_without_citations_remains_accepted(self):
        self.prepare_evidence_state(("Only source",))
        state = research.apply_reviews(self.state_path, {
            "node_id": "n0", "verdict": "answered", "result": "Legacy review format.",
        })
        self.assertEqual(state["nodes"][0]["status"], "answered")
        self.assertEqual(state["uncited"], [{"node_id": "n0", "refs": ["E1"]}])

    def test_apply_review_json_exposes_uncited_diagnostic(self):
        self.prepare_evidence_state()
        review_path = Path(str(self.state_path) + ".review.json")
        review_path.write_text(json.dumps({
            "node_id": "n0", "verdict": "answered", "result": "No citation supplied.",
        }), encoding="utf-8")
        output = StringIO()
        try:
            with redirect_stdout(output):
                self.assertEqual(research.main([
                    "apply-review", "--state", str(self.state_path), "--file", str(review_path), "--json",
                ]), 0)
        finally:
            review_path.unlink(missing_ok=True)
        envelope = json.loads(output.getvalue())
        self.assertTrue(envelope["ok"])
        self.assertEqual(envelope["data"]["uncited"], [
            {"node_id": "n0", "refs": ["E1", "E2"]},
        ])

    def test_task_citation_map_matches_displayed_evidence_ids(self):
        self.prepare_evidence_state()
        state = research.read_state(self.state_path)
        items = {item["id"]: item for item in state["items"]}
        items["i1"]["body"] = "short"
        items["i2"]["body"] = "long body that sorts first"
        self.write_state(state)
        first_task = research.prompt_text(research.read_state(self.state_path), "n0", include_citation_map=True)
        self.assertLess(first_task.index("[E1] Root source two"), first_task.index("[E2] Root source one"))
        self.assertIn("E1 → i2", first_task)
        self.assertIn("E2 → i1", first_task)
        self.assertEqual(research._citation_map(research.read_state(self.state_path), "n0"), {
            "E1": "i2", "E2": "i1",
        })
        self.assertEqual(
            [
                (item["ref"], item["id"])
                for item in research.show_data(research.read_state(self.state_path), "n0")["evidence"]
            ],
            [("E1", "i2"), ("E2", "i1")],
        )

        state = research.read_state(self.state_path)
        items = {item["id"]: item for item in state["items"]}
        items["i1"]["body"] = "new long body sorts first now"
        items["i2"]["body"] = "tiny"
        self.write_state(state)
        second_task = research.prompt_text(research.read_state(self.state_path), "n0", include_citation_map=True)
        self.assertLess(second_task.index("[E1] Root source one"), second_task.index("[E2] Root source two"))
        self.assertIn("E1 → i1", second_task)
        self.assertIn("E2 → i2", second_task)
        self.assertEqual(research._citation_map(research.read_state(self.state_path), "n0"), {
            "E1": "i1", "E2": "i2",
        })

    def test_collector_subprocess_runs_from_invocation_directory(self):
        slot = mock.Mock()
        result = SimpleNamespace(stdout="{}", stderr="", returncode=0)
        with (
            mock.patch.object(research, "_acquire_collector_slot", return_value=slot),
            mock.patch.object(research.subprocess, "run", return_value=result) as run,
        ):
            payload, error, started = research._command_json(
                ["collector"], self.state_path, research.time.monotonic() + 10,
            )

        self.assertEqual(payload, {})
        self.assertIsNone(error)
        self.assertTrue(started)
        self.assertEqual(run.call_args.kwargs["cwd"], Path.cwd())

    def test_fetch_subprocess_runs_from_invocation_directory(self):
        result = SimpleNamespace(stdout='{"text":"body"}', stderr="", returncode=0)
        with (
            mock.patch.dict(os.environ, {"OPEN_NOTEBOOK_URL": ""}),
            mock.patch.object(research, "_dotenv_value", return_value=""),
            mock.patch.object(research.subprocess, "run", return_value=result) as run,
        ):
            payload, error = research._fetch_payload("https://example.com/article")

        self.assertEqual(payload["text"], "body")
        self.assertIsNone(error)
        self.assertEqual(run.call_args.kwargs["cwd"], Path.cwd())

    def test_claim_add_list_delete_validate_and_report_ledger(self):
        self.prepare_evidence_state()
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(research.main([
                "claim", "add", "--state", str(self.state_path), "--node", "n0",
                "--text", "The recorded conclusion.", "--evidence", "E1",
                "--counter", "E2", "--version", "v1.8.1", "--as-of", "2026-11-02",
            ]), 0)
        claim = json.loads(output.getvalue())
        self.assertEqual(claim["id"], "c1")
        self.assertEqual(claim["evidence"], ["E1"])
        self.assertEqual(claim["counter_evidence"], ["E2"])
        self.assertIn("结论账本", research.report_text(research.read_state(self.state_path)))
        self.assertIn("版本 v1.8.1 | 日期 2026-11-02", research.report_text(research.read_state(self.state_path)))
        shown = research.show_data(research.read_state(self.state_path), "n0")
        self.assertEqual(shown["claims"][0]["text"], "The recorded conclusion.")
        with self.assertRaisesRegex(ValueError, 'bad_refs:.*"ref": "E9"'):
            research.add_claim(self.state_path, "n0", "Invalid", ["E9"])

        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(research.main(["claims", "--state", str(self.state_path), "--json"]), 0)
        self.assertEqual(json.loads(output.getvalue())["data"][0]["id"], "c1")
        with redirect_stdout(StringIO()):
            self.assertEqual(research.main([
                "claim", "delete", "--state", str(self.state_path), "--id", "c1",
            ]), 0)
        self.assertEqual(research.claims_data(research.read_state(self.state_path)), [])
        second = research.add_claim(self.state_path, "n0", "A later conclusion.", ["E1"])
        self.assertEqual(second["claims"][0]["id"], "c2")
        research.remove_claim(self.state_path, "c2")
        self.assertIn(
            "层数: 2/4（未达标）｜待拆节点: -｜可滚节点: -｜下一步: 先处理 awaiting_review 节点",
            research.report_text(research.read_state(self.state_path)),
        )

    def test_purpose_deliverable_render_in_prompt_emit_task_and_digest(self):
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(research.main([
                "init", "Research question", "--state", str(self.state_path),
                "--purpose", "Compare two strategies", "--deliverable", "A sourced comparison",
            ]), 0)
        state = research.read_state(self.state_path)
        self.assertEqual(state["purpose"], "Compare two strategies")
        self.assertEqual(state["deliverable"], "A sourced comparison")
        prompt = research.prompt_text(state, "n0")
        self.assertIn("## 调研目的\nCompare two strategies", prompt)
        self.assertIn("## 交付物要求\nA sourced comparison", prompt)

        task_path = Path(str(self.state_path) + ".task.md")
        with redirect_stdout(StringIO()):
            self.assertEqual(research.main([
                "emit-task", "--state", str(self.state_path), "--node", "n0", "--out", str(task_path),
            ]), 0)
        task = task_path.read_text(encoding="utf-8")
        self.assertIn("## 调研目的\nCompare two strategies", task)
        self.assertIn("## 交付物要求\nA sourced comparison", task)

        research.record_gaps(
            self.state_path, [], no_gaps_reason="已检查当前研究范围，作为摘要渲染测试没有待补事实。",
            reviewed_nodes=["n0"],
        )
        digest_path = Path(str(self.state_path) + ".digest.md")
        with redirect_stdout(StringIO()):
            self.assertEqual(research.main([
                "digest", "--state", str(self.state_path), "--out", str(digest_path),
            ]), 0)
        digest = digest_path.read_text(encoding="utf-8")
        self.assertIn("- 调研目的：Compare two strategies", digest)
        self.assertIn("- 交付物要求：A sourced comparison", digest)

    def test_review_gaps_validate_and_do_not_change_answered_verdict(self):
        self.prepare_evidence_state()
        research.add_claim(self.state_path, "n0", "Existing claim", ["E1"])
        invalid_gaps = [
            [{"id": "g1", "type": "unknown", "why": "Reason", "target": "E1", "questions": ["Question"]}],
            [{"id": "g1", "type": "depth", "why": "Reason", "target": "E1", "questions": []}],
            [{"id": "g1", "type": "depth", "why": "Reason", "target": "E99", "questions": ["Question"]}],
            [{"id": "g1", "type": "depth", "why": "Reason", "target": "c99", "questions": ["Question"]}],
            [{"id": "bad", "type": "depth", "why": "Reason", "target": "E1", "questions": ["Question"]}],
            [{"id": "g1", "type": "depth", "why": " ", "target": "E1", "questions": ["Question"]}],
            [
                {"id": "g1", "type": "depth", "why": "Reason", "target": "E1", "questions": ["Question"]},
                {"id": "g1", "type": "quant", "why": "Reason", "target": "E1", "questions": ["Question"]},
            ],
        ]
        for gaps in invalid_gaps:
            with self.assertRaises(ValueError):
                research.apply_reviews(self.state_path, {
                    "node_id": "n0", "verdict": "answered", "result": "Answer", "gaps": gaps,
                })
            self.assertEqual(research.read_state(self.state_path)["nodes"][0]["status"], "awaiting_review")

        state = research.apply_reviews(self.state_path, {
            "node_id": "n0", "verdict": "answered", "result": "Answer", "citations": ["E1"],
            "gaps": [{
                "id": "g1", "type": "depth", "why": "Mechanism still needs detail",
                "target": "E1", "questions": ["Which step causes the effect?"],
            }, {
                "id": "g2", "type": "quant", "why": "Claim still needs a metric",
                "target": "c1", "questions": ["What measured rate supports the claim?"],
            }],
        })
        self.assertEqual(state["nodes"][0]["status"], "answered")
        self.assertEqual([gap["id"] for gap in state["nodes"][0]["review"]["gaps"]], ["g1", "g2"])
        shown = research.show_text(research.show_data(state, "n0"))
        self.assertIn(
            "[g1] depth｜target=E1｜why=Mechanism still needs detail｜待补：Which step causes the effect?",
            shown,
        )

    def test_claim_support_and_gap_links_validate_and_render_in_ledgers(self):
        self.prepare_evidence_state()
        research.apply_reviews(self.state_path, {
            "node_id": "n0", "verdict": "answered", "result": "Answer", "citations": ["E1"],
            "gaps": [{
                "id": "g1", "type": "evidence", "why": "Needs a second check",
                "target": "E1", "questions": ["Can another source verify it?"],
            }],
        })
        with self.assertRaisesRegex(ValueError, "unknown gap id"):
            research.add_claim(self.state_path, "n0", "Invalid gap link", ["E1"], gaps=["g99"])
        with self.assertRaisesRegex(ValueError, "--support"):
            research.add_claim(self.state_path, "n0", "Invalid support", ["E1"], support="many")

        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(research.main([
                "claim", "add", "--state", str(self.state_path), "--node", "n0",
                "--text", "A manually assessed claim", "--evidence", "E1",
                "--support", "single", "--gaps", "g1",
            ]), 0)
        claim = json.loads(output.getvalue())
        self.assertEqual(claim["support"], "single")
        self.assertEqual(claim["gaps"], ["g1"])
        ledger = research.claims_text(research.claims_data(research.read_state(self.state_path)))
        self.assertIn("支撑=single｜关联缺口=g1", ledger)
        self.assertIn("支撑=single｜关联缺口=g1", research.report_text(research.read_state(self.state_path)))

    def test_digest_has_seven_sections_placeholders_and_counts(self):
        self.prepare_evidence_state()
        research.apply_reviews(self.state_path, {
            "node_id": "n0", "verdict": "answered", "result": "Answer", "citations": ["E1"],
            "gaps": [
                {"id": "g1", "type": "quant", "why": "Missing win rate", "target": "E1", "questions": ["What is the measured rate?"]},
                {"id": "g2", "type": "conflict", "why": "Sources disagree", "target": "E2", "questions": ["Which source is current?"]},
            ],
        })
        state = research.read_state(self.state_path)
        child = state["nodes"][1]
        child["status"] = "not_found"
        child["review"]["unresolved"] = ["Need another source"]
        self.write_state(state)
        research.add_claim(
            self.state_path, "n0", "Contested by the available sources", ["E1"],
            support="contested", gaps=["g1", "g2"],
        )
        digest = research.digest_text(research.read_state(self.state_path))
        for section in (
            "## 1. 调研目的与交付物要求", "## 2. 结论清单", "## 3. 分项细节骨架",
            "## 4. 量化数据", "## 5. 矛盾与不确定", "## 6. 未解决", "## 7. 来源清单",
        ):
            self.assertIn(section, digest)
        for placeholder in ("- 结论：", "- 证据引用：", "- 未解决：", "- 版本/日期："):
            self.assertIn(placeholder, digest)
        self.assertIn("相关 claim：c1", digest)
        self.assertIn("contested claim：", digest)
        self.assertIn("Need another source", digest)
        self.assertIn("https://example.com/i1", digest)
        self.assertRegex(
            digest,
            r"节点: 2｜证据: 5｜正文: 5｜结论: 1\n"
            r"层数: 2/4（未达标）｜待拆节点: n1\(第2层,not_found\)｜可滚节点: -｜下一步: "
            r"对最浅的 partial/not_found 节点运行 prompt 补 children$",
        )

    def test_spec_json_describes_runtime_commands_and_shared_gap_enum(self):
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(research.main(["spec", "--json"]), 0)
        envelope = json.loads(output.getvalue())
        self.assertEqual(envelope["command"], "spec")
        contract = envelope["data"]
        self.assertIn("commands", contract)
        self.assertIn("review_schema", contract)
        self.assertIn("exit_codes", contract)
        self.assertIn("hn", contract["collection"]["channels"])
        self.assertIn("web", contract["collection"]["channels"])
        self.assertEqual(contract["collection"]["default_channels"], ["web"])
        self.assertTrue(contract["commands"]["doctor"]["supports_json"])
        init_channels = next(
            argument for argument in contract["commands"]["init"]["arguments"]
            if argument["name"] == "--channels"
        )
        self.assertEqual(init_channels["default"], "web")
        self.assertEqual(
            contract["review_schema"]["gaps"]["fields"]["type"]["enum"],
            sorted(research.GAP_TYPES),
        )
        self.assertTrue(contract["commands"]["run"]["supports_json"])
        self.assertEqual(contract["commands"]["run"]["required_groups"], [["--node", "--ready"]])
        self.assertIn("status", contract["commands"])
        self.assertEqual(contract["collection"]["channel_table"]["browser"]["login_required"], True)
        self.assertEqual(contract["collection"]["channel_table"]["web"]["default_timeout_seconds"], 24)

        state = self.create_state()
        state["claims"] = [{"id": "c1"}]
        with patch.object(research, "GAP_TYPES", {"runtime_type"}):
            live_contract = research.contract_data()
            validated = research._validate_review_gaps(state, "n0", [{
                "id": "g1", "type": "runtime_type", "why": "A reason",
                "target": "c1", "questions": ["What should be checked?"],
            }])
        self.assertEqual(
            live_contract["review_schema"]["gaps"]["fields"]["type"]["enum"], ["runtime_type"],
        )
        self.assertEqual(validated[0]["type"], "runtime_type")

    def test_spec_channel_table_has_scope_login_dependency_and_timeout_for_each_channel(self):
        channel_table = research.contract_data()["collection"]["channel_table"]
        self.assertEqual(set(channel_table), research.RESEARCH_CHANNELS)
        for channel, details in channel_table.items():
            with self.subTest(channel=channel):
                self.assertTrue(details["applies_to"])
                self.assertIsInstance(details["login_required"], bool)
                self.assertGreater(details["default_timeout_seconds"], 0)
                self.assertTrue(details["timeout_scope"])
        self.assertTrue(channel_table["browser"]["login_required"])
        self.assertFalse(channel_table["web"]["login_required"])

    def test_doctor_json_reports_check_contract_without_environment_values(self):
        output = StringIO()
        env = {
            "TAVILY_API_KEY": "doctor-secret-tavily",
            "GROQ_API_KEY": "",
            "AGNES_API_KEY": "doctor-secret-agnes",
            "OPEN_NOTEBOOK_URL": "",
        }
        with patch.dict(os.environ, env), patch.object(
            research.subprocess, "run", return_value=completed({}),
        ), patch.object(research, "_dotenv_value", return_value=""), patch.object(
            research.requests, "get"
        ) as notebook_get, redirect_stdout(output):
            result = research.main(["doctor", "--json"])

        payload = json.loads(output.getvalue())
        self.assertIn(result, (0, 1, 6))
        self.assertTrue({"ok", "checks", "needs_user", "requirements", "config"}.issubset(set(payload)))
        self.assertIsInstance(payload["ok"], bool)
        names = {check["name"] for check in payload["checks"]}
        self.assertTrue({
            "python_version", "script_directory", "state_directory_writable", "fetch_smart.py",
            "collect.py", "collect_browser.py", "TAVILY_API_KEY", "GROQ_API_KEY",
            "AGNES_API_KEY", "search_provider", "opencli_path", "opencli_help", "OPENCLI_LOCK_PATH",
            "chrome_running", "opencli_session", "OPEN_NOTEBOOK_URL",
        }.issubset(names))
        notebook_check = next(check for check in payload["checks"] if check["name"] == "OPEN_NOTEBOOK_URL")
        self.assertEqual((notebook_check["status"], notebook_check["detail"]), ("ok", "unset"))
        notebook_get.assert_not_called()
        for check in payload["checks"]:
            self.assertEqual(set(check), {"name", "status", "detail", "hint"})
            self.assertIn(check["status"], {"ok", "warn", "fail"})
        self.assertNotIn("doctor-secret-tavily", output.getvalue())
        self.assertNotIn("doctor-secret-agnes", output.getvalue())
        self.assertEqual(
            next(check for check in payload["checks"] if check["name"] == "TAVILY_API_KEY")["detail"],
            "set",
        )

    def test_doctor_notebook_probe_failure_is_warn_and_does_not_fail_doctor(self):
        env = {
            "OPEN_NOTEBOOK_URL": "http://notebook.test",
            "TAVILY_API_KEY": "set",
            "GROQ_API_KEY": "set",
            "AGNES_API_KEY": "set",
        }
        with patch.dict(os.environ, env), patch.object(
            research, "_dotenv_value", return_value="",
        ), patch.object(
            research, "_doctor_opencli_command", return_value=[research.sys.executable],
        ), patch.object(
            research, "login_session_checks", return_value=[
                research._doctor_check("chrome_running", "ok", "present", "No action needed."),
                research._doctor_check("opencli_session", "ok", "ready", "No action needed."),
            ],
        ), patch.object(
            research.subprocess, "run", return_value=completed({}),
        ), patch(
            "fetch_smart._acquire_notebook_slot", return_value=SimpleNamespace(release=lambda: None),
        ), patch.object(
            research.requests, "get", side_effect=research.requests.exceptions.ConnectionError("offline"),
        ) as notebook_get:
            data = research.doctor_data()

        self.assertTrue(data["ok"])
        notebook_check = next(check for check in data["checks"] if check["name"] == "OPEN_NOTEBOOK_URL")
        self.assertEqual(notebook_check["status"], "warn")
        self.assertIn("ConnectionError", notebook_check["detail"])
        notebook_get.assert_called_once_with("http://notebook.test/api/notebooks", timeout=5)

    def test_doctor_chrome_process_check_reports_presence_and_absence(self):
        for tasklist_output, expected_status in (
            ("Image Name                     PID\nchrome.exe                    1234", "ok"),
            ("Image Name                     PID\nnotepad.exe                    1234", "fail"),
        ):
            with self.subTest(expected_status=expected_status):
                results = [
                    SimpleNamespace(stdout=tasklist_output, stderr="", returncode=0),
                    SimpleNamespace(stdout="2", stderr="", returncode=0),
                ]
                with patch.object(research.os, "name", "nt"), patch.object(
                    research, "_doctor_opencli_command", return_value=["opencli"],
                ), patch.object(research.subprocess, "run", side_effect=results) as run:
                    checks = research.login_session_checks()

                by_name = {check["name"]: check for check in checks}
                self.assertEqual(by_name["chrome_running"]["status"], expected_status)
                self.assertEqual(by_name["opencli_session"]["status"], "ok")
                self.assertEqual(run.call_args_list[0].args[0], ["tasklist"])
                self.assertEqual(run.call_args_list[1].args[0][-4:], ["browser", "main", "eval", "1+1"])
                self.assertEqual(run.call_args_list[1].kwargs["timeout"], 15)

    def test_spec_declared_cold_start_flow_uses_evidence_review_claim_and_digest(self):
        def json_call(argv, command):
            command_spec = contract["commands"][command]
            declared_arguments = {argument["name"] for argument in command_spec["arguments"]}
            full_argv = [*argv, "--json"]
            for token in full_argv:
                if token.startswith("--"):
                    self.assertIn(token, declared_arguments, (command, token))
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(research.main(full_argv), 0, full_argv)
            envelope = json.loads(output.getvalue())
            self.assertTrue(envelope["ok"], full_argv)
            self.assertEqual(envelope["command"], command)
            return envelope["data"]

        spec_output = StringIO()
        with redirect_stdout(spec_output):
            self.assertEqual(research.main(["spec", "--json"]), 0)
        contract = json.loads(spec_output.getvalue())["data"]
        result_contract = contract["result_contract"]
        self.assertIn("state_document", result_contract)
        self.assertEqual(result_contract["digest"]["section_count"], 7)

        init_data = json_call([
            "init", "Python 3.14 release highlights", "--state", str(self.state_path),
            "--channels", "hn", "--purpose", "Identify notable changes",
        ], "init")
        self.assertTrue(set(result_contract["init"]["data_fields"]).issubset(init_data))
        node_id = init_data["root_id"]

        node_data = json_call([
            "set-node", "--state", init_data["state_path"], "--node", node_id,
        ], "set-node")
        self.assertTrue(set(result_contract["set-node"]["data_fields"]).issubset(node_data))
        self.assertEqual(node_data["status"], "open")

        def fake_hn(command, **_kwargs):
            self.assertEqual(Path(command[1]).name, "collect.py")
            self.assertEqual(command[command.index("--platforms") + 1], "hn")
            return completed({"items": [{
                "platform": "hn", "url": "https://news.ycombinator.com/item?id=314",
                "title": "Python 3.14 release notes", "body": "The release adds several language changes.",
                "method": "hn",
            }], "errors": {}})

        with patch.object(research.subprocess, "run", side_effect=fake_hn):
            run_data = json_call([
                "run", "--state", init_data["state_path"], "--ready", "--fetch", "3",
            ], "run")
        self.assertTrue(set(result_contract["run"]["data_fields"]).issubset(run_data))
        self.assertEqual(run_data["channels_used"], ["hn"])
        self.assertEqual(run_data["nodes"][0]["status"], "awaiting_review")

        show_data = json_call([
            "show", "--state", init_data["state_path"], "--node", node_id,
        ], "show")
        self.assertTrue(set(result_contract["show"]["data_fields"]).issubset(show_data))
        self.assertEqual(show_data["status"], "awaiting_review")
        self.assertEqual(show_data["evidence_count"], 1)
        self.assertIn("id", result_contract["show"]["evidence_fields"])
        self.assertIn("ref", result_contract["show"]["evidence_fields"])
        evidence_ref = contract["evidence_ref_rule"]["format"].replace("#", "1")
        self.assertEqual(len(show_data["evidence"]), 1)
        self.assertEqual(show_data["evidence"][0]["ref"], evidence_ref)
        gap_id = "g1"
        review = {
            "node_id": node_id,
            "verdict": "partial",
            "result": "The release changes are supported by the displayed evidence [E1].",
            "citations": [evidence_ref],
            "gaps": [{
                "id": gap_id, "type": "depth", "why": "Implementation detail is not established.",
                "target": evidence_ref, "questions": ["Which implementation detail changed?"],
            }],
            "children": [{
                "question": "Which implementation details changed in Python 3.14?",
                "scope_delta": "Inspect individual release-note entries.",
                "context": "Follow up the top-level release summary.",
                "acceptance": ["Cite the relevant release note"],
            }],
        }
        review_fields = contract["review_schema"]["fields"]
        self.assertTrue(set(review).issubset(review_fields))
        self.assertTrue(set(review["children"][0]).issubset(review_fields["children"]["fields"]))
        self.assertTrue(set(review["gaps"][0]).issubset(contract["review_schema"]["gaps"]["fields"]))
        review_path = Path(str(self.state_path) + ".review.json")
        review_path.write_text(json.dumps(review), encoding="utf-8")
        applied = json_call([
            "apply-review", "--state", init_data["state_path"], "--file", str(review_path),
        ], "apply-review")
        self.assertEqual(
            next(node for node in applied["nodes"] if node["id"] == node_id)["status"], "partial",
        )

        claim_arguments = {argument["name"] for argument in contract["claim_schema"]["arguments"]}
        claim_argv = [
            "claim", "add", "--state", init_data["state_path"], "--node", node_id,
            "--text", "Python 3.14 includes changes covered by the cited release summary.",
            "--evidence", evidence_ref, "--support", "single", "--gaps", gap_id,
        ]
        self.assertTrue(all(
            token in claim_arguments for token in [*claim_argv, "--json"] if token.startswith("--")
        ))
        claim = json_call(claim_argv, "claim add")
        self.assertTrue(set(result_contract["claim add"]["data_fields"]).issubset(claim))
        self.assertEqual(claim["evidence"], [evidence_ref])
        self.assertEqual(claim["gaps"], [gap_id])

        global_gap_path = Path(str(self.state_path) + ".gaps.json")
        global_gap_path.write_text(json.dumps({"gaps": [{
            "what": "The test follow-up needs a separate global gap record.",
            "why": "The review-local gap does not create a global gap round.",
            "next_question": "Confirm the global gap round can be resolved.",
        }]}), encoding="utf-8")
        global_gaps = json_call([
            "gaps", "--state", init_data["state_path"], "--file", str(global_gap_path),
        ], "gaps")
        self.assertEqual(global_gaps["gaps"][0]["id"], "g1")
        json_call([
            "gaps", "--state", init_data["state_path"], "--resolve", "g1", "--status", "dropped",
            "--note", "The synthetic follow-up is covered by this contract test.",
        ], "gaps")
        digest = json_call(["digest", "--state", init_data["state_path"]], "digest")
        self.assertTrue(set(result_contract["digest"]["data_fields"]).issubset(digest))
        self.assertEqual(
            sum(line.startswith("## ") for line in digest["text"].splitlines()),
            result_contract["digest"]["section_count"],
        )

    def test_json_success_commands_share_envelope_and_init_returns_paths(self):
        def call_json(argv, command):
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(research.main([*argv, "--json"]), 0, argv)
            envelope = json.loads(output.getvalue())
            self.assertEqual(set(envelope), {"ok", "schema_version", "command", "data"})
            self.assertTrue(envelope["ok"])
            self.assertEqual(envelope["schema_version"], research.SCHEMA_VERSION)
            self.assertEqual(envelope["command"], command)
            return envelope["data"]

        init_path = Path(str(self.state_path) + ".init")
        init_data = call_json([
            "init", "JSON workflow", "--state", str(init_path), "--platforms", "web",
        ], "init")
        self.assertEqual(init_data["state_path"], str(init_path.resolve()))
        self.assertEqual(init_data["root_id"], "n0")
        self.assertEqual(init_data["max_depth"], 3)
        self.assertEqual(init_data["min_layers"], 4)

        self.prepare_evidence_state()
        self.assertEqual(call_json([
            "set-node", "--state", str(self.state_path), "--node", "n0", "--kind", "verification",
        ], "set-node")["kind"], "verification")
        call_json(["prompt", "--state", str(self.state_path), "--node", "n0"], "prompt")
        call_json(["show", "--state", str(self.state_path), "--node", "n0"], "show")
        call_json(["report", "--state", str(self.state_path)], "report")
        call_json(["claims", "--state", str(self.state_path)], "claims")
        call_json([
            "claim", "add", "--state", str(self.state_path), "--node", "n0",
            "--text", "A claim", "--evidence", "E1",
        ], "claim add")
        call_json(["claim", "delete", "--state", str(self.state_path), "--id", "c1"], "claim delete")
        call_json(["reset", "--state", str(self.state_path), "--node", "n0"], "reset")
        with patch.object(research.subprocess, "run", return_value=completed({"results": [], "errors": {}})):
            call_json([
                "run", "--state", str(self.state_path), "--node", "n0", "--fetch", "0",
            ], "run")

        review_path = Path(str(self.state_path) + ".review.json")
        review_path.write_text(json.dumps({
            "node_id": "n0", "verdict": "answered", "result": "Verified conclusion.", "citations": ["E1"],
        }), encoding="utf-8")
        call_json(["apply-review", "--state", str(self.state_path), "--file", str(review_path)], "apply-review")
        call_json([
            "gaps", "--state", str(self.state_path), "--no-gaps", "--reviewed-node", "n0",
            "--reviewed-node", "n1", "--note", "Checked both existing nodes for this envelope test; no follow-up is needed.",
        ], "gaps")
        call_json(["digest", "--state", str(self.state_path)], "digest")

        tasks_dir = Path(str(self.state_path) + ".tasks")
        task_path = Path(str(self.state_path) + ".task.md")
        return_path = Path(str(self.state_path) + ".return.json")
        try:
            call_json(["emit-tasks", "--state", str(self.state_path), "--out", str(tasks_dir)], "emit-tasks")
            call_json([
                "emit-task", "--state", str(self.state_path), "--node", "n1", "--out", str(task_path),
            ], "emit-task")
            return_path.write_text(json.dumps({
                "task_id": "json-envelope-task", "node_id": "n1", "queries_run": [],
                "sources": [], "bodies": 0, "diagnostics": [],
            }), encoding="utf-8")
            call_json(["task-run", "--state", str(self.state_path), "--task", str(return_path)], "task-run")
        finally:
            if tasks_dir.exists():
                for path in tasks_dir.iterdir():
                    if path.is_file():
                        path.unlink()
                tasks_dir.rmdir()

        contract = call_json(["spec"], "spec")
        self.assertTrue(contract["commands"])
        self.assertTrue(all(item["supports_json"] for item in contract["commands"].values()))

    def test_json_errors_include_exit_codes_and_recovery_commands(self):
        def call_error(argv, code):
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(research.main([*argv, "--json"]), code)
            envelope = json.loads(output.getvalue())
            self.assertEqual(set(envelope), {"ok", "schema_version", "command", "error"})
            self.assertFalse(envelope["ok"])
            self.assertEqual(envelope["error"]["code"], code)
            self.assertTrue(envelope["error"]["next_command"])
            return envelope["error"]

        self.prepare_evidence_state()
        review_path = Path(str(self.state_path) + ".review.json")
        review_path.write_text(json.dumps({
            "node_id": "n0", "verdict": "answered", "result": "Done.",
        }), encoding="utf-8")
        research.apply_reviews(self.state_path, json.loads(review_path.read_text(encoding="utf-8")))
        state_error = call_error([
            "apply-review", "--state", str(self.state_path), "--file", str(review_path),
        ], 3)
        self.assertIn("answered", state_error["hint"])
        self.assertIn("report", state_error["hint"])
        self.assertIn("reset", state_error["hint"])
        self.assertIn("run", state_error["hint"])

        review_path.write_text(json.dumps({
            "node_id": "n1", "verdict": "answered", "citations": ["E99"],
        }), encoding="utf-8")
        refs_error = call_error([
            "apply-review", "--state", str(self.state_path), "--file", str(review_path),
        ], 2)
        self.assertIn("E99", refs_error["hint"])
        self.assertIn("prompt --state", refs_error["next_command"])
        self.assertIn("--node n1", refs_error["next_command"])

        self.state_path.unlink(missing_ok=True)
        self.prepare_review_at_depth(0)
        review_path.write_text(json.dumps({
            "node_id": "n0", "verdict": "partial", "result": "Still open.",
        }), encoding="utf-8")
        layer_error = call_error([
            "apply-review", "--state", str(self.state_path), "--file", str(review_path),
        ], 2)
        self.assertIn("1", layer_error["hint"])
        self.assertIn("prompt --state", layer_error["next_command"])
        self.assertIn("--allow-early-close", layer_error["hint"])

    def test_run_collection_failure_returns_code_four_json(self):
        self.create_state("Network-dependent question", platforms=("web",))
        output = StringIO()
        with patch.object(
            research.subprocess, "run",
            return_value=completed({"results": [], "errors": {"provider": "offline"}}),
        ), redirect_stdout(output):
            self.assertEqual(research.main([
                "run", "--state", str(self.state_path), "--node", "n0", "--fetch", "0", "--json",
            ]), 4)
        envelope = json.loads(output.getvalue())
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error"]["code"], 4)
        self.assertTrue(envelope["error"]["next_command"])
        self.assertEqual(research.read_state(self.state_path)["nodes"][0]["status"], "awaiting_review")

    def test_digest_routes_verification_nodes_to_question_conclusion_citations(self):
        self.create_state("Verification root")
        state = research.read_state(self.state_path)
        root = state["nodes"][0]
        root.update({
            "kind": "verification", "status": "answered", "result": "Official record confirms the date.",
        })
        root["review"].update({"citations": ["E1", "E2"]})
        verification_digest = research.digest_text(state)
        self.assertIn("（无 content 类型已答节点）", verification_digest)
        self.assertIn("### 核实记录", verification_digest)
        self.assertNotIn("- 组成：", verification_digest)

        content = dict(root)
        content.update({
            "id": "n1", "kind": "content", "question": "How is the process composed?", "depth": 1,
            "status": "answered", "result": "A content conclusion.",
            "review": {"citations": []},
        })
        state["nodes"].append(content)
        digest = research.digest_text(state)
        self.assertIn("### 核实记录", digest)
        self.assertIn("- 问题：Verification root", digest)
        self.assertIn("- 结论：Official record confirms the date.", digest)
        self.assertIn("- 引用：E1, E2", digest)
        self.assertEqual(digest.count("- 证据引用："), 1)


    def test_scroll_candidates_surface_nodes_with_expand_hints(self):
        self.prepare_evidence_state()
        state = research.read_state(self.state_path)
        state["items"][0]["expand_hints"] = {"read_more": 42}
        self.write_state(state)

        progress = research.progress_data(research.read_state(self.state_path), self.state_path)
        self.assertEqual([entry["id"] for entry in progress["scroll_candidates"]], ["n0"])
        self.assertEqual(progress["scroll_candidates"][0]["hints"], 42)
        self.assertTrue(any("--scroll 3" in command for command in progress["suggested_commands"]))
        self.assertIn("可考虑滚动节点", research.status_text(progress))

    def test_scrolled_items_are_not_listed_as_candidates(self):
        self.prepare_evidence_state()
        state = research.read_state(self.state_path)
        state["items"][0]["expand_hints"] = {"read_more": 42}
        state["items"][0]["scroll"] = {"screens": 3, "stopped": "limit"}
        self.write_state(state)

        progress = research.progress_data(research.read_state(self.state_path), self.state_path)
        self.assertEqual(progress["scroll_candidates"], [])


    def test_run_errors_with_usable_evidence_become_warnings(self):
        self.prepare_evidence_state()
        state = research.read_state(self.state_path)
        node = state["nodes"][0]
        node["status"] = "awaiting_review"
        node["fetch_errors"] = ["i9: fetch_smart timed out"]
        state["items"][0]["body_chars"] = 120
        self.write_state(state)

        failure, warnings = research._run_collection_failure(
            {**research.read_state(self.state_path), "_path": self.state_path}, ["n0"]
        )
        self.assertIsNone(failure)
        self.assertTrue(any("i9" in warning for warning in warnings))

    def test_run_errors_without_evidence_still_fail(self):
        self.prepare_evidence_state()
        state = research.read_state(self.state_path)
        node = state["nodes"][0]
        node["status"] = "awaiting_review"
        node["evidence_ids"] = []
        node["fetch_errors"] = ["i9: fetch_smart timed out"]
        state["items"] = []
        self.write_state(state)

        failure, warnings = research._run_collection_failure(
            {**research.read_state(self.state_path), "_path": self.state_path}, ["n0"]
        )
        self.assertIsNotNone(failure)
        self.assertEqual(failure.code, 4)
        self.assertEqual(warnings, [])


    def test_clean_stale_locks_removes_only_old_files(self):
        old = Path(str(self.state_path) + ".item.i1.fetch.lock")
        fresh = Path(str(self.state_path) + ".item.i2.fetch.lock")
        old.write_text("x", encoding="utf-8")
        fresh.write_text("x", encoding="utf-8")
        os.utime(old, (1, 1))
        removed = research.clean_stale_locks(self.state_path, older_than=60)
        self.assertIn(old.name, removed)
        self.assertTrue(fresh.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)


class CapabilityRegistryTests(unittest.TestCase):
    def setUp(self):
        import research
        self.capabilities = research.capabilities
        self.config = research.config
        self.tmp = tempfile.mkdtemp(prefix="research-caps-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(self.config.refresh)
        patcher = mock.patch.object(self.config, "config_file", return_value=Path(self.tmp) / "config.env")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(mock.patch.dict(os.environ, {}, clear=False).stop)

    def test_every_capability_has_a_user_facing_prompt(self):
        from unittest import mock
        for row in self.capabilities.statuses():
            self.assertTrue(row["asked_as"].strip(), row["key"])
            self.assertTrue(row["how_to_get"].strip(), row["key"])
            self.assertTrue(row["missing_effect"].strip(), row["key"])
            self.assertIn(row["tier"], {"required", "user_key", "agent_install", "local_service", "optional"})

    def _blank(self, key):
        real_get = self.config.get

        def fake(name, default=""):
            if name == key:
                return ""
            return real_get(name, default)

        return mock.patch.object(self.config, "get", side_effect=fake)

    def test_missing_user_key_is_reported_and_can_be_declined(self):
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": ""}, clear=False), self._blank("GROQ_API_KEY"):
            self.capabilities.decline("GROQ_API_KEY", "no account")
            row = next(item for item in self.capabilities.statuses() if item["key"] == "GROQ_API_KEY")
            self.assertEqual(row["state"], "declined")

    def test_missing_key_without_decline_stays_missing(self):
        with mock.patch.dict(os.environ, {"AGNES_API_KEY": ""}, clear=False), self._blank("AGNES_API_KEY"):
            row = next(item for item in self.capabilities.statuses() if item["key"] == "AGNES_API_KEY")
            self.assertEqual(row["state"], "missing")

    def test_spec_lists_setup_commands(self):
        parser = research.build_parser()
        text = json.dumps(research.contract_data(parser), ensure_ascii=False)
        self.assertIn("setup", text)


class DoctorSearchGroupTests(unittest.TestCase):
    def test_tavily_and_searxng_collapse_into_one_requirement(self):
        import research

        rows = [
            {"key": "TAVILY_API_KEY", "state": "missing", "tier": "required",
             "asked_as": "a", "how_to_get": "b", "missing_effect": "c"},
            {"key": "SEARXNG_URL", "state": "missing", "tier": "required",
             "asked_as": "a", "how_to_get": "b", "missing_effect": "c"},
        ]
        with mock.patch.object(research.capabilities, "statuses", return_value=rows), \
             mock.patch.object(research.capabilities, "search_ready", return_value=False):
            data = research.doctor_data()
        keys = [item["key"] for item in data["requirements"]]
        self.assertEqual(keys.count("SEARCH_PROVIDER"), 1)
        self.assertNotIn("TAVILY_API_KEY", keys)
        self.assertNotIn("SEARXNG_URL", keys)
        self.assertTrue(data["needs_user"])
