"""Fetch and normalize RSS 2.0 and Atom feeds."""

from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup


try:
    from . import config
except ImportError:
    import config

PROXY_URL = config.proxy_url()
DEFAULT_FEEDS = Path(__file__).resolve().with_name("feeds.json")
USER_AGENT = "HermesRSSReader/1.0 (+https://example.invalid)"


def _configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def _load_env() -> None:
    """Load missing credentials for safe error redaction, without displaying them."""
    env_path = config.find_env_file()
    if env_path is None:
        return
    try:
        lines = env_path.read_text(encoding="utf-8-sig").splitlines()
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


def _safe_error(value: Any) -> str:
    message = str(value) or value.__class__.__name__
    for name in ("TAVILY_API_KEY", "GROQ_API_KEY", "SEARXNG_URL"):
        secret = os.environ.get(name, "").strip()
        if secret:
            message = message.replace(secret, "[redacted]")
    return message[:1000]


def _redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _redact_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    if isinstance(value, str):
        return _safe_error(value)
    return value


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].split(":")[-1].lower()


def _text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    raw = " ".join(part.strip() for part in node.itertext() if part.strip())
    if not raw:
        raw = node.attrib.get("href", "")
    decoded = html.unescape(raw)
    if "<" not in decoded:
        return re.sub(r"\s+", " ", decoded).strip()
    soup = BeautifulSoup(decoded, "html.parser")
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()


def parse_date(value: Any) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            seconds = float(value)
            if seconds > 100_000_000_000:
                seconds /= 1000
            return dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _first_text(element: ET.Element, names: set[str]) -> str:
    for child in element:
        if _local_name(child.tag) in names:
            value = _text(child)
            if value:
                return value
    return ""


def parse_feed_xml(xml_data: str | bytes, name: str = "feed") -> list[dict[str, Any]]:
    """Parse RSS 2.0 or Atom XML into a common, source-tagged item list."""
    root = ET.fromstring(xml_data)
    rows = [node for node in root.iter() if _local_name(node.tag) in {"item", "entry"}]
    items: list[dict[str, Any]] = []
    for row in rows:
        title = _first_text(row, {"title"})
        link = ""
        for node in row:
            if _local_name(node.tag) != "link":
                continue
            href = node.attrib.get("href", "").strip()
            rel = node.attrib.get("rel", "alternate").lower()
            text = _text(node)
            if href and (rel == "alternate" or not link):
                link = href
            elif text and not link:
                link = text
        if not link:
            link = _first_text(row, {"guid", "id"})

        published_raw = _first_text(row, {"pubdate", "published", "updated", "date"})
        if not published_raw:
            published_raw = _first_text(row, {"issued", "created"})
        published_dt = parse_date(published_raw)
        published = published_dt.isoformat().replace("+00:00", "Z") if published_dt else None
        summary = _first_text(row, {"summary", "description", "encoded", "content"})
        items.append(
            {
                "source": name,
                "title": title,
                "link": link,
                "url": link,
                "published": published,
                "date": published,
                "summary": summary[:300],
            }
        )
    return items


def _sort_key(item: dict[str, Any]) -> tuple[int, float]:
    parsed = parse_date(item.get("published") or item.get("date"))
    return (1, parsed.timestamp()) if parsed else (0, 0.0)


