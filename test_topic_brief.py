"""Tests for the topic brief layer (template, structural check, query matrix)."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import research
import topic_brief


def sample(**overrides):
    brief = topic_brief.template("NB/T 47013 换版")
    brief.update({
        "canonical": "NB/T 47013 承压设备无损检测",
        "aliases": [
            {"value": "47013", "kind": "short", "status": "model", "source": "编号"},
            {"value": "承压设备无损检测", "kind": "official", "status": "verified", "source": "标准名"},
        ],
        "exclusions": ["医疗器械无损检测"],
        "purpose": "给设计工程师版本判断",
        "deliverable": "每条结论带标准号与日期",
    })
    brief.update(overrides)
    return brief


class TopicBriefTests(unittest.TestCase):
    def test_template_requires_topic_and_explains_fields(self):
        brief = topic_brief.template("测试主题")
        self.assertEqual(brief["topic"], "测试主题")
        self.assertIn("aliases", brief["field_hints"])
        report = topic_brief.validate(brief)
        self.assertFalse(report["ok"])
        self.assertIn("purpose", report["missing"])

    def test_valid_but_empty_aliases_are_allowed_with_warning(self):
        brief = sample(aliases=[], canonical="")
        report = topic_brief.validate(brief)
        self.assertTrue(report["ok"])
        self.assertTrue(any("别名" in warning for warning in report["warnings"]))

    def test_alias_equal_to_exclusion_is_flagged(self):
        brief = sample(exclusions=["47013"])
        report = topic_brief.validate(brief)
        self.assertTrue(any("排除词与别名相同" in warning for warning in report["warnings"]))

    def test_matrix_starts_with_canonical_and_respects_budget(self):
        brief = sample()
        result = topic_brief.matrix(brief, ["换版", "实施日期"], budget=3)
        self.assertEqual(len(result["candidates"]), 3)
        self.assertEqual(result["candidates"][0]["expression"], "canonical")
        self.assertGreater(result["dropped"], 0)
        queries = [row["query"] for row in result["candidates"]]
        self.assertEqual(len(queries), len(set(queries)))

    def test_matrix_reports_missing_facets(self):
        result = topic_brief.matrix(sample(), [], budget=5)
        self.assertEqual(result["candidates"], [])
        self.assertIn("facet", result["note"])

    def test_mentions_counts_titles_and_bodies_per_alias(self):
        items = [
            {"title": "NB/T 47013 换版", "body": "承压设备无损检测 新版本"},
            {"title": "无关文章", "body": "47013 只是编号巧合"},
        ]
        result = topic_brief.mentions(sample(), items)
        rows = {row["alias"]: row for row in result["by_alias"]}
        self.assertEqual(rows["47013"]["title_hits"], 1)
        self.assertEqual(rows["47013"]["body_hits"], 1)
        self.assertEqual(rows["承压设备无损检测"]["body_hits"], 1)
        self.assertEqual(result["evidence_total"], 2)

    def test_alias_query_observation_checks_literal_text_not_result_hits(self):
        brief = sample(aliases=[
            {"value": "Python 3.13 new features", "kind": "english", "status": "model"},
            {"value": "Python 3.13 runtime", "kind": "english", "status": "model"},
        ])
        nodes = [{
            "question": "Python 3.13 overview",
            "queries": ["PYTHON 3.13 NEW FEATURES"],
            "counter_queries": ["python    3.13 runtime regressions"],
        }]
        self.assertEqual(topic_brief.aliases_without_queries(brief, nodes), [])

        nodes[0]["queries"] = ["Python 3.13 syntax overview"]
        missing = topic_brief.aliases_without_queries(brief, nodes)
        self.assertEqual([row["alias"] for row in missing], ["Python 3.13 new features"])
        self.assertIn("不表示搜索结果是否命中", missing[0]["note"])


class BriefCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="brief-cli-"))
        self.addCleanup(lambda: [p.unlink() for p in self.tmp.glob("*")] and None)
        self.brief_path = self.tmp / "brief.json"
        self.brief_path.write_text(json.dumps(sample(), ensure_ascii=False), encoding="utf-8")

    def _json(self, argv):
        from contextlib import redirect_stdout
        import io

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = research.main(argv)
        return code, json.loads(buffer.getvalue())

    def test_check_returns_ok_for_complete_brief(self):
        code, payload = self._json(["brief", "--check", "--file", str(self.brief_path), "--json"])
        self.assertEqual(code, 0)
        self.assertTrue(payload["data"]["ok"])

    def test_init_with_brief_stores_it_and_syncs_purpose(self):
        state = self.tmp / "tree.json"
        code, payload = self._json([
            "init", "NB/T 47013 是否换版", "--state", str(state), "--brief", str(self.brief_path), "--json",
        ])
        self.assertEqual(code, 0)
        stored = json.loads(state.read_text(encoding="utf-8"))
        self.assertEqual(stored["purpose"], sample()["purpose"])
        self.assertEqual(stored["topic_brief"]["canonical"], sample()["canonical"])

    def test_init_rejects_conflicting_purpose(self):
        state = self.tmp / "tree2.json"
        from contextlib import redirect_stdout
        import io

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = research.main([
                "init", "问题", "--state", str(state), "--brief", str(self.brief_path),
                "--purpose", "另一个目的", "--json",
            ])
        self.assertNotEqual(code, 0)
        self.assertFalse(state.exists())
