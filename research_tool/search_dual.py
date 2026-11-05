"""Query Tavily and SearXNG concurrently and merge their search results."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, wait
from html.parser import HTMLParser
from pathlib import Path

try:
    from . import config
except ImportError:
    import config
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests


TAVILY_SEARCH_URL = "https://api.tavily.com/search"
REQUEST_TIMEOUT_SECONDS = 20
OVERALL_TIMEOUT_SECONDS = 24.0
TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "dclid",
    "msclkid",
    "mc_cid",
    "mc_eid",
    "igshid",
    "yclid",
    "srsltid",
    "ref",
    "ref_src",
    "ref_url",
}


class _TitleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_title = False
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "title":
            self.in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.parts.append(data)


def _cjk_count(text: str) -> int:
    return sum(
        1
        for char in text
        if (
            0x3400 <= ord(char) <= 0x4DBF
            or 0x4E00 <= ord(char) <= 0x9FFF
            or 0xF900 <= ord(char) <= 0xFAFF
            or 0x20000 <= ord(char) <= 0x2FA1F
            or 0x3040 <= ord(char) <= 0x30FF
            or 0xAC00 <= ord(char) <= 0xD7AF
        )
    )


def _has_secondary_mojibake(s: str) -> bool:
    original_cjk = _cjk_count(s)
    counts = [
        sum(start <= ord(char) <= end for char in s)
        for start, end in (
            (0x0100, 0x024F),
            (0x0300, 0x036F),
            (0x0370, 0x03FF),
            (0x0400, 0x052F),
            (0x0530, 0x058F),
            (0x0590, 0x05FF),
            (0x0600, 0x06FF),
            (0x0700, 0x074F),
            (0x0780, 0x07BF),
            (0x07C0, 0x07FF),
        )
    ]
    return (
        original_cjk / max(len(s), 1) < 0.15
        and sum(counts) >= 6
        and sum(count > 0 for count in counts) >= 3
    )


def _fetch_gbk_page_title(url: str) -> str:
    try:
        response = requests.get(url, timeout=5)
    except requests.RequestException:
        return ""
    if not 200 <= response.status_code < 300:
        return ""
    encoding = (response.encoding or "").lower()
    if encoding not in {"gbk", "gb2312", "gb18030", "cp936"}:
        return ""
    parser = _TitleParser()
    parser.feed(response.content.decode(encoding, errors="replace"))
    return "".join(parser.parts).strip()


def _repair_text_with_confidence(s: str) -> tuple[str, bool]:
    """Return repaired text and whether a suspected encoding issue remains."""
    original_cjk = _cjk_count(s)
    latin1_count = sum(0x80 <= ord(char) <= 0xFF for char in s)
    latin1_candidate = latin1_count > 0 and (
        original_cjk > 0 or (latin1_count >= 4 and original_cjk / max(len(s), 1) < 0.15)
    )

    # This also covers text that was re-encoded as UTF-8 before the bad GBK decode.
    widened_candidate = _has_secondary_mojibake(s)
    if not latin1_candidate and not widened_candidate:
        return s, False

    source_bytes = s.encode("latin-1", errors="ignore") if latin1_candidate else s.encode("utf-8")
    candidates = [
        source_bytes.decode(encoding, errors="replace")
        for encoding in ("gbk", "gb18030")
    ]
    minimum_cjk = original_cjk * 2
    confident = [
        candidate
        for candidate in candidates
        if _cjk_count(candidate) >= minimum_cjk and "\ufffd" not in candidate
    ]
    # With no CJK in a widened candidate, a byte-level decode can turn unrelated
    # scripts into plausible-looking but incorrect Chinese text.
    if widened_candidate and original_cjk == 0:
        confident = []
    if confident:
        return max(confident, key=_cjk_count), False
    return s, True


def _repair_text(s: str) -> str:
    """Repair GBK text only when the decoded candidate passes confidence checks."""
    return _repair_text_with_confidence(s)[0]


def _load_local_env() -> None:
    """Load missing variables from the sibling Hermes .env without overwriting env."""
    env_path = config.find_env_file()
    if env_path is None:
        return
    try:
        lines = env_path.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        return
    except OSError:
        return

    for line in lines:
        value_line = line.strip()
        if not value_line or value_line.startswith("#"):
            continue
        if value_line.startswith("export "):
            value_line = value_line[7:].lstrip()
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", value_line)
        if not match:
            continue
        name, value = match.groups()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        if name not in os.environ or not os.environ[name].strip():
            os.environ[name] = value


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Missing required configuration {name}; set it in "
            f"{Path(__file__).resolve().parent.parent / '.env'} or the environment."
        )
    return value


def normalize_url(url: str) -> str:
    """Normalize a result URL and use HTTPS as the scheme-independent canonical form."""
    candidate = str(url or "").strip()
    if not candidate:
        raise ValueError("result has no URL")
    if not urlsplit(candidate).scheme:
        candidate = "https://" + candidate
    parts = urlsplit(candidate)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ValueError(f"unsupported or invalid URL: {candidate}")

    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"invalid URL port: {candidate}") from exc

    hostname = parts.hostname.lower()
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    if port is not None and port not in {80, 443}:
        hostname = f"{hostname}:{port}"

    path = parts.path.rstrip("/")
    query_items = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        lower_key = key.lower()
        if lower_key.startswith("utm_") or lower_key in TRACKING_PARAMS:
            continue
        query_items.append((key, value))
    query_items.sort(key=lambda item: (item[0].lower(), item[0], item[1]))
    query = urlencode(query_items, doseq=True)
    return urlunsplit(("https", hostname, path, query, ""))


def _search_tavily(query: str, limit: int) -> list[dict[str, Any]]:
    api_key = _required_env("TAVILY_API_KEY")
    response = requests.post(
        TAVILY_SEARCH_URL,
        json={
            "api_key": api_key,
            "query": query,
            "max_results": limit,
            "search_depth": "basic",
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"Tavily returned HTTP {response.status_code}")
    payload = response.json()
    rows = payload.get("results", [])
    if not isinstance(rows, list):
        raise RuntimeError("Tavily response did not contain a results list")
    return rows


def _search_searxng(query: str, lang: str) -> list[dict[str, Any]]:
    base_url = _required_env("SEARXNG_URL").rstrip("/")
    params = {"q": query, "format": "json"}
    if lang:
        params["language"] = lang
    response = requests.get(
        f"{base_url}/search",
        params=params,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"SearXNG returned HTTP {response.status_code}")
    payload = response.json()
    rows = payload.get("results", [])
    if not isinstance(rows, list):
        raise RuntimeError("SearXNG response did not contain a results list")
    return rows


def _ranked_rows(rows: list[dict[str, Any]], source: str) -> list[dict[str, Any]]:
    ranked = []
    for rank, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        try:
            normalized = normalize_url(row.get("url", ""))
        except (TypeError, ValueError):
            continue
        original_title = str(row.get("title") or "").strip()
        title, title_unreliable = _repair_text_with_confidence(original_title)
        if _has_secondary_mojibake(original_title):
            page_title = _fetch_gbk_page_title(normalized)
            if "\ufffd" not in page_title and _cjk_count(page_title) > _cjk_count(title):
                title = page_title
                title_unreliable = False
        published = (
            row.get("published_date")
            or row.get("publishedDate")
            or row.get("published")
            or row.get("date")
        )
        snippet = row.get("content") or row.get("snippet") or row.get("raw_content") or ""
        item = {
            "url": normalized,
            "canonical": normalized,
            "title": title,
            "snippet": _repair_text(str(snippet).strip()),
            "published": published,
            "source": source,
            "rank": rank,
        }
        if title_unreliable:
            item["title_encoding_unreliable"] = True
        ranked.append(item)
    return ranked


def merge_results(
    tavily_rows: list[dict[str, Any]], searxng_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Merge provider rows, prioritizing shared results and interleaving unique ones."""
    merged: dict[str, dict[str, Any]] = {}
    for source, rows in (("tavily", tavily_rows), ("searxng", searxng_rows)):
        for item in _ranked_rows(rows, source):
            canonical = item["canonical"]
            result = merged.get(canonical)
            if result is None:
                result = {
                    "url": item["url"],
                    "title": item["title"],
                    "snippet": item["snippet"],
                    "sources": [],
                    "rank_tavily": None,
                    "rank_searxng": None,
                    "published": item["published"],
                    "canonical": canonical,
                }
                if item.get("title_encoding_unreliable"):
                    result["title_encoding_unreliable"] = True
                merged[canonical] = result
            elif len(item["snippet"]) > len(result["snippet"]):
                result["snippet"] = item["snippet"]
            if (
                result.get("title_encoding_unreliable")
                and item.get("title")
                and not item.get("title_encoding_unreliable")
            ):
                result["title"] = item["title"]
                result.pop("title_encoding_unreliable", None)
            if not result["title"] and item["title"]:
                result["title"] = item["title"]
                if item.get("title_encoding_unreliable"):
                    result["title_encoding_unreliable"] = True
            if result["published"] is None and item["published"] is not None:
                result["published"] = item["published"]
            if source not in result["sources"]:
                result["sources"].append(source)
            result[f"rank_{source}"] = item["rank"]

    for result in merged.values():
        result["sources"] = [s for s in ("tavily", "searxng") if s in result["sources"]]

    shared = [result for result in merged.values() if len(result["sources"]) == 2]
    shared.sort(
        key=lambda result: (
            result["rank_tavily"] + result["rank_searxng"],
            result["rank_tavily"],
            result["rank_searxng"],
        )
    )

    tavily_only = sorted(
        (result for result in merged.values() if result["sources"] == ["tavily"]),
        key=lambda result: result["rank_tavily"],
    )
    searxng_only = sorted(
        (result for result in merged.values() if result["sources"] == ["searxng"]),
        key=lambda result: result["rank_searxng"],
    )
    singles = []
    if tavily_only and searxng_only:
        source = (
            "tavily"
            if tavily_only[0]["rank_tavily"] <= searxng_only[0]["rank_searxng"]
            else "searxng"
        )
        while tavily_only or searxng_only:
            current = tavily_only if source == "tavily" else searxng_only
            other = searxng_only if source == "tavily" else tavily_only
            if current:
                singles.append(current.pop(0))
            elif other:
                singles.append(other.pop(0))
            source = "searxng" if source == "tavily" else "tavily"
    else:
        singles = tavily_only + searxng_only
    return shared + singles


