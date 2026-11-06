"""Tests for gap rounds: declare, spawn, resolve, and surface in status/digest."""

import json
import tempfile
import unittest
from pathlib import Path

import research


class GapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gaps-"))
        self.state = self.tmp / "tree.json"
        self.brief = {
            "topic": "主题",
            "canonical": "正规名",
            "aliases": [{"value": "别名A", "kind": "short", "status": "model", "source": "x"}],
            "purpose": "目的",
            "deliverable": "交付物",
        }
        research.init_state("问题", self.state, brief=self.brief)

    def test_record_assigns_sequential_ids_and_rounds(self):
        first = research.record_gaps(self.state, [
            {"what": "缺口一", "next_question": "问题一"},
            {"what": "缺口二", "next_question": "问题二"},
        ])
        self.assertEqual([gap["id"] for gap in first["gaps"]], ["g1", "g2"])
        second = research.record_gaps(self.state, [{"what": "缺口三", "next_question": "问题三"}])
        self.assertEqual(first["round"], 1)
        self.assertEqual(second["round"], 2)
        self.assertEqual(second["gaps"][0]["id"], "g3")

    def test_record_rejects_empty_gap(self):
        with self.assertRaises(ValueError):
            research.record_gaps(self.state, [{}])

    def test_spawn_creates_child_with_queries_and_edge(self):
        research.record_gaps(self.state, [{
            "what": "英文名没搜", "why": "0 命中", "angle": "英文资料",
            "next_question": "英文标准名是什么", "queries": ["NB/T 47013 English"],
        }])
        result = research.spawn_gaps(self.state)
        self.assertEqual(result["created"][0]["node_id"], "n1")
        state = research.read_state(self.state)
        child = [node for node in state["nodes"] if node["id"] == "n1"][0]
        self.assertEqual(child["queries"], ["NB/T 47013 English"])
        self.assertEqual(child["depth"], 1)
        self.assertTrue(any(edge["kind"] == "gap" and edge["to"] == "n1" for edge in state["edges"]))
        self.assertEqual(state["gap_rounds"][0]["gaps"][0]["node_id"], "n1")

    def test_spawn_twice_does_not_duplicate(self):
        research.record_gaps(self.state, [{"what": "缺口", "next_question": "问题"}])
        research.spawn_gaps(self.state)
        with self.assertRaises(ValueError):
            research.spawn_gaps(self.state)

    def test_resolve_requires_note_for_dropped(self):
        research.record_gaps(self.state, [{"what": "缺口", "next_question": "问题"}])
        with self.assertRaises(ValueError):
            research.resolve_gap(self.state, "g1", "dropped")
        gap = research.resolve_gap(self.state, "g1", "dropped", "主题已过时")
        self.assertEqual(gap["status"], "dropped")

    def test_status_and_digest_surface_open_gaps(self):
        research.record_gaps(self.state, [{"what": "缺口X", "next_question": "问题X"}])
        data = research.status_data(self.state) if hasattr(research, "status_data") else None
        state = research.read_state(self.state)
        self.assertTrue(research.digest_text(state).startswith("## 未处理的缺口"))
        research.resolve_gap(self.state, "g1", "covered", "已补")
        self.assertEqual(research.gap_observations(research.read_state(self.state))["open_gaps"], [])

    def test_digest_is_blocked_until_gaps_are_handled(self):
        from contextlib import redirect_stderr, redirect_stdout
        import io

        research.record_gaps(self.state, [{"what": "缺口", "next_question": "问题"}])
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            blocked = research.main(["digest", "--state", str(self.state), "--json"])
            allowed = research.main(["digest", "--state", str(self.state), "--allow-open-gaps", "--json"])
        self.assertEqual(blocked, 3)
        self.assertEqual(allowed, 0)
        research.resolve_gap(self.state, "g1", "covered", "已补")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(research.main(["digest", "--state", str(self.state), "--json"]), 0)

    def test_digest_requires_a_gap_review_even_when_open_gaps_are_allowed(self):
        from contextlib import redirect_stderr, redirect_stdout
        import io

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = research.main([
                "digest", "--state", str(self.state), "--allow-open-gaps", "--json",
            ])
        self.assertEqual(code, 3)

    def test_no_gaps_declaration_is_audited_and_unblocks_digest(self):
        from contextlib import redirect_stderr, redirect_stdout
        import io

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            missing_reason = research.main(["gaps", "--state", str(self.state), "--no-gaps", "--json"])
        self.assertEqual(missing_reason, 2)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            missing_nodes = research.main([
                "gaps", "--state", str(self.state), "--no-gaps", "--note", "检查后没有缺口。", "--json",
            ])
        self.assertEqual(missing_nodes, 2)
        with self.assertRaises(ValueError):
            research.record_gaps(
                self.state, [], no_gaps_reason="检查后没有缺口。", reviewed_nodes=["missing"],
            )

        note = "逐项检查了已审节点、别名查询和交付要求，没有发现待补事实。"
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            recorded = research.main([
                "gaps", "--state", str(self.state), "--no-gaps", "--reviewed-node", "n0",
                "--note", note, "--json",
            ])
            digest = research.main(["digest", "--state", str(self.state), "--json"])
        self.assertEqual(recorded, 0)
        self.assertEqual(digest, 0)
        state = research.read_state(self.state)
        self.assertTrue(research.gap_review_recorded(state))
        self.assertEqual(state["gap_rounds"][0]["no_gaps_reason"], note)
        self.assertEqual(state["gap_rounds"][0]["reviewed_nodes"], ["n0"])
        rounds = research.gap_observations(state)["review_rounds"]
        self.assertEqual(rounds[0]["no_gaps_reason"], note)
        self.assertEqual(rounds[0]["reviewed_nodes"], ["n0"])

    def test_observations_report_aliases_without_hits(self):
        observed = research.gap_observations(research.read_state(self.state))
        self.assertEqual(observed["facts"]["evidence_total"], 0)
        self.assertEqual(observed["facts"]["aliases_without_hits"][0]["alias"], "别名A")
        self.assertEqual(observed["facts"]["nodes_without_evidence"][0]["node_id"], "n0")

    def test_observations_report_aliases_absent_from_query_text(self):
        observed = research.gap_observations(research.read_state(self.state))
        self.assertEqual(observed["facts"]["aliases_without_queries"][0]["alias"], "别名A")

        research.set_node(self.state, "n0", queries=["研究别名A的资料"])
        observed = research.gap_observations(research.read_state(self.state))
        self.assertEqual(observed["facts"]["aliases_without_queries"], [])
        status = research.progress_data(research.read_state(self.state), self.state)
        self.assertEqual(status["aliases_without_queries"], [])

    def test_perspective_context_does_not_claim_literal_coverage(self):
        state_path = self.tmp / "perspectives.json"
        brief = dict(self.brief)
        brief["perspectives"] = [{
            "name": "语言与运行时",
            "questions": ["Python 3.13 的语言变化与运行时行为是什么？"],
        }]
        research.init_state("Python 3.13 的语言特性和运行时行为", state_path, brief=brief)

        observed = research.gap_observations(research.read_state(state_path))
        self.assertNotIn("perspectives_without_node", observed["facts"])
        self.assertEqual(observed["perspective_context"]["perspectives"][0]["name"], "语言与运行时")
        self.assertEqual(observed["perspective_context"]["node_questions"][0]["node_id"], "n0")


if __name__ == "__main__":
    unittest.main()
