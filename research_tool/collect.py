"""Collect search and feed results from independent sources."""

from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import math
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

try:
    from . import fetch_smart, rss_reader, search_dual
except ImportError:
    import fetch_smart
    import rss_reader
    import search_dual


try:
    from . import config
except ImportError:
    import config

PROXY_URL = config.proxy_url()
_MISSING = Path(os.path.abspath("/__missing__/not-installed"))
BILI_EXE = config.resolve_program(
    "BILI_PATH", "bili", candidates=(str(Path.home() / ".agent-reach-venv" / "Scripts" / "bili.exe"),),
) or _MISSING
YTDLP_EXE = config.resolve_program(
    "YT_DLP_PATH", "yt-dlp", candidates=(str(Path.home() / ".agent-reach-venv" / "Scripts" / "yt-dlp.exe"),),
) or _MISSING


def _proxies() -> dict[str, str] | None:
    return {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None
GITHUB_SEARCH_URL = "https://api.github.com/search/repositories"
GITHUB_ISSUES_URL = "https://api.github.com/search/issues"
GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"
V2EX_SEARCH_URL = "https://www.sov2ex.com/api/search"
SO_SEARCH_URL = "https://api.stackexchange.com/2.3/search/advanced"
HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search"
GROQ_TRANSCRIPT_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
DEFAULT_PLATFORMS = ("web", "bili", "youtube", "hn", "github", "rss")
SUPPORTED_PLATFORMS = frozenset(DEFAULT_PLATFORMS) | {"stats", "podcast", "ghissues", "ghdiscussions", "v2ex", "so"}
CHANNEL_TIMEOUT = 30.0
STATS_CHANNEL_TIMEOUT = 75.0
TOTAL_TIMEOUT = 90.0
TRANSCRIBE_MAX_SECONDS = 120 * 60
TRANSCRIPT_MAX_CHARS = 120_000
TRANSCRIBE_BUDGET_SECONDS = 1200.0
TRANSCRIBE_CHUNK_SECONDS = 8 * 60
TRANSCRIBE_OVERLAP_SECONDS = 1
MAX_AUDIO_BYTES = 25_000_000
USER_AGENT = "HermesCollect/1.0"
SCRIPT_DIR = Path(__file__).resolve().parent
STATS_SOURCES_PATH = SCRIPT_DIR / "stats_sources.json"
_GROQ_SEMAPHORE = threading.BoundedSemaphore(2)


@dataclass
class ChannelResult:
    items: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    partial: bool = False


def _configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def _load_env() -> None:
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
    searxng_url = os.environ.get("SEARXNG_URL", "").strip()
    if searxng_url:
        host = urlsplit(searxng_url).hostname
        if host:
            entries = [part.strip() for part in os.environ.get("NO_PROXY", "").split(",") if part.strip()]
            if host not in entries:
                entries.append(host)
            os.environ["NO_PROXY"] = ",".join(entries)
            os.environ["no_proxy"] = os.environ["NO_PROXY"]


SECRET_ENV_NAMES = (
    "TAVILY_API_KEY", "GROQ_API_KEY", "AGNES_API_KEY", "JINA_API_KEY",
    "GITHUB_TOKEN", "SEARXNG_URL", "RESEARCH_PROXY",
)


def _redact_text(value: Any) -> str:
    message = str(value)
    for name in SECRET_ENV_NAMES:
        secret = os.environ.get(name, "").strip()
        if len(secret) >= 8:
            message = message.replace(secret, "[redacted]")
    return message


def _safe_error(value: Any) -> str:
    message = str(value) or value.__class__.__name__
    return _redact_text(message)[:1000]


def _redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key == "query" and item == "":
                result[key] = ""
            elif key == "body" and value.get("platform") == "stats" and isinstance(item, str):
                result[key] = _redact_text(item)[:20000]
            else:
                result[key] = _redact_payload(item)
        return result
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    if isinstance(value, str):
        # Only redact secrets; do not cap length here (transcripts exceed 1,000 chars).
        return _redact_text(value)
    return value


def _parse_date(value: Any) -> dt.datetime | None:
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


def _published(value: Any) -> str | None:
    parsed = _parse_date(value)
    if parsed:
        return parsed.isoformat().replace("+00:00", "Z")
    if value is None or not str(value).strip():
        return None
    return str(value).strip()


def _number(value: Any) -> int | float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip().replace(",", "").lower()
    if not text:
        return None
    multiplier = 1
    if text.endswith("万"):
        multiplier, text = 10_000, text[:-1]
    elif text.endswith("亿"):
        multiplier, text = 100_000_000, text[:-1]
    try:
        parsed = float(text) * multiplier
    except ValueError:
        return None
    return int(parsed) if parsed.is_integer() else parsed


def _make_item(
    platform: str,
    index: int,
    title: Any,
    url: Any,
    published: Any = None,
    metrics: dict[str, Any] | None = None,
    summary: Any = "",
    **extra: Any,
) -> dict[str, Any] | None:
    title_text = re.sub(r"\s+", " ", str(title or "")).strip()
    url_text = str(url or "").strip()
    if not title_text or not url_text:
        return None
    item = {
        "platform": platform,
        "title": title_text,
        "url": url_text,
        "published": _published(published),
        "metrics": metrics or {},
        "summary": re.sub(r"\s+", " ", str(summary or "")).strip(),
        "source_id": f"{platform}-{index}",
    }
    item.update(extra)
    return item


def _search_web(query: str, limit: int) -> ChannelResult:
    payload = search_dual.search_dual(query, limit=limit)
    items = []
    for index, row in enumerate(payload.get("results", []), start=1):
        item = _make_item(
            "web",
            index,
            row.get("title"),
            row.get("url"),
            row.get("published"),
            summary=row.get("snippet", ""),
        )
        if item:
            item["sources"] = row.get("sources", [])
            if row.get("title_encoding_unreliable"):
                item["title_encoding_unreliable"] = True
            items.append(item)
    errors = [f"{source}: {reason}" for source, reason in payload.get("errors", {}).items()]
    return ChannelResult(items, errors, partial=bool(errors) and len(errors) < 2)


def _direct_env() -> dict[str, str]:
    env = os.environ.copy()
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env.pop(name, None)
    no_proxy = "localhost,127.0.0.1,::1"
    env["NO_PROXY"] = no_proxy
    env["no_proxy"] = no_proxy
    return env


def _bili_available() -> bool:
    return BILI_EXE.is_file()


def _yt_dlp_executable() -> str | None:
    configured = os.environ.get("YTDLP_PATH", "").strip().strip("\"")
    if configured:
        candidate = Path(configured)
        if candidate.is_file():
            return str(candidate)
        resolved = shutil.which(configured)
        if resolved:
            return resolved
    if YTDLP_EXE.is_file():
        return str(YTDLP_EXE)
    return shutil.which("yt-dlp")


def _decode_subprocess(completed: subprocess.CompletedProcess[str]) -> tuple[str, str]:
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", errors="replace")
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    return stdout, stderr


def _bili_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("items", "results", "result", "videos", "archives", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
        if isinstance(value, dict):
            nested = _bili_rows(value)
            if nested:
                return nested
    return []


def _collect_bili(query: str, limit: int) -> list[dict[str, Any]]:
    if not _bili_available():
        raise RuntimeError(f"bili CLI unavailable at {BILI_EXE}")
    command = [str(BILI_EXE), "search", query, "--type", "video", "-n", str(limit), "--json"]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CHANNEL_TIMEOUT - 1,
            check=False,
            env=_direct_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("bili CLI timed out") from exc
    except OSError as exc:
        raise RuntimeError(f"could not start bili CLI: {exc}") from exc
    stdout, stderr = _decode_subprocess(completed)
    if completed.returncode != 0:
        reason = stderr.strip() or stdout.strip() or f"exit code {completed.returncode}"
        raise RuntimeError(f"bili CLI exited with code {completed.returncode}: {reason[:400]}")
    try:
        rows = _bili_rows(json.loads(stdout))
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError("bili CLI returned invalid JSON") from exc
    items = []
    for index, row in enumerate(rows[:limit], start=1):
        bvid = str(row.get("bvid") or row.get("BV") or row.get("id") or "").strip()
        url = row.get("url") or (f"https://www.bilibili.com/video/{bvid}" if bvid else "")
        author = row.get("author") or row.get("owner") or row.get("up_name") or ""
        if isinstance(author, dict):
            author = author.get("name") or author.get("uname") or ""
        play = _number(row.get("play") or row.get("view") or row.get("view_count"))
        published = row.get("pubdate") or row.get("publish_time") or row.get("created_at")
        duration = _duration_seconds(row.get("duration") or row.get("length"))
        item = _make_item(
            "bili",
            index,
            row.get("title"),
            url,
            published,
            metrics={"play": play} if play is not None else {},
            summary=row.get("description") or row.get("desc") or "",
            author=str(author),
            bvid=bvid or None,
            duration_seconds=duration,
        )
        if item:
            items.append(item)
    return items


def _duration_seconds(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    if re.fullmatch(r"\d+(?::\d{1,2}){1,2}", text):
        parts = [int(part) for part in text.split(":")]
        seconds = 0
        for part in parts:
            seconds = seconds * 60 + part
        return seconds
    return None


def _is_audio_url(url: str, content_type: str | None = None) -> bool:
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    if mime.startswith("audio/"):
        return True
    path = urlsplit(str(url or "")).path.lower()
    return Path(path).suffix in {".mp3", ".m4a", ".wav", ".ogg", ".aac"}


def _is_http_url(value: str) -> bool:
    parsed = urlsplit(value)
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


def _audio_content_type(url: str, timeout: float = 8.0) -> str:
    headers = {"User-Agent": USER_AGENT, "Accept": "audio/*,*/*;q=0.8"}
    proxies = _proxies()
    try:
        response = requests.head(
            url, headers=headers, timeout=(min(5.0, timeout), timeout),
            proxies=proxies, allow_redirects=True,
        )
        content_type = str(response.headers.get("Content-Type") or "")
        if content_type or response.status_code not in {405, 501}:
            return content_type
    except requests.RequestException:
        pass
    try:
        response = requests.get(
            url, headers={**headers, "Range": "bytes=0-0"}, stream=True,
            timeout=(min(5.0, timeout), timeout), proxies=proxies,
        )
        content_type = str(response.headers.get("Content-Type") or "")
        response.close()
        return content_type
    except requests.RequestException:
        return ""


def _local_tag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1].lower()


def _child_text(element: ET.Element, name: str) -> str | None:
    for child in element:
        if _local_tag(child) == name.lower():
            text = "".join(child.itertext()).strip()
            return text or None
    return None


def _parse_podcast_feed(xml_text: str | bytes, limit: int | None = None) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_text)
    episodes = []
    for entry in root.iter():
        if _local_tag(entry) != "item":
            continue
        title = _child_text(entry, "title")
        published = _child_text(entry, "pubdate") or _child_text(entry, "published")
        duration_text = _child_text(entry, "duration")
        enclosure_url = None
        episode_url = _child_text(entry, "link")
        for child in entry:
            if _local_tag(child) == "enclosure":
                enclosure_url = child.attrib.get("url")
                if enclosure_url:
                    break
        if not title or not enclosure_url:
            continue
        episodes.append({
            "title": title,
            "published": _published(published),
            "duration_seconds": _duration_seconds(duration_text),
            "audio_url": enclosure_url.strip(),
            "episode_url": episode_url,
            "summary": _child_text(entry, "description") or _child_text(entry, "summary") or "",
        })
    episodes.sort(
        key=lambda row: _parse_date(row.get("published")) or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
        reverse=True,
    )
    return episodes[:limit] if limit is not None else episodes


def _collect_podcast(query: str, limit: int) -> ChannelResult:
    query = query.strip()
    if _is_http_url(query):
        content_type = ""
        if not _is_audio_url(query):
            content_type = _audio_content_type(query)
        if not _is_audio_url(query, content_type):
            return ChannelResult([], ["URL does not point to a supported audio resource"])
        path_name = Path(urlsplit(query).path).stem
        title = path_name or urlsplit(query).hostname or "Audio"
        item = _make_item(
            "podcast", 1, title, query, summary="",
            duration_seconds=None, audio_url=query, audio_content_type=content_type or None,
        )
        return ChannelResult([item] if item else [], [])

    payload = _request_json(
        ITUNES_SEARCH_URL,
        {"term": query, "entity": "podcast", "limit": max(10, min(25, limit * 3))},
        timeout=(5.0, 20.0),
    )
    feeds = []
    seen_feeds = set()
    for row in payload.get("results", []):
        if not isinstance(row, dict):
            continue
        feed_url = str(row.get("feedUrl") or "").strip()
        if not feed_url or feed_url in seen_feeds:
            continue
        seen_feeds.add(feed_url)
        feeds.append((str(row.get("collectionName") or "Podcast"), feed_url))
    if not feeds:
        return ChannelResult([], ["iTunes Search returned no podcast feeds"])

    items = []
    errors = []
    for podcast_title, feed_url in feeds:
        try:
            response = requests.get(
                feed_url,
                headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, application/xml, */*"},
                timeout=(5.0, 18.0),
                proxies=_proxies(),
            )
            response.raise_for_status()
            for episode in _parse_podcast_feed(response.content, limit=limit):
                item = _make_item(
                    "podcast",
                    len(items) + 1,
                    episode["title"],
                    episode["audio_url"],
                    episode.get("published"),
                    summary=episode.get("summary", ""),
                    duration_seconds=episode.get("duration_seconds"),
                    audio_url=episode["audio_url"],
                    episode_url=episode.get("episode_url"),
                    podcast_title=podcast_title,
                    feed_url=feed_url,
                )
                if item:
                    items.append(item)
            if len(items) >= limit:
                break
        except Exception as exc:
            errors.append(f"{podcast_title}: {_safe_error(exc)}")

    items = sort_items(items)[:limit]
    return ChannelResult(items, errors, partial=bool(items) and bool(errors))


def _collect_youtube(query: str, limit: int) -> list[dict[str, Any]]:
    executable = _yt_dlp_executable()
    if not executable:
        raise RuntimeError("yt-dlp unavailable (not installed or not on PATH)")
    output_template = "%(title)s\t%(view_count)s\t%(webpage_url)s\t%(duration)s"
    command = [
        executable,
        "--proxy",
        PROXY_URL,
        "--flat-playlist",
        "--print",
        output_template,
        f"ytsearch{limit}:{query}",
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CHANNEL_TIMEOUT - 1,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("yt-dlp search timed out") from exc
    except OSError as exc:
        raise RuntimeError(f"could not start yt-dlp: {exc}") from exc
    stdout, stderr = _decode_subprocess(completed)
    if completed.returncode != 0:
        reason = stderr.strip() or stdout.strip() or f"exit code {completed.returncode}"
        raise RuntimeError(f"yt-dlp search exited with code {completed.returncode}: {reason[:400]}")
    items = []
    for line in stdout.splitlines():
        fields = line.split("\t")
        if len(fields) < 3:
            continue
        title, views_text, url = fields[:3]
        duration = _duration_seconds(fields[3]) if len(fields) > 3 else None
        views = _number(views_text)
        item = _make_item(
            "youtube",
            len(items) + 1,
            title,
            url,
            metrics={"views": views} if views is not None else {},
            duration_seconds=duration,
        )
        if item:
            items.append(item)
        if len(items) >= limit:
            break
    return items


def _request_json(
    url: str,
    params: dict[str, Any],
    timeout: tuple[float, float] = (5.0, 20.0),
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    request_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    response = requests.get(
        url,
        params=params,
        headers=request_headers,
        timeout=timeout,
        proxies=_proxies(),
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("API returned a non-object JSON response")
    return payload


def _github_headers() -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json"}
    token = (config.get("GITHUB_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _collect_github_issues(query: str, limit: int) -> list[dict[str, Any]]:
    payload = _request_json(
        GITHUB_ISSUES_URL,
        {"q": f"{query} is:issue", "sort": "updated", "order": "desc", "per_page": limit},
        timeout=(5.0, 20.0),
        headers=_github_headers(),
    )
    rows = payload.get("items", [])
    items = []
    for index, row in enumerate(rows[:limit], start=1):
        if not isinstance(row, dict):
            continue
        repository = "/".join((row.get("repository_url") or "").rstrip("/").split("/")[-2:])
        reactions = row.get("reactions") or {}
        item = _make_item(
            "ghissues",
            index,
            row.get("title"),
            row.get("html_url"),
            row.get("updated_at") or row.get("created_at"),
            metrics={
                "comments": _number(row.get("comments")) or 0,
                "reactions": _number(reactions.get("total_count")) or 0,
                "state": row.get("state") or "",
            },
            summary=(row.get("body") or "")[:400],
            repo=repository,
        )
        if item:
            items.append(item)
    return items


def _collect_github_discussions(query: str, limit: int) -> list[dict[str, Any]]:
    token = (config.get("GITHUB_TOKEN") or "").strip()
    if not token:
        raise RuntimeError(
            "GitHub Discussions search needs GITHUB_TOKEN; add one with: "
            "echo '{\"GITHUB_TOKEN\":\"ghp_xxx\"}' | research config set --stdin"
        )
    graphql = """
    query($q: String!, $n: Int!) {
      search(query: $q, type: DISCUSSION, first: $n) {
        nodes {
          ... on Discussion {
            title
            url
            updatedAt
            bodyText
            comments { totalCount }
            repository { nameWithOwner }
          }
        }
      }
    }
    """
    response = requests.post(
        GITHUB_GRAPHQL_URL,
        json={"query": graphql, "variables": {"q": query, "n": limit}},
        headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json",
                 "Authorization": f"Bearer {token}"},
        timeout=(5.0, 20.0),
        proxies=_proxies(),
    )
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, dict) and payload.get("errors"):
        raise RuntimeError("GitHub GraphQL error: " + str(payload["errors"][0].get("message", ""))[:200])
    nodes = (((payload.get("data") or {}).get("search") or {}).get("nodes") or [])
    items = []
    for index, row in enumerate(nodes[:limit], start=1):
        if not isinstance(row, dict):
            continue
        item = _make_item(
            "ghdiscussions",
            index,
            row.get("title"),
            row.get("url"),
            row.get("updatedAt"),
            metrics={"comments": _number((row.get("comments") or {}).get("totalCount")) or 0},
            summary=(row.get("bodyText") or "")[:400],
            repo=(row.get("repository") or {}).get("nameWithOwner", ""),
        )
        if item:
            items.append(item)
    return items


def _collect_v2ex(query: str, limit: int) -> list[dict[str, Any]]:
    payload = _request_json(V2EX_SEARCH_URL, {"q": query, "size": min(max(limit * 10, 20), 50)}, timeout=(5.0, 20.0))
    hits = [hit for hit in (payload.get("hits") or []) if isinstance(hit, dict)]
    hits.sort(key=lambda hit: str((hit.get("_source") or {}).get("created") or ""), reverse=True)
    items = []
    for index, hit in enumerate(hits[:limit], start=1):
        source = hit.get("_source") if isinstance(hit, dict) else None
        if not isinstance(source, dict):
            continue
        topic_id = source.get("id")
        if not topic_id:
            continue
        item = _make_item(
            "v2ex",
            index,
            source.get("title"),
            f"https://www.v2ex.com/t/{topic_id}",
            source.get("created"),
            metrics={"replies": _number(source.get("replies")) or 0},
            summary=(source.get("content") or "")[:300],
            author=source.get("member") or "",
        )
        if item:
            items.append(item)
    return items


def _collect_stackoverflow(query: str, limit: int) -> list[dict[str, Any]]:
    payload = _request_json(
        SO_SEARCH_URL,
        {"order": "desc", "sort": "creation", "q": query,
         "site": "stackoverflow", "pagesize": limit},
        timeout=(5.0, 20.0),
    )
    items = []
    for index, row in enumerate((payload.get("items") or [])[:limit], start=1):
        if not isinstance(row, dict):
            continue
        created = row.get("creation_date")
        published = None
        if isinstance(created, (int, float)):
            published = dt.datetime.fromtimestamp(created, dt.timezone.utc).isoformat().replace("+00:00", "Z")
        item = _make_item(
            "so",
            index,
            row.get("title"),
            row.get("link"),
            published,
            metrics={
                "score": _number(row.get("score")) or 0,
                "answers": _number(row.get("answer_count")) or 0,
                "answered": bool(row.get("is_answered")),
            },
            summary="",
            tags=",".join(row.get("tags") or []),
        )
        if item:
            items.append(item)
    return items


def _collect_hn(query: str, limit: int) -> list[dict[str, Any]]:
    payload = _request_json(HN_SEARCH_URL, {"query": query, "hitsPerPage": limit})
    items = []
    for index, row in enumerate(payload.get("hits", [])[:limit], start=1):
        if not isinstance(row, dict):
            continue
        title = row.get("title") or row.get("story_title")
        url = row.get("url") or row.get("story_url")
        if not url and row.get("objectID"):
            url = f"https://news.ycombinator.com/item?id={row['objectID']}"
        item = _make_item(
            "hn",
            index,
            title,
            url,
            row.get("created_at"),
            metrics={
                "points": _number(row.get("points")) or 0,
                "num_comments": _number(row.get("num_comments")) or 0,
            },
            summary=row.get("story_text") or "",
        )
        if item:
            items.append(item)
    return items


def _collect_github(query: str, limit: int) -> list[dict[str, Any]]:
    payload = _request_json(
        GITHUB_SEARCH_URL,
        {"q": query, "sort": "stars", "per_page": limit},
        timeout=(5.0, 20.0),
    )
    rows = payload.get("items", [])
    items = []
    for index, row in enumerate(rows[:limit], start=1):
        if not isinstance(row, dict):
            continue
        item = _make_item(
            "github",
            index,
            row.get("full_name"),
            row.get("html_url"),
            row.get("pushed_at"),
            metrics={"stars": _number(row.get("stargazers_count")) or 0},
            summary=row.get("description") or "",
        )
        if item:
            items.append(item)
    return items


def _collect_rss(query: str, limit: int, days: int) -> ChannelResult:
    payload = rss_reader.read_feeds(
        SCRIPT_DIR / "feeds.json",
        days=days,
        limit=max(limit * 5, limit),
        timeout=9,
        overall_timeout=12,
    )
    needle = query.casefold()
    selected = []
    for row in payload.get("items", []):
        haystack = f"{row.get('title', '')} {row.get('summary', '')}".casefold()
        if needle not in haystack:
            continue
        item = _make_item(
            "rss",
            len(selected) + 1,
            row.get("title"),
            row.get("link") or row.get("url"),
            row.get("published"),
            summary=row.get("summary", ""),
            source=row.get("source", ""),
        )
        if item:
            selected.append(item)
        if len(selected) >= limit:
            break
    errors = [f"{row['source']}: {row['reason']}" for row in payload.get("errors", [])]
    sources = payload.get("stats", {}).get("sources", {}).values()
    has_successful_source = any(row.get("status") == "success" for row in sources)
    return ChannelResult(
        selected,
        errors,
        partial=bool(errors) and has_successful_source,
    )


def _extract_stats_tables(html_text: str | bytes) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html_text, "html.parser")
    tables = []
    for index, table in enumerate(soup.find_all("table"), start=1):
        table_rows = []
        for row in table.find_all("tr"):
            cells = row.find_all(("th", "td"), recursive=False)
            if cells:
                table_rows.append((any(cell.name == "th" for cell in cells), [
                    cell.get_text(" ", strip=True) for cell in cells
                ]))
        headers_present = bool(table_rows and table_rows[0][0])
        headers = table_rows[0][1] if headers_present else []
        rows = table_rows[1:] if headers_present else table_rows
        tables.append({
            "index": index,
            "headers": headers,
            "rows": [values for _, values in rows[:200]],
        })
    return tables


def _collect_stats(_query: str, _limit: int) -> ChannelResult:
    try:
        sources = json.loads(STATS_SOURCES_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ChannelResult([], [f"stats_sources_missing: {STATS_SOURCES_PATH}"])
    except (OSError, json.JSONDecodeError) as exc:
        return ChannelResult([], [f"stats_error: could not read sources: {_safe_error(exc)}"])
    if not isinstance(sources, list):
        return ChannelResult([], ["stats_error: sources config must be a JSON list"])

    items = []
    errors = []
    for index, source in enumerate(sources, start=1):
        if not isinstance(source, dict):
            errors.append(f"stats_error: source {index} must be an object")
            continue
        name = source.get("name")
        url = source.get("url")
        if not isinstance(name, str) or not name.strip() or not isinstance(url, str) or not url.strip():
            errors.append(f"stats_error: source {index} requires non-empty name and url")
            continue
        if "proxy" in source and type(source["proxy"]) is not bool:
            errors.append(f"stats_error: source {name} proxy must be a boolean")
            continue
        name = name.strip()
        url = url.strip()

        try:
            fetched = fetch_smart.fetch_smart(url, min_chars=200)
            body = str(fetched.get("text") or "")
            proxy_url = PROXY_URL if source.get("proxy") is True else None
            response = None
            last_exc: Exception | None = None
            for _attempt in range(2):
                try:
                    response = requests.get(
                        url,
                        headers={"User-Agent": USER_AGENT},
                        timeout=(10.0, 30.0),
                        proxies={"http": proxy_url, "https": proxy_url},
                    )
                    response.raise_for_status()
                    break
                except Exception as exc:  # noqa: BLE001 - retry once on transient network errors
                    last_exc = exc
                    response = None
                    time.sleep(1.5)
            if response is None:
                raise last_exc if last_exc else RuntimeError("stats request failed")
            tables = _extract_stats_tables(response.content)
            items.append({
                "title": name,
                "url": url,
                "platform": "stats",
                "body": body[:20000],
                "tables": tables,
                "method": fetched.get("method", ""),
                "chars": fetched.get("chars", len(body)),
                "login_wall": fetched.get("login_wall", False),
            })
        except Exception as exc:
            errors.append(f"stats_error: {name}: {_safe_error(exc)}")
    return ChannelResult(items, errors, partial=bool(items) and bool(errors))


def _heat(item: dict[str, Any]) -> float:
    metrics = item.get("metrics") or {}
    for key in ("play", "points", "stars", "views", "view_count"):
        value = _number(metrics.get(key))
        if value is not None:
            return float(value)
    return 0.0


def sort_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def key(item: dict[str, Any]) -> tuple[int, int, float]:
        parsed = _parse_date(item.get("published"))
        if parsed is None:
            return (0, 0, 0.0)
        return (1, parsed.date().toordinal(), _heat(item))

    return sorted(items, key=key, reverse=True)


def _within_days(items: list[dict[str, Any]], days: int) -> list[dict[str, Any]]:
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    kept = []
    for item in items:
        parsed = _parse_date(item.get("published"))
        if parsed is None or parsed >= cutoff:
            kept.append(item)
    return kept


def _status_for(count: int, errors: list[str], timed_out: bool = False) -> str:
    if timed_out:
        return "timeout"
    if not errors:
        return "success"
    if count:
        return "partial"
    if all("unavailable" in error.lower() or "missing required configuration" in error.lower() for error in errors):
        return "unavailable"
    return "failed"


def _run_channel(func: Callable[..., Any], query: str, limit: int, days: int) -> tuple[ChannelResult, float]:
    started = time.monotonic()
    try:
        result = func(query, limit, days) if func is _collect_rss else func(query, limit)
        if isinstance(result, ChannelResult):
            outcome = result
        else:
            outcome = ChannelResult(list(result or []), [])
    except Exception as exc:
        outcome = ChannelResult([], [_safe_error(exc)])
    return outcome, time.monotonic() - started


def _collect_channels(
    query: str,
    limit: int,
    days: int,
    platforms: tuple[str, ...],
    deadline: float,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, str]]:
    functions: dict[str, Callable[..., Any]] = {
        "web": _search_web,
        "bili": _collect_bili,
        "youtube": _collect_youtube,
        "hn": _collect_hn,
        "github": _collect_github,
        "ghissues": _collect_github_issues,
        "ghdiscussions": _collect_github_discussions,
        "v2ex": _collect_v2ex,
        "so": _collect_stackoverflow,
        "rss": _collect_rss,
        "podcast": _collect_podcast,
        "stats": _collect_stats,
    }
    channels: dict[str, Any] = {}
    errors: dict[str, str] = {}
    items_by_channel: dict[str, list[dict[str, Any]]] = {}
    requested = {name for name in platforms}
    executor = ThreadPoolExecutor(max_workers=max(1, len(requested)), thread_name_prefix="collect-channel")
    futures = {
        executor.submit(_run_channel, functions[name], query, limit, days): name
        for name in platforms
    }
    collection_started = time.monotonic()
    remaining = min(CHANNEL_TIMEOUT, max(0.0, deadline - collection_started))
    done, pending = wait(futures, timeout=remaining)
    stats_pending = {future for future in pending if futures[future] == "stats"}
    if stats_pending:
        elapsed = time.monotonic() - collection_started
        stats_remaining = min(
            max(0.0, deadline - time.monotonic()),
            max(0.0, STATS_CHANNEL_TIMEOUT - elapsed),
        )
        stats_done, _ = wait(stats_pending, timeout=stats_remaining)
        done.update(stats_done)
        pending.difference_update(stats_done)
    for future in done:
        name = futures[future]
        try:
            outcome, elapsed = future.result()
            items_by_channel[name] = outcome.items
            channel_errors = [_safe_error(error) for error in outcome.errors]
            if channel_errors:
                errors[name] = "; ".join(channel_errors)
            channels[name] = {
                "count": len(outcome.items),
                "elapsed_seconds": round(elapsed, 3),
                "status": (
                    "partial"
                    if channel_errors and outcome.partial
                    else _status_for(len(outcome.items), channel_errors)
                ),
            }
        except Exception as exc:
            reason = _safe_error(exc)
            errors[name] = reason
            channels[name] = {
                "count": 0,
                "elapsed_seconds": round(remaining, 3),
                "status": _status_for(0, [reason]),
            }
    for future in pending:
        name = futures[future]
        future.cancel()
        timeout = STATS_CHANNEL_TIMEOUT if name == "stats" else CHANNEL_TIMEOUT
        reason = f"timed out after {timeout:g} seconds"
        errors[name] = reason
        channels[name] = {
            "count": 0,
            "elapsed_seconds": round(min(timeout, time.monotonic() - collection_started), 3),
            "status": "timeout",
        }
    executor.shutdown(wait=False, cancel_futures=True)
    for name in platforms:
        if name not in channels:
            channels[name] = {"count": 0, "elapsed_seconds": 0.0, "status": "skipped"}
    ordered_channels = {name: channels[name] for name in platforms}
    ordered_errors = {name: errors[name] for name in platforms if name in errors}
    all_items = [item for name in platforms for item in items_by_channel.get(name, [])]
    return all_items, ordered_channels, ordered_errors


def _clean_subtitles(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""
    lines = []
    previous = None
    for line in text.splitlines():
        line = html_unescape(line.strip())
        if not line or line.upper() == "WEBVTT" or line.startswith(("NOTE", "Kind:", "Language:")):
            continue
        if "-->" in line or re.fullmatch(r"\d+", line):
            continue
        line = re.sub(r"<[^>]+>", "", line)
        line = re.sub(r"\s+", " ", line).strip()
        if line and line != previous:
            lines.append(line)
            previous = line
    return " ".join(lines).strip()


def html_unescape(text: str) -> str:
    import html

    return html.unescape(text)


def _download_subtitle(executable: str, url: str, basename: Path, timeout: float) -> str | None:
    output = f"{basename}.subtitle.%(ext)s"
    command = [
        executable,
        "--proxy",
        PROXY_URL,
        "--no-playlist",
        "--skip-download",
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs",
        "zh-Hans,zh,en",
        "--sub-format",
        "srt/best",
        "--convert-subs",
        "srt",
        "--output",
        output,
        "--quiet",
        "--no-warnings",
        url,
    ]
    try:
        subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(0.1, timeout),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    for path in sorted(basename.parent.glob(f"{basename.name}.subtitle.*")):
        if path.suffix.lower() in {".vtt", ".srt", ".ass", ".lrc"}:
            transcript = _clean_subtitles(path)
            if transcript:
                return transcript
    return None


def _download_audio(
    executable: str,
    url: str,
    basename: Path,
    timeout: float,
    *,
    enforce_limit: bool = True,
    max_seconds: int | None = None,
) -> Path:
    output = f"{basename}.audio.%(ext)s"
    download_deadline = time.monotonic() + max(0.1, timeout)
    bitrates = (48, 32) if enforce_limit else (48,)
    for bitrate in bitrates:
        remaining = download_deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("audio download timed out")
        command = [
            executable,
            "--proxy",
            PROXY_URL,
            "--no-playlist",
            "-f",
            "bestaudio",
            "-x",
            "--audio-format",
            "mp3",
            "--postprocessor-args",
            f"ExtractAudio:-b:a {bitrate}k -ac 1 -ar 16000",
            "--output",
            output,
            "--quiet",
            "--no-warnings",
        ]
        if max_seconds is not None:
            hours, rest = divmod(max_seconds, 3600)
            minutes, seconds = divmod(rest, 60)
            end = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
            command.extend(["--download-sections", f"*00:00:00-{end}"])
        command.append(url)
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=max(0.1, remaining),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("audio download timed out") from exc
        except OSError as exc:
            raise RuntimeError(f"could not start yt-dlp: {exc}") from exc
        if completed.returncode != 0:
            raise RuntimeError(f"audio download failed with exit code {completed.returncode}")
        files = [
            path for path in basename.parent.glob(f"{basename.name}.audio.*")
            if path.is_file() and not path.name.endswith(".part")
        ]
        if not files:
            raise RuntimeError("yt-dlp produced no audio file")
        audio_path = max(files, key=lambda path: path.stat().st_size)
        if not enforce_limit or audio_path.stat().st_size <= MAX_AUDIO_BYTES:
            return audio_path
        if bitrate == 32:
            raise RuntimeError("audio exceeds Groq's 25 MB upload limit")
        for path in files:
            try:
                path.unlink()
            except OSError as exc:
                raise RuntimeError(f"could not remove oversized audio before retry: {exc}") from exc
    raise RuntimeError("audio exceeds Groq's 25 MB upload limit")


def _download_direct_audio(url: str, basename: Path, timeout: float) -> tuple[Path, str]:
    remaining = max(0.1, timeout)
    response = requests.get(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "audio/*,*/*;q=0.8"},
        stream=True,
        timeout=(min(10.0, remaining), remaining),
        proxies=_proxies(),
    )
    try:
        response.raise_for_status()
        content_type = str(response.headers.get("Content-Type") or "")
        if not _is_audio_url(url, content_type):
            raise RuntimeError("audio URL returned a non-audio response")
        extension = Path(urlsplit(url).path).suffix.lower()
        if extension not in {".mp3", ".m4a", ".wav", ".ogg", ".aac"}:
            extension = {
                "audio/mpeg": ".mp3",
                "audio/mp4": ".m4a",
                "audio/wav": ".wav",
                "audio/x-wav": ".wav",
                "audio/ogg": ".ogg",
                "audio/aac": ".aac",
            }.get(content_type.split(";", 1)[0].strip().lower(), ".audio")
        audio_path = basename.with_suffix(extension)
        downloaded = 0
        download_deadline = time.monotonic() + remaining
        with audio_path.open("wb") as output:
            for block in response.iter_content(chunk_size=256 * 1024):
                if time.monotonic() >= download_deadline:
                    raise RuntimeError("audio download timed out")
                if block:
                    output.write(block)
                    downloaded += len(block)
        if downloaded == 0:
            raise RuntimeError("audio URL returned an empty response")
        return audio_path, content_type
    except Exception:
        for path in basename.parent.glob(f"{basename.name}*"):
            try:
                path.unlink()
            except OSError:
                pass
        raise
    finally:
        response.close()


def _ffmpeg_executable() -> str | None:
    return shutil.which("ffmpeg")


def _probe_audio_duration(audio_path: Path, timeout: float = 15.0) -> int | None:
    executable = shutil.which("ffprobe")
    if not executable:
        return None
    try:
        completed = subprocess.run(
            [
                executable, "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(0.1, timeout),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    try:
        duration = float(completed.stdout.strip())
    except (TypeError, ValueError):
        return None
    return max(1, math.ceil(duration)) if duration > 0 else None


def _detect_silence_points(audio_path: Path, duration_seconds: int, deadline: float) -> list[float]:
    executable = _ffmpeg_executable()
    remaining = deadline - time.monotonic()
    if not executable or remaining <= 0:
        return []
    try:
        completed = subprocess.run(
            [
                executable, "-hide_banner", "-nostats", "-i", str(audio_path),
                "-t", str(duration_seconds), "-af", "silencedetect=noise=-35dB:d=0.5",
                "-f", "null", "-",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(0.1, min(60.0, remaining)),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if completed.returncode != 0:
        return []
    return [
        float(match.group(1))
        for match in re.finditer(r"silence_(?:start|end):\s*([0-9]+(?:\.[0-9]+)?)", completed.stderr)
    ]


def _plan_audio_chunks(
    duration_seconds: int | float,
    silence_points: list[float] | tuple[float, ...] = (),
    chunk_seconds: int = TRANSCRIBE_CHUNK_SECONDS,
    overlap_seconds: int = TRANSCRIBE_OVERLAP_SECONDS,
    max_seconds: int = TRANSCRIBE_MAX_SECONDS,
) -> list[dict[str, int]]:
    duration = min(max_seconds, max(0, int(float(duration_seconds))))
    if duration <= 0:
        return []
    plans = []
    previous_boundary = 0
    target = chunk_seconds
    while target <= duration:
        boundary = target
        candidates = [
            point for point in silence_points
            if target - 30 <= point <= target + 30
            and previous_boundary < point <= duration
        ]
        if candidates:
            boundary = round(min(candidates, key=lambda point: abs(point - target)))
        boundary = min(duration, max(previous_boundary + 1, boundary))
        plans.append({
            "start_seconds": max(0, previous_boundary - overlap_seconds),
            "end_seconds": boundary,
        })
        previous_boundary = boundary
        target += chunk_seconds
    if previous_boundary < duration:
        plans.append({
            "start_seconds": max(0, previous_boundary - overlap_seconds),
            "end_seconds": duration,
        })
    return plans


def _cut_audio_chunk(
    source: Path, destination: Path, start_seconds: int, end_seconds: int, timeout: float
) -> None:
    executable = _ffmpeg_executable()
    if not executable:
        raise RuntimeError("ffmpeg unavailable for audio chunking")
    length = end_seconds - start_seconds
    if length <= 0:
        raise RuntimeError("audio chunk has an invalid duration")
    completed = subprocess.run(
        [
            executable, "-hide_banner", "-loglevel", "error", "-ss", str(start_seconds),
            "-i", str(source), "-t", str(length), "-vn", "-ac", "1", "-ar", "16000",
            "-b:a", "48k", "-y", str(destination),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=max(0.1, timeout),
        check=False,
    )
    if completed.returncode != 0 or not destination.is_file() or destination.stat().st_size == 0:
        reason = completed.stderr.strip() or "ffmpeg did not create an audio chunk"
        raise RuntimeError(reason[:400])
    if destination.stat().st_size > MAX_AUDIO_BYTES:
        raise RuntimeError("audio chunk exceeds Groq's 25 MB upload limit")


def _groq_transcribe(audio_path: Path, api_key: str, timeout: float) -> str:
    if audio_path.stat().st_size > MAX_AUDIO_BYTES:
        raise RuntimeError("audio chunk exceeds Groq's 25 MB upload limit")
    acquired = _GROQ_SEMAPHORE.acquire(timeout=max(0.1, timeout))
    if not acquired:
        raise RuntimeError("Groq concurrency wait timed out")
    try:
        with audio_path.open("rb") as audio_file:
            response = requests.post(
                GROQ_TRANSCRIPT_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                data={"model": "whisper-large-v3"},
                files={"file": (audio_path.name, audio_file, "application/octet-stream")},
                timeout=(max(0.1, min(10.0, timeout)), max(0.1, timeout)),
                proxies=_proxies(),
            )
        if not 200 <= response.status_code < 300:
            raise RuntimeError(f"Groq transcription returned HTTP {response.status_code}")
        payload = response.json()
        transcript = str(payload.get("text") or "").strip()
        if not transcript:
            raise RuntimeError("Groq returned an empty transcript")
        return transcript
    finally:
        _GROQ_SEMAPHORE.release()


def _transcribe_one_chunk(
    source: Path,
    plan: dict[str, int],
    index: int,
    chunk_dir: Path,
    api_key: str,
    deadline: float,
) -> tuple[str | None, str | None, bool]:
    last_error = "audio chunk failed"
    for attempt in range(2):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, _transcribe_budget_error(deadline), True
        chunk_path = chunk_dir / f"chunk-{index:03d}.mp3"
        try:
            _cut_audio_chunk(
                source,
                chunk_path,
                plan["start_seconds"],
                plan["end_seconds"],
                min(120.0, remaining),
            )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, _transcribe_budget_error(deadline), True
            transcript = _groq_transcribe(chunk_path, api_key, remaining)
            return transcript, None, False
        except Exception as exc:
            last_error = _safe_error(exc)
            if deadline - time.monotonic() <= 0:
                return None, _transcribe_budget_error(deadline), True
            if attempt == 1:
                return None, last_error, False
    return None, last_error, False


def _remove_transcript_overlap(previous: str, current: str) -> str:
    maximum = min(300, len(previous), len(current))
    for overlap in range(maximum, 2, -1):
        if previous[-overlap:] != current[:overlap]:
            continue
        matched_words = re.findall(r"[A-Za-z0-9\u4e00-\u9fff]", current[:overlap])
        if len(matched_words) >= 3:
            return current[overlap:].lstrip(" \t,.;:!?，。；：！？、")
    return current


def _format_transcript_offset(seconds: int) -> str:
    minutes, remainder = divmod(max(0, seconds), 60)
    return f"[{minutes:02d}:{remainder:02d}]"


def _merge_chunk_transcripts(chunks: list[tuple[str, int]]) -> str:
    merged = []
    previous = ""
    for transcript, offset in chunks:
        text = re.sub(r"\s+", " ", str(transcript or "")).strip()
        if not text:
            continue
        if previous:
            text = _remove_transcript_overlap(previous, text)
        if text:
            merged.append(f"{_format_transcript_offset(offset)} {text}")
            previous = re.sub(r"\s+", " ", str(transcript or "")).strip()
    return "\n".join(merged)


def _transcribe_audio_chunks(
    audio_path: Path,
    duration_seconds: int,
    api_key: str,
    deadline: float,
) -> dict[str, Any]:
    duration_limit = min(duration_seconds, TRANSCRIBE_MAX_SECONDS)
    truncated = duration_seconds > TRANSCRIBE_MAX_SECONDS
    silence_points = _detect_silence_points(audio_path, duration_limit, deadline)
    plans = _plan_audio_chunks(duration_limit, silence_points)
    if not plans:
        return {
            "transcript": None, "chunk_count": 0, "partial": False,
            "timed_out": False, "truncated": truncated, "error": "audio has no usable duration",
        }

    results: dict[int, tuple[str | None, str | None, bool]] = {}
    with tempfile.TemporaryDirectory(prefix="collect-audio-chunks-") as temporary_dir:
        chunk_dir = Path(temporary_dir)
        executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="collect-groq")
        futures = {
            executor.submit(
                _transcribe_one_chunk, audio_path, plan, index, chunk_dir, api_key, deadline
            ): index
            for index, plan in enumerate(plans, start=1)
        }
        _done, pending = wait(futures, timeout=max(0.0, deadline - time.monotonic()))
        timed_out = False
        errors = []
        for future, index in futures.items():
            if future in pending:
                future.cancel()
                results[index] = (None, _transcribe_budget_error(deadline), True)
                timed_out = True
                errors.append(f"chunk {index}: {_transcribe_budget_error(deadline)}")
                continue
            try:
                results[index] = future.result()
            except Exception as exc:
                results[index] = (None, _safe_error(exc), False)
            _transcript, error, chunk_timed_out = results[index]
            if chunk_timed_out:
                timed_out = True
            if error:
                errors.append(f"chunk {index}: {error}")
        executor.shutdown(wait=True, cancel_futures=True)

    completed = [
        (transcript, plans[index - 1]["start_seconds"])
        for index, (transcript, _error, _timed_out) in sorted(results.items())
        if transcript
    ]
    transcript = _merge_chunk_transcripts(completed) or None
    return {
        "transcript": transcript,
        "chunk_count": len(plans),
        "partial": bool(errors) or timed_out,
        "timed_out": timed_out,
        "truncated": truncated,
        "error": "; ".join(errors)[:1000] or None,
    }


def _transcribe_item(
    item: dict[str, Any], deadline: float
) -> tuple[str | None, str | None, str | None]:
    duration = _duration_seconds(item.get("duration_seconds"))
    is_audio = (
        item.get("platform") == "podcast"
        or bool(item.get("audio_url"))
        or _is_audio_url(str(item.get("url") or ""), item.get("audio_content_type"))
    )
    item.update({"chunk_count": 0, "partial": False, "timed_out": False})
    url = str(item.get("audio_url") or item.get("url") or "")
    if not is_audio and duration is None:
        return None, None, "duration is unavailable; transcription skipped"
    executable = None if is_audio else _yt_dlp_executable()
    if not is_audio and not executable:
        return None, None, "yt-dlp unavailable for subtitles/audio"
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        item["timed_out"] = True
        return None, None, _transcribe_budget_error(deadline)
    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if is_audio and not api_key:
        return None, None, "Groq API key unavailable"

    with tempfile.TemporaryDirectory(prefix="collect-transcribe-") as temporary_dir:
        basename = Path(temporary_dir) / "source"
        try:
            if is_audio:
                audio_path, _content_type = _download_direct_audio(url, basename, remaining)
            else:
                if duration is not None and duration <= TRANSCRIBE_MAX_SECONDS:
                    subtitle = _download_subtitle(executable, url, basename, min(12.0, remaining))
                    if subtitle:
                        return subtitle[:TRANSCRIPT_MAX_CHARS], "subtitle", None
                if not api_key:
                    return None, None, "Groq API key unavailable"
                download_limit = TRANSCRIBE_MAX_SECONDS if duration and duration > TRANSCRIBE_MAX_SECONDS else None
                audio_path = _download_audio(
                    executable,
                    url,
                    basename,
                    max(0.1, deadline - time.monotonic()),
                    enforce_limit=False,
                    max_seconds=download_limit,
                )
            probed_duration = _probe_audio_duration(
                audio_path, timeout=max(0.1, min(15.0, deadline - time.monotonic()))
            )
            if probed_duration is not None:
                duration = probed_duration
            if duration is None:
                return None, None, "duration is unavailable; transcription skipped"
            item["duration_seconds"] = duration
            outcome = _transcribe_audio_chunks(audio_path, duration, api_key, deadline)
            item["chunk_count"] = outcome["chunk_count"]
            item["partial"] = outcome["partial"]
            item["timed_out"] = outcome["timed_out"]
            if outcome["truncated"]:
                item["truncated"] = "duration_limit"
            return outcome["transcript"], "groq" if outcome["transcript"] else None, outcome["error"]
        except Exception as exc:
            return None, None, _safe_error(exc)


def _collection_timeout(transcribe: bool) -> float:
    if not transcribe:
        return TOTAL_TIMEOUT
    return max(TOTAL_TIMEOUT, TRANSCRIBE_BUDGET_SECONDS + 60.0)


def _transcribe_budget_error(deadline: float) -> str:
    remaining = max(0.0, deadline - time.monotonic())
    limit = TRANSCRIBE_BUDGET_SECONDS
    return f"transcribe budget exhausted ({remaining:.1f}s remaining; {limit:.0f}s overall limit)"


def run_collection(
    query: str,
    limit: int = 5,
    days: int = 7,
    platforms: tuple[str, ...] | list[str] = DEFAULT_PLATFORMS,
    transcribe: bool = False,
) -> dict[str, Any]:
    _load_env()
    started = time.monotonic()
    deadline = started + _collection_timeout(transcribe)
    selected_platforms = tuple(platform for platform in platforms if platform in SUPPORTED_PLATFORMS)
    items, channels, errors = _collect_channels(
        query, limit, days, selected_platforms, deadline
    )
    podcast_items = [item for item in items if item.get("platform") == "podcast"]
    dated_items = _within_days(
        [item for item in items if item.get("platform") != "podcast"], days
    )
    items = podcast_items + dated_items
    if transcribe:
        pending: list[dict[str, Any]] = []
        for item in items:
            is_audio = (
                item.get("platform") == "podcast"
                or bool(item.get("audio_url"))
                or _is_audio_url(str(item.get("url") or ""), item.get("audio_content_type"))
            )
            if item.get("platform") not in {"youtube", "bili"} and not is_audio:
                continue
            item.setdefault("duration_seconds", None)
            item["chunk_count"] = 0
            item["partial"] = False
            item["timed_out"] = False
            item.setdefault("transcribe_error", None)
            item.setdefault("transcript_source", None)
            duration = _duration_seconds(item.get("duration_seconds"))
            if duration is None and not is_audio:
                item["transcribed"] = False
                item["transcribe_error"] = "duration is unavailable; transcription skipped"
            else:
                if duration is not None and duration > TRANSCRIBE_MAX_SECONDS:
                    item["truncated"] = "duration_limit"
                pending.append(item)

        executor = ThreadPoolExecutor(max_workers=2)
        futures = {
            executor.submit(
                _transcribe_item,
                item,
                min(deadline, time.monotonic() + TRANSCRIBE_BUDGET_SECONDS),
            ): item
            for item in pending
        }
        done, _ = wait(futures, timeout=max(0.0, deadline - time.monotonic()))
        for future, item in futures.items():
            if future not in done:
                item["transcribed"] = False
                item["partial"] = True
                item["timed_out"] = True
                item["transcribe_error"] = _transcribe_budget_error(deadline)
                continue
            try:
                transcript, source, error = future.result()
                item["transcribed"] = transcript is not None
                if transcript is not None:
                    item["summary"] = transcript[:TRANSCRIPT_MAX_CHARS]
                    item["transcript_source"] = source
                if error:
                    item["transcribe_error"] = _safe_error(error)
                else:
                    item["transcribe_error"] = None
            except Exception as exc:
                item["transcribed"] = False
                item["transcribe_error"] = _safe_error(exc)
        executor.shutdown(wait=False, cancel_futures=True)
    items = sort_items(items)
    final_counts: dict[str, int] = {}
    for item in items:
        platform = item["platform"]
        final_counts[platform] = final_counts.get(platform, 0) + 1
    for name, row in channels.items():
        row["count"] = final_counts.get(name, 0)
    elapsed = time.monotonic() - started
    return {
        "query": query,
        "items": items,
        "stats": {
            "channels": channels,
            "total_count": len(items),
            "elapsed_seconds": round(elapsed, 3),
        },
        "errors": errors,
    }


def _format_heat(item: dict[str, Any]) -> str:
    metrics = item.get("metrics") or {}
    for key, label in (("play", "播放"), ("points", "points"), ("stars", "stars"), ("views", "views")):
        value = metrics.get(key)
        if value is not None:
            return f"{label} {value}"
    return "热度 0"


def _human_output(payload: dict[str, Any], platforms: tuple[str, ...]) -> str:
    lines = []
    items_by_platform: dict[str, list[dict[str, Any]]] = {}
    for item in payload["items"]:
        items_by_platform.setdefault(item["platform"], []).append(item)
    for platform in platforms:
        for item in items_by_platform.get(platform, []):
            stamp = item.get("published") or "未知时间"
            lines.append(
                f"[{platform}] {item['title']} — {item['url']}（{stamp} / {_format_heat(item)}）"
            )
    for item in payload["items"]:
        if "transcribed" not in item:
            continue
        if item["transcribed"]:
            lines.append(f"transcribed=true ({item.get('transcript_source', 'unknown')}) {item['title']}")
        elif item.get("transcribe_error"):
            lines.append(f"transcribe_error={item['transcribe_error']} {item['title']}")
        elif item.get("transcribe_skipped"):
            lines.append(f"transcribe_skipped={item['transcribe_skipped']} {item['title']}")
    stats = payload["stats"]
    channels = stats["channels"]
    channel_text = ", ".join(
        f"{name}={row['count']}条/{row['elapsed_seconds']:.3f}s({row['status']})"
        for name, row in channels.items()
    ) or "无"
    failures = "; ".join(f"{name}: {reason}" for name, reason in payload["errors"].items()) or "无"
    lines.append(f"渠道统计：{channel_text}")
    lines.append(f"失败渠道：{failures}")
    lines.append(f"总耗时：{stats['elapsed_seconds']:.3f}s")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    _configure_console()
    _load_env()
    parser = argparse.ArgumentParser(description="Collect results from web, video, podcasts, HN, GitHub and RSS.")
    parser.add_argument("query")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--platforms", default=",".join(DEFAULT_PLATFORMS))
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument(
        "--transcribe",
        action="store_true",
        help="transcribe audio and videos with Groq, up to 120 minutes per item",
    )
    parser.add_argument("--out", metavar="FILE")
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("--limit must be at least 1")
    if args.days < 0:
        parser.error("--days must be zero or greater")
    platforms = tuple(dict.fromkeys(part.strip().lower() for part in args.platforms.split(",") if part.strip()))
    unknown = sorted(set(platforms) - SUPPORTED_PLATFORMS)
    if unknown:
        parser.error("unknown platform(s): " + ", ".join(unknown))
    if args.transcribe:
        try:
            from . import capabilities
        except ImportError:
            import capabilities
        ready, _detail = capabilities._probe(capabilities.BY_KEY["GROQ_API_KEY"])
        if not ready and "GROQ_API_KEY" not in capabilities.declined():
            message = capabilities.BY_KEY["GROQ_API_KEY"].asked_as
            if args.as_json:
                print(json.dumps({
                    "ok": False,
                    "error": {"code": 6, "message": message, "key": "GROQ_API_KEY",
                              "how_to_fix": capabilities.BY_KEY["GROQ_API_KEY"].how_to_get,
                              "decline_command": "research config decline --key GROQ_API_KEY --reason \"用户无法提供\""},
                }, ensure_ascii=False, indent=2))
            else:
                print(f"collect: 需要用户提供 GROQ_API_KEY 才能转写。{message}", file=sys.stderr)
            return 6

    try:
        payload = run_collection(
            args.query,
            limit=args.limit,
            days=args.days,
            platforms=platforms,
            transcribe=args.transcribe,
        )
    except Exception as exc:
        print(f"collect: {_safe_error(exc)}", file=sys.stderr)
        return 2
    payload = _redact_payload(payload)
    json_text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out:
        try:
            Path(args.out).write_text(json_text + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"collect: could not write output file: {_safe_error(exc)}", file=sys.stderr)
            return 2
    if args.as_json:
        print(json_text)
    else:
        print(_human_output(payload, platforms))
        if args.out:
            print(f"JSON 已写入：{args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