def search_dual(query: str, limit: int = 10, lang: str = "") -> dict[str, Any]:
    """Search Tavily and SearXNG concurrently, returning merged results and diagnostics."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string")
    if limit < 1:
        raise ValueError("limit must be at least 1")

    _load_local_env()
    started = time.monotonic()
    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="dual-search")
    futures = {
        executor.submit(_search_tavily, query, limit): "tavily",
        executor.submit(_search_searxng, query, lang): "searxng",
    }
    done, pending = wait(futures, timeout=OVERALL_TIMEOUT_SECONDS)
    errors: dict[str, str] = {}
    results_by_source: dict[str, list[dict[str, Any]]] = {"tavily": [], "searxng": []}

    for future in done:
        source = futures[future]
        try:
            results_by_source[source] = future.result()
        except Exception as exc:
            errors[source] = str(exc) or exc.__class__.__name__
    for future in pending:
        source = futures[future]
        future.cancel()
        errors[source] = f"timed out after {OVERALL_TIMEOUT_SECONDS:g} seconds (overall deadline)"
    executor.shutdown(wait=False, cancel_futures=True)

    all_merged = merge_results(results_by_source["tavily"], results_by_source["searxng"])
    results = all_merged[:limit]
    elapsed = time.monotonic() - started
    return {
        "query": query,
        "results": results,
        "stats": {
            "tavily_results": len(results_by_source["tavily"]),
            "searxng_results": len(results_by_source["searxng"]),
            "unique_results": len(all_merged),
            "merged_results": len(results),
            "duplicates_removed": (
                len(results_by_source["tavily"])
                + len(results_by_source["searxng"])
                - len(all_merged)
            ),
            "elapsed_seconds": round(elapsed, 3),
        },
        "degraded": bool(errors),
        "errors": errors,
    }


def _configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except (OSError, ValueError):
                pass


def main(argv: list[str] | None = None) -> int:
    _configure_console()
    parser = argparse.ArgumentParser(description="Search Tavily and SearXNG together.")
    parser.add_argument("query", help="search query")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--lang", default="")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    try:
        payload = search_dual(args.query, limit=args.limit, lang=args.lang)
    except Exception as exc:
        print(f"search_dual: {exc}", file=sys.stderr)
        return 3

    if args.as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for result in payload["results"]:
            if len(result["sources"]) == 2:
                source_label = "[双源]"
            elif result["sources"] == ["tavily"]:
                source_label = "[T]"
            else:
                source_label = "[S]"
            print(f"{source_label} {result['title']} — {result['url']}")
        stats = payload["stats"]
        print(
            "统计：Tavily 返回 {tavily_results} 条，SearXNG 返回 {searxng_results} 条，"
            "合并后 {merged_results} 条，去重 {duplicates_removed} 条，耗时 {elapsed_seconds:.3f}s".format(
                **stats
            )
        )
    for source, error in payload["errors"].items():
        print(f"{source} error: {error}", file=sys.stderr)
    if set(payload["errors"]) == {"tavily", "searxng"}:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
