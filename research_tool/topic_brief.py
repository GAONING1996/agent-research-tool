"""Topic brief: structured understanding before any query is written.

The tool only provides a template, structural checks and mechanical query candidates.
Judgement (what the topic really means, which alias is trustworthy, what to search)
stays with the model; nothing here scores or auto-selects.
"""

from __future__ import annotations

import re
from typing import Any

ALIAS_KINDS = ("official", "short", "old", "colloquial", "english", "abbrev")
ALIAS_STATES = ("provided", "model", "verified", "unknown")
REQUIRED_FIELDS = ("topic", "purpose", "deliverable")


def template(topic: str) -> dict[str, Any]:
    return {
        "schema": 1,
        "topic": topic.strip(),
        "canonical": "",
        "aliases": [],
        "relations": {"broader": [], "narrower": []},
        "scope": {
            "judgement_date": "",
            "event_window": "",
            "jurisdiction": "",
            "industry": "",
        },
        "exclusions": [],
        "purpose": "",
        "deliverable": "",
        "perspectives": [],
        "unknowns": [],
        "field_hints": {
            "canonical": "正规全名；不确定就留空并把词写进 unknowns，不要编。",
            "aliases": "每条 {value, kind(official/short/old/colloquial/english/abbrev), status(provided/model/verified/unknown), source}",
            "relations": "broader=上位概念，narrower=下位概念；没有明确层级就留空。",
            "scope": "judgement_date=这批结论对应哪一天；event_window=事件发生的时间范围；jurisdiction=地域或标准体系；industry=行业限定。",
            "exclusions": "明确排除的同名事物，用来防同名污染。",
            "perspectives": "每个 {name, why, questions[]}；why 写为什么这个视角对本次调研有用。",
            "unknowns": "你无法确认但会影响检索的词（拼写、旧称、英文名等）。",
        },
    }


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def _alias_rows(brief: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for item in brief.get("aliases") or []:
        if isinstance(item, str):
            rows.append({"value": item, "kind": "official", "status": "model", "source": ""})
        elif isinstance(item, dict) and str(item.get("value") or "").strip():
            rows.append(item)
    return rows


def validate(brief: dict[str, Any]) -> dict[str, Any]:
    """Structural check only: missing fields and obvious literal conflicts."""
    if not isinstance(brief, dict):
        return {"ok": False, "missing": ["brief must be a JSON object"], "warnings": []}

    missing = [name for name in REQUIRED_FIELDS if not str(brief.get(name) or "").strip()]
    warnings: list[str] = []

    canonical = str(brief.get("canonical") or "").strip()
    aliases = _alias_rows(brief)
    if not canonical and not aliases:
        warnings.append("没有正规名也没有别名：允许，但检索会只依赖主题原话，建议补充或写进 unknowns。")

    for index, alias in enumerate(aliases, start=1):
        value = str(alias.get("value") or "").strip()
        kind = str(alias.get("kind") or "").strip()
        state = str(alias.get("status") or "").strip()
        if not value:
            missing.append(f"aliases[{index}].value")
        if kind and kind not in ALIAS_KINDS:
            warnings.append(f"aliases[{index}].kind 不在 {ALIAS_KINDS}：{kind}")
        if state and state not in ALIAS_STATES:
            warnings.append(f"aliases[{index}].status 不在 {ALIAS_STATES}：{state}")
        if state == "unknown" and value:
            warnings.append(f"aliases[{index}]={value} 标为 unknown 却填了值，确认是候选还是已核实。")

    exclusions = [str(item).strip() for item in (brief.get("exclusions") or []) if str(item).strip()]
    for alias in aliases:
        value = str(alias.get("value") or "").strip()
        for exclusion in exclusions:
            if value and _norm(value) == _norm(exclusion):
                warnings.append(f"排除词与别名相同：{value}；这会让检索自我抵消。")

    scope = brief.get("scope") or {}
    if not isinstance(scope, dict):
        warnings.append("scope 必须是对象。")

    for perspective in brief.get("perspectives") or []:
        if not isinstance(perspective, dict) or not str(perspective.get("name") or "").strip():
            warnings.append("perspectives 里每条至少要有 name。")
            break

    return {"ok": not missing, "missing": missing, "warnings": warnings}


def matrix(brief: dict[str, Any], facets: list[str], budget: int = 8) -> dict[str, Any]:
    """Mechanical query candidates: canonical/topic first, then trustworthy aliases."""
    facets = [str(facet).strip() for facet in facets if str(facet).strip()]
    if not facets:
        return {"candidates": [], "dropped": 0, "note": "至少给一个 --facet，例如 换版 / 价格 / 兼容性。"}

    canonical = str(brief.get("canonical") or "").strip() or str(brief.get("topic") or "").strip()
    aliases = [
        alias for alias in _alias_rows(brief)
        if str(alias.get("status") or "model") != "unknown"
    ]

    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(term: str, facet: str, expression: str, reason: str) -> None:
        query = re.sub(r"\s+", " ", f"{term} {facet}").strip()
        key = _norm(query)
        if not query or key in seen:
            return
        seen.add(key)
        candidates.append({
            "query": query,
            "facet": facet,
            "expression": expression,
            "reason": reason,
        })

    for facet in facets:
        add(canonical, facet, "canonical", "先用正规名或主题原话，覆盖最主流的表述。")
    for alias in aliases:
        kind = str(alias.get("kind") or "official")
        label = f"alias:{kind}"
        for facet in facets:
            add(str(alias.get("value")), facet, label, "换一种叫法能捞到主流表述搜不到的结果。")

    dropped = max(0, len(candidates) - max(1, budget))
    return {
        "candidates": candidates[: max(1, budget)],
        "dropped": dropped,
        "note": "候选只是原料：挑哪些、先跑哪些由你决定；budget 是为了防止一次派发过多查询。",
    }


def mentions(brief: Any, items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Per-alias observation counts. Not a score, not a gate."""
    if not isinstance(brief, dict):
        return None
    aliases = [str(alias.get("value") or "").strip() for alias in _alias_rows(brief)]
    aliases = [alias for alias in aliases if alias]
    rows = []
    for alias in aliases:
        key = alias.casefold()
        title_hits = sum(1 for item in items if key in str(item.get("title") or "").casefold())
        body_hits = sum(1 for item in items if key in str(item.get("body") or "").casefold())
        rows.append({"alias": alias, "title_hits": title_hits, "body_hits": body_hits})
    return {
        "evidence_total": len(items),
        "by_alias": rows,
        "note": "纯观察：命中不等于相关，未命中也可能是别名写错；不据此判定成败。",
    }


def aliases_without_queries(brief: Any, nodes: list[dict[str, Any]]) -> list[dict[str, str]] | None:
    """List aliases absent from node query text; this does not inspect search results."""
    if not isinstance(brief, dict):
        return None

    query_texts = []
    for node in nodes:
        query_texts.append(str(node.get("question") or ""))
        for field in ("queries", "counter_queries"):
            values = node.get(field)
            if isinstance(values, list):
                query_texts.extend(str(value) for value in values if str(value).strip())
    normalized_queries = [" ".join(query.casefold().split()) for query in query_texts]

    missing = []
    for alias in _alias_rows(brief):
        value = str(alias.get("value") or "").strip()
        normalized_alias = " ".join(value.casefold().split())
        if normalized_alias and not any(normalized_alias in query for query in normalized_queries):
            missing.append({
                "alias": value,
                "note": "该叫法未按字面出现在任何节点问题、查询或反查查询中；不表示搜索结果是否命中。",
            })
    return missing