def _select_round_robin(
    items: list[dict[str, Any]],
    source_order: list[str],
    limit: int,
    per_source: int,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {source: [] for source in source_order}
    for item in items:
        source = str(item.get("source") or "")
        grouped.setdefault(source, []).append(item)

    selected: list[dict[str, Any]] = []
    for index in range(per_source):
        for source in grouped:
            rows = grouped[source]
            if index < len(rows):
                selected.append(rows[index])
                if len(selected) == limit:
                    return selected
    return selected


def filter_sort_dedupe(
    items: list[dict[str, Any]],
    days: int = 3,
    now: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    if days < 0:
        raise ValueError("days must be zero or greater")
    current = now or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    cutoff = current.astimezone(dt.timezone.utc) - dt.timedelta(days=days)
    unique: dict[str, dict[str, Any]] = {}
    for item in items:
        link = str(item.get("link") or item.get("url") or "").strip()
        if not link:
            continue
        published = parse_date(item.get("published") or item.get("date"))
        if published and published < cutoff:
            continue
        if link not in unique:
            row = dict(item)
            row["link"] = link
            row["url"] = link
            row["published"] = (
                published.isoformat().replace("+00:00", "Z") if published else None
            )
            row["date"] = row["published"]
            unique[link] = row
    return sorted(unique.values(), key=_sort_key, reverse=True)


def _load_feeds(path: str | Path | None) -> list[dict[str, Any]]:
    target = Path(path) if path is not None else DEFAULT_FEEDS
    if not target.is_absolute():
        target = Path.cwd() / target
    data = json.loads(target.read_text(encoding="utf-8-sig"))
    feeds = data.get("feeds") if isinstance(data, dict) else None
    if not isinstance(feeds, list):
        raise ValueError("feeds.json must contain a 'feeds' list")
    valid = []
    for row in feeds:
        if not isinstance(row, dict) or not row.get("url"):
            continue
        valid.append(
            {
                "name": str(row.get("name") or row["url"]),
                "url": str(row["url"]),
                "proxy": bool(row.get("proxy", False)),
            }
        )
    return valid


def _fetch_feed(feed: dict[str, Any], timeout: float | tuple[float, float]) -> list[dict[str, Any]]:
    proxies = {"http": PROXY_URL, "https": PROXY_URL} if (feed["proxy"] and PROXY_URL) else {"http": None, "https": None}
    response = requests.get(
        feed["url"],
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
        proxies=proxies,
    )
    response.raise_for_status()
    return parse_feed_xml(response.content, name=feed["name"])


def _fetch_timed_feed(
    feed: dict[str, Any], timeout: float | tuple[float, float]
) -> tuple[list[dict[str, Any]], float, str | None]:
    started = time.monotonic()
    try:
        return _fetch_feed(feed, timeout), time.monotonic() - started, None
    except Exception as exc:
        return [], time.monotonic() - started, _safe_error(exc)


def read_feeds(
    feeds_path: str | Path | None = None,
    days: int = 3,
    limit: int = 50,
    timeout: float | tuple[float, float] = 10.0,
    overall_timeout: float = 13.0,
    per_source: int = 3,
) -> dict[str, Any]:
    """Fetch feeds independently; one malformed or unreachable source is isolated."""
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if per_source < 1:
        raise ValueError("per_source must be at least 1")
    started = time.monotonic()
    feeds = _load_feeds(feeds_path)
    if not feeds:
        return {
            "items": [],
            "errors": [],
            "stats": {"sources": {}, "elapsed_seconds": round(time.monotonic() - started, 3)},
        }

    fetched_items: dict[str, list[dict[str, Any]]] = {}
    errors: list[dict[str, str]] = []
    source_stats: dict[str, dict[str, Any]] = {}
    executor = ThreadPoolExecutor(max_workers=min(len(feeds), 16), thread_name_prefix="rss-feed")
    request_timeout = (min(3.0, timeout), timeout) if isinstance(timeout, (int, float)) else timeout
    futures = {executor.submit(_fetch_timed_feed, feed, request_timeout): feed for feed in feeds}
    done, pending = wait(futures, timeout=overall_timeout)
    for future in done:
        feed = futures[future]
        try:
            rows, elapsed, reason = future.result()
            if reason:
                errors.append({"source": feed["name"], "reason": reason})
                source_stats[feed["name"]] = {
                    "fetched": 0,
                    "kept": 0,
                    "status": "failed",
                    "elapsed_seconds": round(elapsed, 3),
                }
                continue
            fetched_items[feed["name"]] = rows
            source_stats[feed["name"]] = {
                "fetched": len(rows),
                "kept": 0,
                "status": "success",
                "elapsed_seconds": round(elapsed, 3),
            }
        except Exception as exc:
            reason = _safe_error(exc)
            errors.append({"source": feed["name"], "reason": reason})
            source_stats[feed["name"]] = {
                "fetched": 0,
                "kept": 0,
                "status": "failed",
                "elapsed_seconds": round(
                    timeout if isinstance(timeout, (int, float)) else timeout[-1], 3
                ),
            }
    for future in pending:
        feed = futures[future]
        future.cancel()
        errors.append({"source": feed["name"], "reason": f"timed out after {overall_timeout:g} seconds"})
        source_stats[feed["name"]] = {
            "fetched": 0,
            "kept": 0,
            "status": "timeout",
            "elapsed_seconds": round(overall_timeout, 3),
        }
    executor.shutdown(wait=False, cancel_futures=True)

    all_items = [
        item
        for feed in feeds
        for item in fetched_items.get(feed["name"], [])
    ]
    sorted_items = filter_sort_dedupe(all_items, days=days)
    source_order = list(dict.fromkeys(feed["name"] for feed in feeds))
    items = _select_round_robin(sorted_items, source_order, limit, per_source)
    counts: dict[str, int] = {}
    for item in items:
        source = item["source"]
        counts[source] = counts.get(source, 0) + 1
    for name, stat in source_stats.items():
        stat["kept"] = counts.get(name, 0)
    source_stats = {feed["name"]: source_stats[feed["name"]] for feed in feeds}
    errors_by_source = {error["source"]: error for error in errors}
    errors = [errors_by_source[feed["name"]] for feed in feeds if feed["name"] in errors_by_source]
    return {
        "items": items,
        "errors": errors,
        "stats": {
            "sources": source_stats,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        },
    }


def _human_output(payload: dict[str, Any]) -> str:
    lines = []
    for item in payload["items"]:
        stamp = item.get("published") or "未知时间"
        lines.append(f"[{item['source']}] {item['title']} — {item['link']}（{stamp}）")
    sources = payload["stats"]["sources"]
    source_text = ", ".join(
        f"{name}={row['fetched']}/{row['kept']}条（{row['elapsed_seconds']:.3f}s）"
        for name, row in sources.items()
    ) or "无订阅源"
    failed = "; ".join(
        f"{error['source']}: {error['reason']}" for error in payload["errors"]
    ) or "无"
    lines.append(f"源统计：{source_text}")
    lines.append(f"失败源：{failed}")
    lines.append(f"总耗时：{payload['stats']['elapsed_seconds']:.3f}s")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    _configure_console()
    _load_env()
    parser = argparse.ArgumentParser(description="Read RSS 2.0 and Atom feeds.")
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--feeds", default=str(DEFAULT_FEEDS))
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--per-source", type=int, default=3)

    def timeout_value(value: str) -> float:
        try:
            timeout = float(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError("timeout must be a number from 3 to 30") from exc
        if not 3 <= timeout <= 30:
            raise argparse.ArgumentTypeError("timeout must be between 3 and 30 seconds")
        return timeout

    parser.add_argument("--timeout", type=timeout_value, default=10.0)
    args = parser.parse_args(argv)
    try:
        payload = read_feeds(
            args.feeds,
            days=args.days,
            limit=args.limit,
            per_source=args.per_source,
            timeout=args.timeout,
        )
    except Exception as exc:
        print(f"rss_reader: {_safe_error(exc)}", file=sys.stderr)
        return 2
    payload = _redact_payload(payload)
    if args.as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(_human_output(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
