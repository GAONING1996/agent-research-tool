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

    def test_observations_report_aliases_without_hits(self):
        observed = research.gap_observations(research.read_state(self.state))
        self.assertEqual(observed["facts"]["evidence_total"], 0)
        self.assertEqual(observed["facts"]["aliases_without_hits"][0]["alias"], "别名A")
        self.assertEqual(observed["facts"]["nodes_without_evidence"][0]["node_id"], "n0")


if __name__ == "__main__":
    unittest.main()
