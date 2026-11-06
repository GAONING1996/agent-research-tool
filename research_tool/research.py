"""Tree-based research collection with explicit review by the calling agent."""

from __future__ import annotations

import argparse

try:
    from . import capabilities, config, topic_brief
except ImportError:
    import capabilities
    import config
    import topic_brief
import difflib
import hashlib
import json
import math
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
import uuid
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests


SCRIPT_DIR = Path(__file__).resolve().parent
STATE_VERSION = 4
SCHEMA_VERSION = 1
DEFAULT_MAX_DEPTH = 3
MIN_LAYERS = 4
DEFAULT_TIMEOUT = 120.0
BROWSER_LOCK_TIMEOUT = 120.0
COLLECTOR_SLOTS = 4
FETCH_TIMEOUT = 45
DEFAULT_FETCH_COUNT = 6
DEFAULT_PLATFORMS = ("web", "bili", "youtube")
DEFAULT_CHANNELS = ("web",)
RESEARCH_CHANNELS = {"web", "bili", "youtube", "hn", "github", "rss", "stats", "browser"}
OPENCLI_PATH = config.resolve_program(
    "OPENCLI_PATH", "opencli",
    candidates=(str(Path.home() / "AppData" / "Local" / "hermes" / "node" / "opencli"),),
) or Path(os.path.abspath("/__missing__/not-installed"))
OPENCLI_NODE = OPENCLI_PATH.parent / "node.exe"
OPENCLI_ENTRY = OPENCLI_PATH.parent / "node_modules/@jackwener/opencli/dist/src/main.js"
COLLECT_PLATFORMS = {"bili", "youtube", "hn", "github", "rss", "stats"}
BROWSER_PLATFORMS = {"xhs", "zhihu", "reddit"}
BROWSER_PLATFORM_ORDER = ("xhs", "zhihu", "reddit")
SUPPORTED_PLATFORMS = COLLECT_PLATFORMS | BROWSER_PLATFORMS | {"web"}
VERDICTS = {"answered", "partial", "not_found"}
NODE_STATUSES = {"open", "awaiting_review", *VERDICTS, "closed_unresolved"}
TERMINAL_NODE_STATUSES = NODE_STATUSES - {"open", "awaiting_review"}
NODE_KINDS = {"content", "verification"}
STATUS_TRANSITIONS = {
    "run": {"from": {"open"}, "to": {"awaiting_review"}},
    "apply-review": {"from": {"awaiting_review"}, "to": TERMINAL_NODE_STATUSES},
    "reset": {"from": NODE_STATUSES, "to": {"open"}},
}
CLAIM_REF_RE = re.compile(r"E\d+\Z")
CLAIM_ID_RE = re.compile(r"c\d+\Z")
GAP_ID_RE = re.compile(r"g\d+\Z")
INLINE_REF_RE = re.compile(r"\[E(\d+)\]")
GAP_TYPES = {"depth", "evidence", "quant", "mechanism", "timeliness", "conflict"}
CLAIM_SUPPORTS = {"single", "dual", "contested"}
DEFAULT_PLATFORM_OPTIONS = {"detail": 0, "images": 0, "vision": 0}
EXIT_CODES = {
    0: "success",
    2: "parameter or validation error",
    3: "operation is not allowed for the current state",
    4: "collection or network failure",
}
SPAM_TITLE_RE = re.compile(r"黑料|吃瓜|成人|短剧|同人|小说|裸|约炮|彩票|返利|私服|外挂")
SPAM_DOMAIN_MARKERS = ("mingxhal1", "57duanju", "juvmzfmte", "37e0", "51漫画", "hhl", "八卦")


class BrowserLockTimeout(TimeoutError):
    """The browser collector is already in use by another process."""


class ResearchCLIError(Exception):
    def __init__(
        self, message: str, *, code: int = 2, hint: str = "", next_command: str = "",
        errors: list[str] | None = None, metadata: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.hint = hint
        self.next_command = next_command
        self.errors = list(errors or [])
        self.metadata = dict(metadata or {})


class ResearchArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ResearchCLIError(message)


class FileLock:
    """Small cross-process exclusive lock using the platform's file locking API."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.handle: Any = None

    def acquire(self, timeout: float | None = None, poll: float = 0.05) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        self.handle.seek(0, os.SEEK_END)
        if self.handle.tell() == 0:
            self.handle.write(b"\0")
            self.handle.flush()
        started = time.monotonic()
        while True:
            self.handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except (OSError, BlockingIOError):
                if timeout is not None and time.monotonic() - started >= timeout:
                    self.handle.close()
                    self.handle = None
                    return False
                time.sleep(poll)

    def release(self) -> None:
        if self.handle is None:
            return
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None

    def __enter__(self) -> "FileLock":
        if not self.acquire():
            raise TimeoutError(f"could not acquire lock: {self.path}")
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.release()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _default_state_path(query: str) -> Path:
    token = hashlib.sha256(query.encode("utf-8")).hexdigest()[:12]
    return config.data_dir() / f"research_{token}.json"


def _state_lock_path(path: Path) -> Path:
    return Path(str(path) + ".lock")


def _browser_lock_path(path: Path) -> Path:
    return Path(str(path) + ".browser.lock")


def _collector_slot_paths(path: Path) -> list[Path]:
    return [Path(f"{path}.collector.{index}.lock") for index in range(COLLECTOR_SLOTS)]


SECRET_ENV_NAMES = (
    "TAVILY_API_KEY", "GROQ_API_KEY", "AGNES_API_KEY", "JINA_API_KEY",
    "GITHUB_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY",
    "SEARXNG_URL", "RESEARCH_PROXY",
)


def _safe_text(value: Any) -> str:
    text = str(value or "")
    for name in SECRET_ENV_NAMES:
        secret = (os.environ.get(name) or "").strip()
        if len(secret) >= 8:
            text = text.replace(secret, "[redacted]")
    return text


def _safe_tree(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _safe_tree(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe_tree(item) for item in value]
    if isinstance(value, str):
        return _safe_text(value)
    return value


def canonicalize_url(url: str) -> str:
    """Return a stable HTTP URL identity without fragments or tracking parameters."""
    candidate = str(url or "").strip()
    if not candidate:
        raise ValueError("empty URL")
    if not urlsplit(candidate).scheme:
        candidate = "https://" + candidate
    parts = urlsplit(candidate)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ValueError(f"unsupported URL: {candidate}")
    if any(char.isspace() or ord(char) < 33 for char in parts.hostname):
        raise ValueError(f"invalid URL host: {candidate}")
    if ":" not in parts.hostname:
        try:
            parts.hostname.encode("idna")
        except UnicodeError as exc:
            raise ValueError(f"invalid URL host: {candidate}") from exc
    if parts.username or parts.password:
        raise ValueError("URLs with embedded credentials are not supported")
    hostname = parts.hostname.lower()
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"invalid URL port: {candidate}") from exc
    netloc = hostname if port in {None, 80, 443} else f"{hostname}:{port}"
    query = sorted(
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"}
    )
    path = parts.path.rstrip("/")
    return urlunsplit(("https", netloc, path, urlencode(query, doseq=True), ""))


def _host(canonical: str) -> str | None:
    try:
        return urlsplit(canonical).hostname
    except ValueError:
        return None


def _iso_date(value: Any, field: str = "version date") -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"{field} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError(f"{field} must be YYYY-MM-DD") from exc


def _normalize_version_dates(values: Any) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, (list, tuple)):
        raise ValueError("version_dates must be a list of YYYY-MM-DD dates")
    return sorted({_iso_date(value) for value in values})


def _version_age_note(state: dict[str, Any], published: Any) -> str:
    published_date = None
    if isinstance(published, str) and re.match(r"^\d{4}-\d{2}-\d{2}", published):
        try:
            published_date = date.fromisoformat(published[:10])
        except ValueError:
            pass
    if published_date is None:
        return "无日期"

    version_dates = _normalize_version_dates(state.get("version_dates", []))
    if not version_dates:
        return "未设置版本日期"
    notes = []
    for index, reference_date in enumerate(date.fromisoformat(value) for value in version_dates):
        delta = (published_date - reference_date).days
        if delta < 0:
            note = f"早于版本 {-delta} 天"
        elif delta > 0:
            note = f"晚于版本 {delta} 天"
        else:
            note = "与版本日期相同"
        details = [reference_date.isoformat()]
        if index == len(version_dates) - 1 and state.get("version_label"):
            details.append(str(state["version_label"]))
        notes.append(f"{note}（{'；'.join(details)}）")
    return "；".join(notes)


def _new_state(
    query: str,
    max_depth: int,
    platforms: tuple[str, ...],
    platform_options: dict[str, int] | None = None,
    channels: tuple[str, ...] | None = None,
    version_dates: list[str] | tuple[str, ...] | None = None,
    version_label: str | None = None,
    purpose: str = "",
    deliverable: str = "",
) -> dict[str, Any]:
    stamp = _now()
    return {
        "state_version": STATE_VERSION,
        "root": "n0",
        "max_depth": max_depth,
        "version_dates": _normalize_version_dates(version_dates),
        "version_label": version_label.strip() if isinstance(version_label, str) and version_label.strip() else None,
        "purpose": purpose,
        "deliverable": deliverable,
        "platforms": list(platforms),
        "channels": list(channels if channels is not None else DEFAULT_CHANNELS),
        "platform_options": dict(platform_options or DEFAULT_PLATFORM_OPTIONS),
        "created_at": stamp,
        "updated_at": stamp,
        "nodes": [{
            "id": "n0",
            "kind": "content",
            "parent": None,
            "question": query,
            "context": "",
            "acceptance": [],
            "scope_delta": "",
            "depth": 0,
            "status": "open",
            "channels": [],
            "queries": [],
            "queries_configured": False,
            "counter_queries": [],
            "evidence_ids": [],
            "dropped": 0,
            "dropped_spam": 0,
            "result": "",
            "review": {
                "verdict": "",
                "rationale": "",
                "unresolved": [],
                "early_close": False,
                "early_close_reason": None,
                "reviewed_at": None,
                "gaps": [],
            },
        }],
        "edges": [],
        "items": [],
        "claims": [],
        "claim_seq": 0,
        "task_runs": [],
    }


def _validate_state(state: Any) -> dict[str, Any]:
    if not isinstance(state, dict) or state.get("state_version") != STATE_VERSION:
        raise ValueError(f"state_version must be {STATE_VERSION}")
    if not isinstance(state.get("nodes"), list) or not isinstance(state.get("edges"), list):
        raise ValueError("state has invalid nodes or edges")
    if not isinstance(state.get("items"), list) or not isinstance(state.get("max_depth"), int):
        raise ValueError("state has invalid items or max_depth")
    if not isinstance(state.get("claims", []), list):
        raise ValueError("state has invalid claims")
    state.setdefault("claims", [])
    for field in ("purpose", "deliverable"):
        value = state.setdefault(field, "")
        if not isinstance(value, str):
            raise ValueError(f"state has invalid {field}")
    task_runs = state.get("task_runs", [])
    if not isinstance(task_runs, list):
        raise ValueError("state has invalid task_runs")
    for task_run in task_runs:
        if not isinstance(task_run, dict):
            raise ValueError("state has invalid task_runs")
        if not all(isinstance(task_run.get(name), str) for name in (
            "task_id", "node_id", "worker", "returned_at", "worker_note",
        )):
            raise ValueError("state has invalid task_runs")
        if any(type(task_run.get(name)) is not int or task_run[name] < 0 for name in (
            "source_count", "body_count", "body_chars_total",
        )):
            raise ValueError("state has invalid task_runs")
        if not isinstance(task_run.get("diagnostics"), list) or not all(
            isinstance(value, str) for value in task_run["diagnostics"]
        ):
            raise ValueError("state has invalid task_runs")
    state["task_runs"] = task_runs
    state["version_dates"] = _normalize_version_dates(state.get("version_dates", []))
    version_label = state.get("version_label")
    if version_label is not None and not isinstance(version_label, str):
        raise ValueError("state has invalid version_label")
    state["version_label"] = version_label.strip() if isinstance(version_label, str) and version_label.strip() else None
    claim_numbers = [
        int(match.group(1))
        for claim in state["claims"] if isinstance(claim, dict)
        if (match := re.fullmatch(r"c(\d+)", str(claim.get("id", ""))))
    ]
    claim_seq = state.get("claim_seq")
    if type(claim_seq) is not int or claim_seq < max(claim_numbers, default=0):
        state["claim_seq"] = max(claim_numbers, default=0)
    channels = state.get("channels", [])
    if not isinstance(channels, list) or not all(
        isinstance(channel, str) and channel in RESEARCH_CHANNELS for channel in channels
    ):
        raise ValueError("state has invalid channels")
    state.setdefault("channels", [])
    if not any(isinstance(node, dict) and node.get("id") == state.get("root") for node in state["nodes"]):
        raise ValueError("state root node is missing")
    options = state.get("platform_options")
    if not isinstance(options, dict):
        options = {}
    state["platform_options"] = {
        name: options.get(name)
        if type(options.get(name)) is int and options.get(name) >= 0
        else default
        for name, default in DEFAULT_PLATFORM_OPTIONS.items()
    }
    for node in state["nodes"]:
        if isinstance(node, dict):
            review = node.get("review")
            if not isinstance(review, dict):
                review = {}
                node["review"] = review
            gaps = review.setdefault("gaps", [])
            if not isinstance(gaps, list):
                raise ValueError(f"node {node.get('id')} review.gaps must be a list")
            node.setdefault("dropped", 0)
            node.setdefault("dropped_spam", 0)
            node.setdefault("acceptance", [])
            node.setdefault("channels", [])
            node.setdefault("queries", [])
            node.setdefault("counter_queries", [])
            node.setdefault("queries_configured", False)
            if not isinstance(node["channels"], list) or not all(
                isinstance(channel, str) and channel in RESEARCH_CHANNELS for channel in node["channels"]
            ):
                raise ValueError(f"node {node.get('id')} has invalid channels")
            for name in ("queries", "counter_queries"):
                if not isinstance(node[name], list) or not all(isinstance(value, str) for value in node[name]):
                    raise ValueError(f"node {node.get('id')} has invalid {name}")
            if type(node["queries_configured"]) is not bool:
                node["queries_configured"] = False
            if node.get("status") not in NODE_STATUSES:
                node["status"] = "awaiting_review"
            if node.get("kind") not in NODE_KINDS:
                node["kind"] = "content"
            for name in ("evidence", "eligible", "hosts_eligible", "filtered"):
                node.pop(name, None)
            _fetch_stats(node)
    _upgrade_state_item_contract(state)
    return state


def _read_state_unlocked(path: Path) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid state JSON: {exc}") from exc
    return _validate_state(state)


def read_state(path: Path | str) -> dict[str, Any]:
    state_path = Path(path)
    with FileLock(_state_lock_path(state_path)):
        return _read_state_unlocked(state_path)


def _write_state_unlocked(path: Path, state: dict[str, Any]) -> None:
    _validate_state(state)
    state["updated_at"] = _now()
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(_safe_tree(state), ensure_ascii=False, indent=2) + "\n"
    temp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", delete=False,
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp",
        ) as handle:
            temp_name = handle.name
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def _load_brief_file(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    try:
        payload = json.loads(target.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ValueError(f"brief file not found: {target}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"brief file is not valid JSON: {target}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("brief must be a JSON object")
    return payload


def _brief_task_lines(brief: Any) -> list[str]:
    if not isinstance(brief, dict):
        return []
    aliases = [str(item.get("value") if isinstance(item, dict) else item).strip()
               for item in (brief.get("aliases") or [])]
    aliases = [alias for alias in aliases if alias]
    exclusions = [str(item).strip() for item in (brief.get("exclusions") or []) if str(item).strip()]
    lines = ["## 主题解析卡"]
    canonical = str(brief.get("canonical") or "").strip()
    if canonical:
        lines.append(f"- 正规名：{canonical}")
    if aliases:
        lines.append("- 别名：" + "、".join(aliases[:12]))
        lines.append("- 查询要求：至少覆盖两种叫法（正规名与别名/英文名各一条）。")
    if exclusions:
        lines.append("- 排除（防同名污染）：" + "、".join(exclusions[:8]))
    for perspective in (brief.get("perspectives") or [])[:5]:
        if isinstance(perspective, dict) and perspective.get("name"):
            questions = "；".join(str(q) for q in (perspective.get("questions") or [])[:3])
            lines.append(f"- 视角 {perspective['name']}：{questions or '（未写问题）'}")
    return lines if len(lines) > 1 else []


def init_state(
    query: str,
    state_path: Path | str | None = None,
    max_depth: int = DEFAULT_MAX_DEPTH,
    platforms: tuple[str, ...] = DEFAULT_PLATFORMS,
    platform_options: dict[str, int] | None = None,
    channels: tuple[str, ...] | None = None,
    version_dates: list[str] | tuple[str, ...] | None = None,
    version_label: str | None = None,
    purpose: str = "",
    deliverable: str = "",
    brief: dict[str, Any] | None = None,
) -> dict[str, Any]:
    query = str(query).strip()
    if not query:
        raise ValueError("root question cannot be empty")
    if not isinstance(purpose, str) or not isinstance(deliverable, str):
        raise ValueError("--purpose and --deliverable must be text")
    if max_depth < 0:
        raise ValueError("--max-depth cannot be negative")
    version_dates = _normalize_version_dates(version_dates)
    if version_label is not None and not isinstance(version_label, str):
        raise ValueError("--version-label must be text")
    invalid = sorted(set(platforms) - SUPPORTED_PLATFORMS)
    if not platforms or invalid:
        raise ValueError("unsupported or empty platforms" + (": " + ", ".join(invalid) if invalid else ""))
    if channels is not None:
        invalid_channels = sorted(set(channels) - RESEARCH_CHANNELS)
        if not channels or invalid_channels:
            raise ValueError(
                "unsupported or empty channels" + (": " + ", ".join(invalid_channels) if invalid_channels else "")
            )
    options = dict(DEFAULT_PLATFORM_OPTIONS)
    if platform_options is not None:
        if not isinstance(platform_options, dict):
            raise ValueError("platform_options must be an object")
        for name, value in platform_options.items():
            if name not in options or type(value) is not int or value < 0:
                raise ValueError(f"invalid platform option: {name}")
            options[name] = value
    if brief is not None:
        report = topic_brief.validate(brief)
        if not report["ok"]:
            raise ValueError("brief is incomplete: " + ", ".join(report["missing"]))
        brief_purpose = str(brief.get("purpose") or "").strip()
        brief_deliverable = str(brief.get("deliverable") or "").strip()
        if purpose.strip() and brief_purpose and purpose.strip() != brief_purpose:
            raise ValueError("--purpose 与 brief.purpose 不一致；只保留一个来源。")
        if deliverable.strip() and brief_deliverable and deliverable.strip() != brief_deliverable:
            raise ValueError("--deliverable 与 brief.deliverable 不一致；只保留一个来源。")
        purpose = purpose.strip() or brief_purpose
        deliverable = deliverable.strip() or brief_deliverable

    path = Path(state_path) if state_path is not None else _default_state_path(query)
    with FileLock(_state_lock_path(path)):
        if path.exists():
            raise FileExistsError(f"state file already exists: {path}")
        state = _new_state(
            query, max_depth, tuple(dict.fromkeys(platforms)), options,
            tuple(dict.fromkeys(channels)) if channels is not None else None,
            version_dates, version_label, purpose, deliverable,
        )
        if brief is not None:
            state["topic_brief"] = brief
        _write_state_unlocked(path, state)
    return state


def set_node(
    state_path: Path | str,
    node_id: str,
    *,
    channels: tuple[str, ...] | None = None,
    queries: list[str] | None = None,
    counter_queries: list[str] | None = None,
    kind: str = "content",
    scroll: int | None = None,
) -> dict[str, Any]:
    path = Path(state_path)
    if kind not in NODE_KINDS:
        raise ValueError("--kind must be content or verification")
    if scroll is not None and not 0 <= int(scroll) <= 10:
        raise ValueError("--scroll must be between 0 and 10")
    if channels is not None:
        invalid = sorted(set(channels) - RESEARCH_CHANNELS)
        if invalid:
            raise ValueError("unsupported channels: " + ", ".join(invalid))
    for name, values in (("queries", queries), ("counter_queries", counter_queries)):
        if values is not None and not all(isinstance(value, str) for value in values):
            raise ValueError(f"{name} must contain strings")
    with FileLock(_state_lock_path(path)):
        state = _read_state_unlocked(path)
        node = _node_index(state).get(node_id)
        if node is None:
            raise ValueError(f"unknown node: {node_id}")
        node["kind"] = kind
        if channels is not None:
            node["channels"] = list(dict.fromkeys(channels))
        if queries is not None:
            node["queries"] = list(queries)
            node["queries_configured"] = bool(queries)
        if scroll is not None:
            node["scroll"] = int(scroll)
        if counter_queries is not None:
            node["counter_queries"] = list(counter_queries)
        _write_state_unlocked(path, state)
    return read_state(path)


def _node_index(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {node["id"]: node for node in state["nodes"]}


def _transition_node_status(node: dict[str, Any], command: str, target: str) -> None:
    transition = STATUS_TRANSITIONS[command]
    current = node.get("status")
    if current not in transition["from"] or target not in transition["to"]:
        raise ValueError(f"invalid node status transition for {command}: {current} -> {target}")
    node["status"] = target


def _next_numbered_id(rows: list[dict[str, Any]], prefix: str) -> str:
    numbers = []
    for row in rows:
        match = re.fullmatch(re.escape(prefix) + r"(\d+)", str(row.get("id", "")))
        if match:
            numbers.append(int(match.group(1)))
    return f"{prefix}{max(numbers, default=0) + 1}"


def _normal_question(question: str) -> str:
    value = unicodedata.normalize("NFKC", question).casefold()
    return "".join(char for char in value if char.isalnum())


def _same_question(first: str, second: str) -> bool:
    left = _normal_question(first)
    right = _normal_question(second)
    if not left or not right:
        return False
    if left == right:
        return True
    if min(len(left), len(right)) >= 8 and difflib.SequenceMatcher(None, left, right).ratio() >= 0.92:
        return True
    return False


def _ancestor_questions(state: dict[str, Any], node_id: str) -> list[str]:
    nodes = _node_index(state)
    current = nodes[node_id]
    questions = []
    while current:
        questions.append(str(current.get("question") or ""))
        parent_id = current.get("parent")
        current = nodes.get(parent_id) if parent_id else None
    return questions


def _unpack_reviews(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        reviews = payload
    elif isinstance(payload, dict) and isinstance(payload.get("reviews"), list):
        reviews = payload["reviews"]
    elif isinstance(payload, dict) and payload.get("node_id"):
        reviews = [payload]
    elif isinstance(payload, dict):
        reviews = [dict(value, node_id=key) for key, value in payload.items() if isinstance(value, dict)]
    else:
        raise ValueError("reviews file must contain a review object or list")
    if not reviews or not all(isinstance(row, dict) for row in reviews):
        raise ValueError("reviews file must contain at least one review object")
    return reviews


def _citation_map(state: dict[str, Any], node_id: str) -> dict[str, str]:
    data = show_data(state, node_id, chars=0)
    return {f"E{index}": str(item["id"]) for index, item in enumerate(data["evidence"], 1)}


def _child_text_refs(value: Any) -> list[str]:
    if isinstance(value, str):
        return [f"E{number}" for number in INLINE_REF_RE.findall(value)]
    if isinstance(value, list):
        return [ref for item in value for ref in _child_text_refs(item)]
    if isinstance(value, dict):
        return [
            ref
            for key, item in value.items()
            if key != "question"
            for ref in _child_text_refs(item)
        ]
    return []


def _review_refs(raw: dict[str, Any], node_id: str) -> list[str]:
    declared = raw.get("citations", [])
    if not isinstance(declared, list) or not all(isinstance(value, str) for value in declared):
        raise ValueError(f"node {node_id} citations must be a list of strings")
    refs = list(declared)
    for name in ("result", "rationale"):
        value = raw.get(name, "")
        if isinstance(value, str):
            refs.extend(f"E{number}" for number in INLINE_REF_RE.findall(value))
    refs.extend(_child_text_refs(raw.get("children", [])))
    return list(dict.fromkeys(refs))


def _bad_review_refs(state: dict[str, Any], node_id: str, refs: list[str]) -> list[dict[str, Any]]:
    local = _citation_map(state, node_id)
    other_nodes: dict[str, list[str]] = {}
    for other_id in _node_index(state):
        if other_id == node_id:
            continue
        for ref in _citation_map(state, other_id):
            other_nodes.setdefault(ref, []).append(other_id)
    bad = []
    for ref in refs:
        if not CLAIM_REF_RE.fullmatch(ref):
            bad.append({"ref": ref, "reason": "invalid_format"})
        elif ref not in local:
            reason = "belongs_to_other_node" if ref in other_nodes else "missing"
            record = {"ref": ref, "reason": reason}
            if reason == "belongs_to_other_node":
                record["nodes"] = other_nodes[ref]
            bad.append(record)
    return bad


def _validate_review_gaps(
    state: dict[str, Any], node_id: str, raw_gaps: Any,
) -> list[dict[str, Any]]:
    if not isinstance(raw_gaps, list) or not all(isinstance(gap, dict) for gap in raw_gaps):
        raise ValueError(f"node {node_id} gaps must be a list of objects")
    evidence_refs = set(_citation_map(state, node_id))
    claim_refs = {
        str(claim.get("id")) for claim in state.get("claims", [])
        if isinstance(claim, dict) and claim.get("id")
    }
    seen: set[str] = set()
    gaps = []
    for gap in raw_gaps:
        gap_id = gap.get("id")
        gap_type = gap.get("type")
        why = gap.get("why")
        target = gap.get("target")
        questions = gap.get("questions")
        if not isinstance(gap_id, str) or not GAP_ID_RE.fullmatch(gap_id):
            raise ValueError(f"node {node_id} gap id must match g\\d+")
        if gap_id in seen:
            raise ValueError(f"node {node_id} has duplicate gap id {gap_id}")
        seen.add(gap_id)
        if not isinstance(gap_type, str) or gap_type not in GAP_TYPES:
            raise ValueError(f"node {node_id} gap {gap_id} type is invalid")
        if not isinstance(why, str) or not why.strip():
            raise ValueError(f"node {node_id} gap {gap_id} why must be non-empty")
        if not isinstance(target, str) or not (
            (CLAIM_REF_RE.fullmatch(target) and target in evidence_refs)
            or (CLAIM_ID_RE.fullmatch(target) and target in claim_refs)
        ):
            raise ValueError(f"node {node_id} gap {gap_id} target must reference local evidence or an existing claim")
        if not isinstance(questions, list) or not all(isinstance(question, str) for question in questions):
            raise ValueError(f"node {node_id} gap {gap_id} questions must be a list of strings")
        normalized_questions = [question.strip() for question in questions if question.strip()]
        if not normalized_questions:
            raise ValueError(f"node {node_id} gap {gap_id} needs at least one non-empty question")
        gaps.append({
            "id": gap_id,
            "type": gap_type,
            "why": why.strip(),
            "target": target,
            "questions": normalized_questions,
        })
    return gaps


def _validate_reviews(
    state: dict[str, Any], reviews: list[dict[str, Any]], *, allow_early_close: bool = False,
) -> list[dict[str, Any]]:
    nodes = _node_index(state)
    validated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in reviews:
        node_id = str(raw.get("node_id") or raw.get("id") or "")
        if node_id not in nodes:
            raise ValueError(f"unknown node: {node_id or '(missing node_id)'}")
        if node_id in seen:
            raise ValueError(f"duplicate review for node {node_id}")
        seen.add(node_id)
        node = nodes[node_id]
        if node.get("status") not in STATUS_TRANSITIONS["apply-review"]["from"]:
            raise ValueError(f"node {node_id} is not awaiting_review")
        verdict = str(raw.get("verdict") or "")
        if verdict not in VERDICTS:
            raise ValueError(f"node {node_id} verdict must be answered, partial, or not_found")
        result = raw.get("result", "")
        rationale = raw.get("rationale", "")
        unresolved = raw.get("unresolved", [])
        children = raw.get("children", [])
        early_close_reason = raw.get("early_close_reason", "")
        if not isinstance(result, str) or not isinstance(rationale, str):
            raise ValueError(f"node {node_id} result and rationale must be strings")
        if not isinstance(early_close_reason, str):
            raise ValueError(f"node {node_id} early_close_reason must be a string")
        if not isinstance(unresolved, list) or not all(isinstance(value, str) for value in unresolved):
            raise ValueError(f"node {node_id} unresolved must be a list of strings")
        if not isinstance(children, list) or not all(isinstance(child, dict) for child in children):
            raise ValueError(f"node {node_id} children must be a list of objects")
        if len(children) > 3:
            raise ValueError(f"node {node_id} has more than 3 children")
        if verdict == "answered" and children:
            raise ValueError(f"answered node {node_id} cannot have children")
        early_close = (
            verdict in {"partial", "not_found"}
            and int(node["depth"]) + 1 < MIN_LAYERS
            and not children
        )
        if early_close and not allow_early_close:
            raise ValueError(f"未达到 {MIN_LAYERS} 层，请继续拆或显式放弃")
        if early_close and not early_close_reason.strip():
            raise ValueError("--allow-early-close requires early_close_reason")
        if int(node["depth"]) >= state["max_depth"] and verdict != "answered" and children:
            raise ValueError(f"node {node_id} reached max_depth; children are not allowed")
        for child in children:
            question = child.get("question")
            scope_delta = child.get("scope_delta")
            context = child.get("context", "")
            acceptance = child.get("acceptance", [])
            if not isinstance(question, str) or not question.strip():
                raise ValueError(f"node {node_id} child question must be a non-empty self-contained string")
            if not isinstance(scope_delta, str) or not scope_delta.strip():
                raise ValueError(f"node {node_id} child scope_delta must be a non-empty string")
            if not isinstance(context, str):
                raise ValueError(f"node {node_id} child context must be a string")
            if not isinstance(acceptance, list) or not all(isinstance(value, str) for value in acceptance):
                raise ValueError(f"node {node_id} child acceptance must be a list of strings")
            if any(_same_question(question, ancestor) for ancestor in _ancestor_questions(state, node_id)):
                raise ValueError(f"子问题“{question}”与祖先重复")
        citations = _review_refs(raw, node_id)
        bad_refs = _bad_review_refs(state, node_id, citations)
        if bad_refs:
            raise ValueError("bad_refs: " + json.dumps(bad_refs, ensure_ascii=False))
        gaps = _validate_review_gaps(state, node_id, raw.get("gaps", []))
        validated.append({
            "node_id": node_id,
            "verdict": verdict,
            "result": result,
            "rationale": rationale,
            "citations": citations,
            "unresolved": list(unresolved),
            "gaps": gaps,
            "children": children,
            "early_close": early_close,
            "early_close_reason": early_close_reason.strip() if early_close else None,
        })
    return validated


def apply_reviews(
    state_path: Path | str, reviews_payload: Any, *, allow_early_close: bool = False,
) -> dict[str, Any]:
    path = Path(state_path)
    reviews = _unpack_reviews(reviews_payload)
    with FileLock(_state_lock_path(path)):
        state = _read_state_unlocked(path)
        validated = _validate_reviews(state, reviews, allow_early_close=allow_early_close)
        nodes = _node_index(state)
        uncited: list[dict[str, Any]] = []
        for review_row in validated:
            node = nodes[review_row["node_id"]]
            verdict = review_row["verdict"]
            cited = set(review_row["citations"])
            uncited_refs = [ref for ref in _citation_map(state, node["id"]) if ref not in cited]
            if uncited_refs:
                uncited.append({"node_id": node["id"], "refs": uncited_refs})
            node["result"] = review_row["result"]
            node["review"] = {
                "verdict": verdict,
                "rationale": review_row["rationale"],
                "citations": review_row["citations"],
                "unresolved": review_row["unresolved"],
                "early_close": review_row["early_close"],
                "early_close_reason": review_row["early_close_reason"],
                "reviewed_at": _now(),
                "gaps": review_row["gaps"],
            }
            target_status = (
                "closed_unresolved"
                if node["depth"] >= state["max_depth"] and verdict == "partial"
                else verdict
            )
            _transition_node_status(node, "apply-review", target_status)
            for child_data in review_row["children"]:
                child_id = _next_numbered_id(state["nodes"], "n")
                child = {
                    "id": child_id,
                    "kind": "content",
                    "parent": node["id"],
                    "question": child_data["question"].strip(),
                    "context": child_data.get("context", ""),
                    "acceptance": list(child_data.get("acceptance", [])),
                    "scope_delta": child_data["scope_delta"].strip(),
                    "depth": int(node["depth"]) + 1,
                    "status": "open",
                    "channels": [],
                    "queries": [],
                    "queries_configured": False,
                    "counter_queries": [],
                    "evidence_ids": [],
                    "dropped": 0,
                    "dropped_spam": 0,
                    "result": "",
                    "review": {
                        "verdict": "",
                        "rationale": "",
                        "unresolved": [],
                        "early_close": False,
                        "early_close_reason": None,
                        "reviewed_at": None,
                        "gaps": [],
                    },
                }
                state["nodes"].append(child)
                nodes[child_id] = child
                state["edges"].append({"from": node["id"], "to": child_id, "kind": "split"})
        _write_state_unlocked(path, state)
        state["uncited"] = uncited
    return state


# ---------------------------------------------------------------------------
# Gap rounds: after a wave the agent declares what is still missing, the tool
# records it, can turn each gap into a child node, and keeps it visible until
# the agent marks it covered or dropped with a reason. No scoring, no auto-close.

GAP_STATUSES = ("open", "covered", "dropped")


def _gap_rounds(state: dict[str, Any]) -> list[dict[str, Any]]:
    rounds = state.get("gap_rounds")
    if not isinstance(rounds, list):
        rounds = []
        state["gap_rounds"] = rounds
    return rounds


def _all_gaps(state: dict[str, Any]) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    for row in _gap_rounds(state):
        for gap in row.get("gaps") or []:
            if isinstance(gap, dict):
                gaps.append(gap)
    return gaps


def gap_observations(state: dict[str, Any]) -> dict[str, Any]:
    """Mechanical facts an agent can use to decide what is missing."""
    nodes = _node_index(state)
    items = state.get("items") or []

    alias_rows: list[dict[str, Any]] = []
    brief = state.get("topic_brief")
    if isinstance(brief, dict):
        mentions = topic_brief.mentions(brief, items)
        for row in (mentions or {}).get("by_alias", []):
            if int(row.get("title_hits") or 0) + int(row.get("body_hits") or 0) == 0:
                alias_rows.append({"alias": row["alias"], "note": "该叫法在所有证据里 0 命中；可能写错，也可能确实没人这么叫。"})

    empty_nodes = [
        {"node_id": node["id"], "question": node.get("question", ""), "status": node.get("status")}
        for node in nodes.values()
        if node.get("status") in {"open", "partial"} and not node.get("evidence_ids")
    ]

    perspectives = []
    if isinstance(brief, dict):
        questions = " ".join(str(node.get("question") or "") + " " + str(node.get("context") or "") for node in nodes.values()).casefold()
        for row in brief.get("perspectives") or []:
            name = str((row or {}).get("name") or "").strip()
            if name and name.casefold() not in questions:
                perspectives.append({"name": name, "note": "这个视角目前没有任何节点在问，但要不要成枝由你判断。"})

    open_rows = [gap for gap in _all_gaps(state) if gap.get("status") == "open"]
    return {
        "rounds": len(_gap_rounds(state)),
        "open_gaps": open_rows,
        "facts": {
            "aliases_without_hits": alias_rows,
            "nodes_without_evidence": empty_nodes,
            "perspectives_without_node": perspectives,
            "evidence_total": len(items),
        },
        "note": "这些只是事实观察，不是缺口清单；缺什么、补不补由你判断。",
    }


def record_gaps(state_path: Path | str, gaps: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(gaps, list) or not gaps:
        raise ValueError("gaps must be a non-empty list")
    path = Path(state_path)
    with FileLock(_state_lock_path(path)):
        state = _read_state_unlocked(path)
        rounds = _gap_rounds(state)
        number = len(rounds) + 1
        existing_ids = {str(gap.get("id")) for gap in _all_gaps(state)}
        recorded = []
        for index, raw in enumerate(gaps, start=1):
            if not isinstance(raw, dict):
                raise ValueError(f"gap #{index} must be an object")
            question = str(raw.get("next_question") or raw.get("question") or "").strip()
            what = str(raw.get("what") or "").strip()
            if not question and not what:
                raise ValueError(f"gap #{index} needs next_question or what")
            counter = len(existing_ids)
            gap_id = f"g{counter + 1}"
            while gap_id in existing_ids:
                counter += 1
                gap_id = f"g{counter + 1}"
            gap = {
                "id": gap_id,
                "what": what or question,
                "why": str(raw.get("why") or "").strip(),
                "kind": str(raw.get("kind") or "query").strip() or "query",
                "angle": str(raw.get("angle") or "").strip(),
                "next_question": question or what,
                "queries": [str(q) for q in (raw.get("queries") or []) if str(q).strip()],
                "acceptance": [str(a) for a in (raw.get("acceptance") or []) if str(a).strip()],
                "parent": str(raw.get("parent") or "n0"),
                "node_id": None,
                "status": "open",
                "note": "",
                "created_at": _now(),
            }
            existing_ids.add(gap_id)
            recorded.append(gap)
        rounds.append({"round": number, "created_at": _now(), "gaps": recorded})
        _write_state_unlocked(path, state)
    return {"round": number, "gaps": recorded}


def _find_gap(state: dict[str, Any], gap_id: str) -> dict[str, Any]:
    for gap in _all_gaps(state):
        if str(gap.get("id")) == gap_id:
            return gap
    raise ValueError(f"unknown gap: {gap_id}")


def resolve_gap(state_path: Path | str, gap_id: str, status: str, note: str = "") -> dict[str, Any]:
    if status not in {"covered", "dropped"}:
        raise ValueError("--status must be covered or dropped")
    if status == "dropped" and not note.strip():
        raise ValueError("dropped 需要 --note 说明为什么放弃")
    path = Path(state_path)
    with FileLock(_state_lock_path(path)):
        state = _read_state_unlocked(path)
        gap = _find_gap(state, gap_id)
        gap["status"] = status
        gap["note"] = note.strip()
        gap["resolved_at"] = _now()
        _write_state_unlocked(path, state)
    return gap


def spawn_gaps(state_path: Path | str, gap_ids: list[str] | None = None) -> dict[str, Any]:
    """Turn recorded, unresolved gaps into child nodes. One child per gap."""
    path = Path(state_path)
    with FileLock(_state_lock_path(path)):
        state = _read_state_unlocked(path)
        nodes = _node_index(state)
        targets = [gap for gap in _all_gaps(state) if gap.get("status") == "open" and not gap.get("node_id")]
        if gap_ids:
            wanted = set(gap_ids)
            targets = [gap for gap in targets if str(gap.get("id")) in wanted]
            missing = wanted - {str(gap.get("id")) for gap in targets}
            if missing:
                raise ValueError("no spawnable gap: " + ", ".join(sorted(missing)))
        if not targets:
            raise ValueError("没有可派生的缺口：先 gaps --file 记录，再 --spawn")

        created = []
        for gap in targets:
            parent = nodes.get(str(gap.get("parent") or "n0"))
            if parent is None:
                parent = nodes.get("n0")
            if parent is None:
                raise ValueError("状态里没有根节点 n0")
            if int(parent.get("depth") or 0) + 1 > int(state.get("max_depth") or 3):
                raise ValueError(f"节点 {parent['id']} 已到最大层数，缺口 {gap['id']} 不能再派生；把它写进父节点结论或标记 dropped。")
            child_id = _next_numbered_id(state["nodes"], "n")
            child = {
                "id": child_id,
                "kind": "content",
                "parent": parent["id"],
                "question": gap["next_question"],
                "context": chr(10).join(part for part in [
                    f"缺口：{gap['what']}",
                    f"原因：{gap['why']}" if gap["why"] else "",
                    f"角度：{gap['angle']}" if gap["angle"] else "",
                ] if part),
                "acceptance": gap["acceptance"] or ["Collect direct evidence that closes this gap."],
                "scope_delta": gap["angle"] or gap["kind"],
                "depth": int(parent.get("depth") or 0) + 1,
                "status": "open",
                "channels": [],
                "queries": list(gap["queries"]),
                "queries_configured": bool(gap["queries"]),
                "counter_queries": [],
                "evidence_ids": [],
                "dropped": 0,
                "dropped_spam": 0,
                "result": "",
                "review": {
                    "verdict": "", "rationale": "", "unresolved": [], "early_close": False,
                    "early_close_reason": None, "reviewed_at": None, "gaps": [],
                },
            }
            state["nodes"].append(child)
            nodes[child_id] = child
            state["edges"].append({"from": parent["id"], "to": child_id, "kind": "gap"})
            gap["node_id"] = child_id
            created.append({"gap": gap["id"], "node_id": child_id, "question": child["question"]})
        _write_state_unlocked(path, state)
    return {"created": created}


def _acquire_collector_slot(path: Path, deadline: float) -> FileLock | None:
    slots = _collector_slot_paths(path)
    while time.monotonic() < deadline:
        for slot_path in slots:
            lock = FileLock(slot_path)
            if lock.acquire(timeout=0):
                return lock
        time.sleep(0.03)
    return None


def _command_json(
    command: list[str],
    state_path: Path,
    deadline: float,
    *,
    browser: bool = False,
) -> tuple[dict[str, Any] | None, str | None, bool]:
    if time.monotonic() >= deadline:
        return None, "node hard timeout reached", False
    browser_lock: FileLock | None = None
    slot_lock: FileLock | None = None
    if browser:
        browser_lock = FileLock(_browser_lock_path(state_path))
        lock_timeout = min(BROWSER_LOCK_TIMEOUT, max(0.0, deadline - time.monotonic()))
        if not browser_lock.acquire(timeout=lock_timeout):
            raise BrowserLockTimeout("browser lock wait timed out")
    try:
        slot_lock = _acquire_collector_slot(state_path, deadline)
        if slot_lock is None:
            return None, "collector concurrency slot wait timed out", False
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, "node hard timeout reached", False
            try:
                completed = subprocess.run(
                    command,
                    cwd=SCRIPT_DIR,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=remaining,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                return None, "collector process timed out", True
            except OSError as exc:
                return None, _safe_text(f"could not start collector: {exc}"), False
            try:
                payload = json.loads(completed.stdout or "")
            except json.JSONDecodeError:
                detail = _safe_text((completed.stderr or completed.stdout or "").strip())[:300]
                return None, f"collector returned invalid JSON (exit {completed.returncode}): {detail}", True
            if not isinstance(payload, dict):
                return None, f"collector returned unexpected JSON (exit {completed.returncode})", True
            return _safe_tree(payload), None, True
        finally:
            slot_lock.release()
    finally:
        if browser_lock is not None:
            browser_lock.release()


def _search_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    raw_rows = payload.get("results", [])
    if not isinstance(raw_rows, list):
        return [raw_rows]
    for row in raw_rows:
        if not isinstance(row, dict):
            rows.append(row)
            continue
        mapped = {
            "platform": row.get("platform", "web"),
            "url": row.get("url"),
            "title": row.get("title"),
            "published_raw": row.get("published_raw", row.get("published")),
            "published": row.get("published"),
            "summary": row.get("snippet", row.get("summary", "")),
            "sources": row.get("sources", []),
        }
        if "canonical" in row:
            mapped["canonical"] = row["canonical"]
        for name in (
            "body", "text", "body_chars", "quality", "noise_ratio",
            "method", "fetched_at", "node_ids", "ocr_text",
            "vision_text", "vision_source", "expand_hints", "scroll",
        ):
            if name in row:
                mapped[name] = row[name]
        rows.append(mapped)
    return rows


def _collector_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_rows = payload.get("items", [])
    return raw_rows if isinstance(raw_rows, list) else [raw_rows]


def _valid_collector_row_count(rows: list[dict[str, Any]]) -> int:
    return sum(
        1 for row in rows
        if isinstance(row, dict) and not _is_spam_row(row) and _normalize_item_row(row) is not None
    )


def _effective_channels(node: dict[str, Any], state: dict[str, Any]) -> tuple[tuple[str, ...], bool]:
    node_channels = node.get("channels")
    if isinstance(node_channels, list) and node_channels:
        return tuple(node_channels), False
    state_channels = state.get("channels")
    if isinstance(state_channels, list) and state_channels:
        return tuple(state_channels), False
    return DEFAULT_CHANNELS, True


def _collect_node(
    node: dict[str, Any],
    state_path: Path,
    platforms: tuple[str, ...],
    deadline: float,
    platform_options: dict[str, int] | None = None,
) -> tuple[list[dict[str, Any]], bool, list[str]]:
    rows: list[dict[str, Any]] = []
    issued = False
    errors: list[str] = []
    if not platforms:
        return rows, issued, ["no collection channels configured"]
    query = str(node["question"])
    options = dict(DEFAULT_PLATFORM_OPTIONS)
    options.update(platform_options or {})
    configured_queries = node.get("queries") if isinstance(node.get("queries"), list) else []
    has_custom_queries = bool(node.get("queries_configured")) or any(
        value != query for value in configured_queries
    )
    support_queries = list(dict.fromkeys(
        [value for value in configured_queries if str(value).strip()] + [query]
        if has_custom_queries and configured_queries else [query]
    ))
    counter_queries = node.get("counter_queries") if isinstance(node.get("counter_queries"), list) else []
    query_jobs = [(str(value), "support") for value in support_queries]
    query_jobs.extend((str(value), "counter") for value in counter_queries if str(value).strip())

    # Sequential child calls keep one node worker bounded; ready nodes provide the parallelism.
    browser_blocked = False
    for current_query, query_kind in query_jobs:
        for platform in platforms:
            tasks: list[tuple[str, list[str], bool]] = []
            if platform == "web":
                tasks.append(("search_dual.py", [
                    sys.executable, str(SCRIPT_DIR / "search_dual.py"), current_query, "--json",
                ], False))
            elif platform in COLLECT_PLATFORMS:
                tasks.append(("collect.py", [
                    sys.executable, str(SCRIPT_DIR / "collect.py"), current_query,
                    "--platforms", platform, "--json",
                ], False))
            elif platform == "browser":
                browser_platforms = ",".join(BROWSER_PLATFORM_ORDER)
                tasks.append(("collect_browser.py", [
                    sys.executable, str(SCRIPT_DIR / "collect_browser.py"), current_query,
                    "--platforms", browser_platforms,
                    "--detail", str(options["detail"]),
                    "--images", str(options["images"]),
                    "--vision", str(options["vision"]),
                    "--json",
                ], True))
            elif platform in BROWSER_PLATFORMS:
                tasks.append(("collect_browser.py", [
                    sys.executable, str(SCRIPT_DIR / "collect_browser.py"), current_query,
                    "--platforms", platform,
                    "--detail", str(options["detail"]),
                    "--images", str(options["images"]),
                    "--vision", str(options["vision"]),
                    "--json",
                ], True))

            for script_name, command, browser in tasks:
                try:
                    payload, error, started = _command_json(command, state_path, deadline, browser=browser)
                except BrowserLockTimeout as exc:
                    errors.append(_safe_text(str(exc)))
                    browser_blocked = True
                    break
                issued = issued or started
                if error:
                    errors.append(f"{script_name}: {_safe_text(error)}")
                    continue
                if payload is None:
                    continue
                collected = _search_rows(payload) if script_name == "search_dual.py" else _collector_rows(payload)
                for row in collected:
                    if isinstance(row, dict):
                        row = dict(row)
                        row["query_kind"] = query_kind
                    rows.append(row)
                for source, reason in (payload.get("errors") or {}).items():
                    errors.append(f"{script_name}.{source}: {_safe_text(reason)}")
            if browser_blocked:
                break
        if browser_blocked:
            break
    return rows, issued, errors


def _merge_items(state: dict[str, Any], node: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    indexed = {str(item.get("canonical")): item for item in state["items"]}
    node["dropped"] = int(node.get("dropped") or 0)
    node["dropped_spam"] = int(node.get("dropped_spam") or 0)
    for row in rows:
        query_kind = row.get("query_kind", "support") if isinstance(row, dict) else "support"
        if query_kind not in {"support", "counter"}:
            query_kind = "support"
        if _is_spam_row(row):
            node["dropped_spam"] += 1
            continue
        normalized = _normalize_item_row(row)
        if normalized is None:
            node["dropped"] += 1
            continue
        canonical = normalized["canonical"]
        item = indexed.get(canonical)
        if item is None:
            host = _host(canonical)
            item = normalized
            item["id"] = _next_numbered_id(state["items"], "i")
            item["hosts"] = [host] if host else []
            item["node_ids"] = []
            item["node_refs"] = []
            state["items"].append(item)
            indexed[canonical] = item
        else:
            current_sources = item.get("sources")
            if not isinstance(current_sources, list):
                current_sources = []
                item["sources"] = current_sources
            for source in normalized["sources"]:
                if source not in current_sources:
                    current_sources.append(source)
            if len(normalized["body"]) > len(str(item.get("body") or "")):
                for name in ("body", "body_chars", "quality", "noise_ratio", "method", "fetched_at",
                             "expand_hints", "scroll"):
                    if name in normalized:
                        item[name] = normalized[name]
                    else:
                        item.pop(name, None)
            if not item.get("published") and normalized["published"]:
                item["published"] = normalized["published"]
                item["published_raw"] = normalized["published_raw"]
            if len(normalized["summary"]) > len(str(item.get("summary") or "")):
                item["summary"] = normalized["summary"]
            for name in ("ocr_text", "vision_text", "vision_source"):
                if normalized.get(name) and not item.get(name):
                    item[name] = normalized[name]
            for name in ("tables", "chars", "login_wall"):
                if name in normalized and name not in item:
                    item[name] = normalized[name]
        if node["id"] not in item["node_ids"]:
            item["node_ids"].append(node["id"])
        relation = {"node_id": node["id"], "query_kind": query_kind}
        node_refs = item.setdefault("node_refs", [])
        if relation not in node_refs:
            node_refs.append(relation)
        if item["id"] not in node["evidence_ids"]:
            node["evidence_ids"].append(item["id"])


def _normalize_item_row(row: Any) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None

    url = row.get("url")
    title = row.get("title")
    if not isinstance(url, str) or not isinstance(title, str):
        return None
    title = re.sub(r"\s+", " ", title).strip()
    if not title:
        return None
    try:
        canonical = canonicalize_url(url)
    except (TypeError, ValueError):
        return None

    if "canonical" in row and not isinstance(row["canonical"], str):
        return None
    platform = row.get("platform", "web")
    summary = row.get("summary", row.get("snippet", ""))
    body = row.get("body", row.get("text", ""))
    method = row.get("method", "")
    if not all(isinstance(value, str) for value in (platform, summary, body, method)):
        return None

    published_raw = row.get("published_raw", row.get("published"))
    published = row.get("published", published_raw)
    if published_raw is not None and not isinstance(published_raw, str):
        return None
    if published is not None and not isinstance(published, str):
        return None

    sources = row.get("sources", [])
    if not isinstance(sources, list) or not all(isinstance(source, str) for source in sources):
        return None
    node_ids = row.get("node_ids", [])
    if not isinstance(node_ids, list) or not all(isinstance(node_id, str) for node_id in node_ids):
        return None

    body_chars = len(body)
    metrics = {}
    if body:
        for name in ("quality", "noise_ratio"):
            value = row.get(name)
            if type(value) in {int, float} and math.isfinite(float(value)):
                metrics[name] = float(value)
    fetched_at = row.get("fetched_at")
    if fetched_at is not None and not isinstance(fetched_at, str):
        return None

    result: dict[str, Any] = {
        "canonical": canonical,
        "url": url.strip(),
        "title": title,
        "platform": platform,
        "published_raw": published_raw,
        "published": published,
        "summary": re.sub(r"\s+", " ", summary).strip(),
        "sources": list(dict.fromkeys(sources)),
        "body": body,
        "body_chars": body_chars,
        "method": method,
        "fetched_at": fetched_at,
        "node_ids": list(dict.fromkeys(node_ids)),
    }
    result.update(metrics)
    for name in ("tables", "chars", "login_wall"):
        if name in row:
            result[name] = row[name]
    for name in ("ocr_text", "vision_text", "vision_source"):
        if name in row:
            value = row[name]
            if value is not None and not isinstance(value, str):
                return None
            result[name] = value
    hints = row.get("expand_hints")
    if isinstance(hints, dict):
        cleaned_hints = {
            str(key): value for key, value in hints.items()
            if isinstance(key, str) and type(value) is int and value > 0
        }
        if cleaned_hints:
            result["expand_hints"] = cleaned_hints
    scroll_meta = row.get("scroll")
    if isinstance(scroll_meta, dict):
        result["scroll"] = {
            str(key): value for key, value in scroll_meta.items()
            if type(value) in {int, str}
        }
    return result


def _is_spam_row(row: Any) -> bool:
    if not isinstance(row, dict):
        return False
    title = row.get("title")
    if isinstance(title, str) and SPAM_TITLE_RE.search(title):
        return True
    for field in ("url", "canonical"):
        value = row.get(field)
        if not isinstance(value, str) or not value.strip():
            continue
        candidate = value.strip()
        try:
            parts = urlsplit(candidate)
            if not parts.scheme:
                parts = urlsplit("https://" + candidate.lstrip("/"))
            hostname = parts.hostname or ""
        except ValueError:
            continue
        folded_hostname = hostname.casefold()
        if any(marker.casefold() in folded_hostname for marker in SPAM_DOMAIN_MARKERS):
            return True
    return False


def _upgrade_state_item_contract(state: dict[str, Any]) -> None:
    if not state.get("items"):
        return
    references: dict[str, list[dict[str, Any]]] = {}
    for node in state["nodes"]:
        if not isinstance(node, dict):
            continue
        for item_id in node.get("evidence_ids", []):
            references.setdefault(str(item_id), []).append(node)

    items = []
    dropped_ids = set()
    for raw in state["items"]:
        if not isinstance(raw, dict):
            continue
        item_id = raw.get("id")
        if not isinstance(item_id, str) or not item_id:
            item_id = ""
        row = dict(raw)
        linked_nodes = references.get(item_id, [])
        if not isinstance(row.get("node_ids"), list) or not row.get("node_ids"):
            row["node_ids"] = [str(node["id"]) for node in linked_nodes if isinstance(node.get("id"), str)]
        normalized = _normalize_item_row(row)
        if normalized is None or not item_id:
            dropped_ids.add(item_id)
            affected = linked_nodes or [
                node for node in state["nodes"]
                if isinstance(node, dict) and node.get("id") in row.get("node_ids", [])
            ]
            for node in affected:
                node["dropped"] = int(node.get("dropped") or 0) + 1
            continue
        normalized["id"] = item_id
        host = _host(normalized["canonical"])
        normalized["hosts"] = [host] if host else []
        raw_refs = row.get("node_refs")
        refs = []
        if isinstance(raw_refs, list):
            for relation in raw_refs:
                if not isinstance(relation, dict):
                    continue
                node_id = relation.get("node_id")
                query_kind = relation.get("query_kind")
                if isinstance(node_id, str) and query_kind in {"support", "counter"}:
                    candidate = {"node_id": node_id, "query_kind": query_kind}
                    if candidate not in refs:
                        refs.append(candidate)
        if not refs:
            refs = [
                {"node_id": str(node["id"]), "query_kind": "support"}
                for node in linked_nodes if isinstance(node.get("id"), str)
            ]
        normalized["node_refs"] = refs
        items.append(normalized)

    if dropped_ids:
        for node in state["nodes"]:
            if isinstance(node, dict):
                node["evidence_ids"] = [
                    item_id for item_id in node.get("evidence_ids", [])
                    if str(item_id) not in dropped_ids
                ]
    state["items"] = items


def _question_tokens(question: str) -> set[str]:
    value = unicodedata.normalize("NFKC", question).casefold()
    tokens: set[str] = set()
    for chunk in re.findall(r"[\u3400-\u9fff]+|[^\s\u3400-\u9fff]+", value):
        if re.fullmatch(r"[\u3400-\u9fff]+", chunk):
            for width in (2, 3, 4):
                tokens.update(chunk[index:index + width] for index in range(len(chunk) - width + 1))
        else:
            token = "".join(char for char in chunk if char.isalnum())
            if token:
                tokens.add(token)
    return tokens


def _item_body_chars(item: dict[str, Any]) -> int:
    return len(str(item.get("body") or ""))


def _item_has_body(item: dict[str, Any]) -> bool:
    return bool(str(item.get("body") or ""))


def _fetch_stats(node: dict[str, Any]) -> dict[str, Any]:
    previous = node.get("fetch_stats")
    previous = previous if isinstance(previous, dict) else {}
    stats = {}
    for name in ("tried", "ok", "failed"):
        value = previous.get(name)
        stats[name] = value if type(value) is int and value >= 0 else 0
    chars = previous.get("chars")
    stats["chars"] = [value for value in chars if type(value) is int and value >= 0] \
        if isinstance(chars, list) else []
    node["fetch_stats"] = stats
    return stats


def _looks_like_pdf(url: str) -> bool:
    return urlsplit(url).path.lower().endswith(".pdf")


def _fetch_candidates(state: dict[str, Any], node: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    tokens = _question_tokens(str(node.get("question") or ""))
    item_index = {str(item.get("id")): item for item in state["items"]}
    ranked = []
    for position, item_id in enumerate(node.get("evidence_ids", [])):
        item = item_index.get(str(item_id))
        if item is None or _item_has_body(item):
            continue
        title_tokens = _question_tokens(str(item.get("title") or ""))
        overlap = len(tokens & title_tokens)
        ranked.append((-overlap, position, item))
    ranked.sort(key=lambda entry: entry[:2])
    return [entry[2] for entry in ranked[:limit]]


def _fetch_payload(url: str, scroll: int | None = None) -> tuple[dict[str, Any] | None, str | None]:
    command = [sys.executable, str(SCRIPT_DIR / "fetch_smart.py"), url, "--json"]
    if scroll:
        command.extend(["--scroll", str(int(scroll))])
    fetch_route = os.environ.get("FETCH_SMART_ROUTE", "").strip()
    if not fetch_route and (
        os.environ.get("OPEN_NOTEBOOK_URL", "").strip() or _dotenv_value("OPEN_NOTEBOOK_URL")
    ):
        # Notebook 服务可用时默认优先走它：抽到的正文通常是原链路的数倍。
        # 服务不可用或抽取失败会在 fetch_smart 内部自动回落原链路。
        fetch_route = "notebook"
    if fetch_route in {"auto", "logged-in", "notebook"}:
        command.extend(["--route", fetch_route])
    try:
        completed = subprocess.run(
            command,
            cwd=SCRIPT_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=FETCH_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, f"fetch timed out after {FETCH_TIMEOUT}s"
    except OSError as exc:
        return None, _safe_text(f"could not start fetcher: {exc}")
    if completed.returncode != 0:
        detail = _safe_text((completed.stderr or completed.stdout or "").strip())[:300]
        return None, f"fetcher exited with code {completed.returncode}" + (f": {detail}" if detail else "")
    try:
        payload = json.loads(completed.stdout or "")
    except json.JSONDecodeError:
        return None, "fetcher returned invalid JSON"
    if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
        return None, "fetcher returned no text"
    return payload, None


def _fetch_node_items(state_path: Path, node_id: str, limit: int) -> None:
    attempt_limit = max(int(limit), 0)
    if attempt_limit == 0:
        return
    with FileLock(_state_lock_path(state_path)):
        state = _read_state_unlocked(state_path)
        node = _node_index(state)[node_id]
        candidates = _fetch_candidates(state, node, len(node.get("evidence_ids", [])))

    attempted = 0
    for candidate in candidates:
        if attempted >= attempt_limit:
            break
        item_id = str(candidate["id"])
        url = str(candidate.get("url") or candidate.get("canonical") or "")
        claim = FileLock(Path(f"{state_path}.item.{item_id}.fetch.lock"))
        if not claim.acquire(timeout=0):
            continue
        try:
            with FileLock(_state_lock_path(state_path)):
                state = _read_state_unlocked(state_path)
                item = next((row for row in state["items"] if str(row.get("id")) == item_id), None)
                if item is None or _item_has_body(item):
                    continue
                node = _node_index(state)[node_id]
                current_url = str(item.get("url") or item.get("canonical") or url)
                stats = _fetch_stats(node)
                stats["tried"] += 1
                attempted += 1
                _write_state_unlocked(state_path, state)

            try:
                payload, error = _fetch_payload(current_url, node.get("scroll"))
            except Exception as exc:
                payload, error = None, _safe_text(f"fetch failed: {exc}")
            with FileLock(_state_lock_path(state_path)):
                state = _read_state_unlocked(state_path)
                node = _node_index(state)[node_id]
                item = next((row for row in state["items"] if str(row.get("id")) == item_id), None)
                stats = _fetch_stats(node)
                if error:
                    node.setdefault("fetch_errors", []).append(f"{item_id}: {_safe_text(error)}")
                    stats["failed"] += 1
                else:
                    text = str(payload["text"])
                    stats["ok"] += 1
                    stats["chars"].append(len(text))
                    if item is not None and not _item_has_body(item):
                        item["body"] = text
                        item["body_chars"] = len(text)
                        item["method"] = payload.get("method") if isinstance(payload.get("method"), str) else ""
                        if isinstance(payload.get("expand_hints"), dict):
                            item["expand_hints"] = payload["expand_hints"]
                        if isinstance(payload.get("scroll"), dict):
                            item["scroll"] = payload["scroll"]
                        item["fetched_at"] = (
                            payload.get("fetched_at")
                            if isinstance(payload.get("fetched_at"), str) and payload.get("fetched_at")
                            else _now()
                        )
                        for name in ("quality", "noise_ratio"):
                            value = payload.get(name)
                            if type(value) in {int, float} and math.isfinite(float(value)):
                                item[name] = float(value)
                            else:
                                item.pop(name, None)
                _write_state_unlocked(state_path, state)
        finally:
            claim.release()


def _run_node(
    state_path: Path, node_id: str, timeout: float, fetch_count: int, retry_zero_results: bool = True,
) -> dict[str, Any] | None:
    claim_path = Path(f"{state_path}.node.{node_id}.lock")
    claim = FileLock(claim_path)
    if not claim.acquire(timeout=0):
        return None
    try:
        with FileLock(_state_lock_path(state_path)):
            state = _read_state_unlocked(state_path)
            node = _node_index(state).get(node_id)
            if node is None:
                raise ValueError(f"unknown node: {node_id}")
            if node.get("status") not in STATUS_TRANSITIONS["run"]["from"]:
                return None
            node["dropped"] = 0
            node["dropped_spam"] = 0
            node["run_errors"] = []
            node["fetch_errors"] = []
            node["fetch_stats"] = {"tried": 0, "ok": 0, "failed": 0, "chars": []}
            node["zero_result_retry"] = False
            node["retry_channels"] = []
            node["retry_rows"] = 0
            platforms, fallback_default = _effective_channels(node, state)
            _write_state_unlocked(state_path, state)
            platform_options = dict(state.get("platform_options") or DEFAULT_PLATFORM_OPTIONS)
            node_snapshot = dict(node)

        deadline = time.monotonic() + timeout
        try:
            rows, issued, errors = _collect_node(
                node_snapshot, state_path, platforms, deadline, platform_options
            )
        except Exception as exc:
            rows, issued, errors = [], False, [_safe_text(f"collector failed: {exc}")]

        retry_attempted = retry_zero_results and "web" not in platforms and _valid_collector_row_count(rows) == 0
        retry_rows = 0
        channels_used = list(platforms)
        if retry_attempted:
            try:
                web_rows, web_issued, web_errors = _collect_node(
                    node_snapshot, state_path, ("web",), deadline, platform_options
                )
            except Exception as exc:
                web_rows, web_issued, web_errors = [], False, [_safe_text(f"web retry failed: {exc}")]
            retry_rows = _valid_collector_row_count(web_rows)
            rows.extend(web_rows)
            channels_used.append("web")
            if retry_rows:
                errors = web_errors
            else:
                errors.extend(web_errors)
            issued = issued or web_issued

        with FileLock(_state_lock_path(state_path)):
            state = _read_state_unlocked(state_path)
            node = _node_index(state)[node_id]
            node["zero_result_retry"] = retry_attempted
            node["retry_channels"] = ["web"] if retry_attempted else []
            node["retry_rows"] = retry_rows
            _merge_items(state, node, rows)
            has_custom_queries = bool(node_snapshot.get("queries_configured")) or any(
                value != str(node_snapshot["question"])
                for value in (node_snapshot.get("queries") or [])
            )
            if issued and not has_custom_queries:
                node["queries"].append(str(node["question"]))
            if errors:
                node["result"] = "采集诊断：" + "；".join(errors)
            elif str(node.get("result") or "").startswith("采集诊断："):
                node["result"] = ""
            node["run_errors"] = [_safe_text(error) for error in errors]
            _transition_node_status(node, "run", "awaiting_review")
            _write_state_unlocked(state_path, state)
        if fetch_count:
            try:
                _fetch_node_items(state_path, node_id, fetch_count)
            except Exception as exc:
                with FileLock(_state_lock_path(state_path)):
                    state = _read_state_unlocked(state_path)
                    node = _node_index(state)[node_id]
                    node.setdefault("fetch_errors", []).append(_safe_text(f"fetch failed: {exc}"))
                    _write_state_unlocked(state_path, state)
        return {
            "node_id": node_id,
            "channels": channels_used,
            "fallback_default": fallback_default,
        }
    finally:
        claim.release()


def run_nodes(
    state_path: Path | str,
    node_ids: list[str],
    timeout: float = DEFAULT_TIMEOUT,
    fetch: int = DEFAULT_FETCH_COUNT,
    retry_zero_results: bool = True,
) -> dict[str, Any]:
    if timeout <= 0:
        raise ValueError("--timeout must be positive")
    if fetch < 0:
        raise ValueError("--fetch cannot be negative")
    path = Path(state_path)
    ids = list(dict.fromkeys(node_ids))
    invalid = [node_id for node_id in ids if not re.fullmatch(r"n\d+", node_id)]
    if invalid:
        raise ValueError(f"invalid node id: {invalid[0]}")
    run_records = []
    if ids:
        with ThreadPoolExecutor(max_workers=min(COLLECTOR_SLOTS, len(ids)), thread_name_prefix="research-node") as pool:
            futures = [
                pool.submit(_run_node, path, node_id, timeout, fetch, retry_zero_results)
                for node_id in ids
            ]
            for future in as_completed(futures):
                record = future.result()
                if record is not None:
                    run_records.append(record)
    state = read_state(path)
    channels_by_node = {record["node_id"]: record["channels"] for record in run_records}
    fallback_by_node = {record["node_id"]: record["fallback_default"] for record in run_records}
    channels_used = list(dict.fromkeys(
        channel for record in run_records for channel in record["channels"]
    ))
    errors = []
    nodes = _node_index(state)
    for node_id in channels_by_node:
        node = nodes[node_id]
        for error in [*(node.get("run_errors") or []), *(node.get("fetch_errors") or [])]:
            errors.append(f"{node_id}: {_safe_text(error)}")
    state.update({
        "channels_used": channels_used,
        "channels_by_node": channels_by_node,
        "fallback_by_node": fallback_by_node,
        "fallback_default": any(fallback_by_node.values()),
        "errors": errors,
    })
    state.update(progress_data(state, path))
    return state


def run_ready(
    state_path: Path | str,
    timeout: float = DEFAULT_TIMEOUT,
    fetch: int = DEFAULT_FETCH_COUNT,
    retry_zero_results: bool = True,
) -> dict[str, Any]:
    path = Path(state_path)
    state = read_state(path)
    ready = [node["id"] for node in state["nodes"] if node.get("status") == "open"]
    return run_nodes(path, ready, timeout=timeout, fetch=fetch, retry_zero_results=retry_zero_results)


def reset_nodes(state_path: Path | str, node_ids: list[str]) -> dict[str, Any]:
    path = Path(state_path)
    ids = list(dict.fromkeys(node_ids))
    with FileLock(_state_lock_path(path)):
        state = _read_state_unlocked(path)
        nodes = _node_index(state)
        for node_id in ids:
            if node_id not in nodes:
                raise ValueError(f"unknown node: {node_id}")
        for node_id in ids:
            _transition_node_status(nodes[node_id], "reset", "open")
        _write_state_unlocked(path, state)
    return read_state(path)


def add_claim(
    state_path: Path | str,
    node_id: str,
    text: str,
    evidence: list[str],
    counter_evidence: list[str] | None = None,
    version: str | None = None,
    as_of: str | None = None,
    support: str | None = None,
    gaps: list[str] | None = None,
) -> dict[str, Any]:
    claim_text = str(text or "").strip()
    if not claim_text:
        raise ValueError("claim --text cannot be empty")
    if not isinstance(evidence, list) or not evidence or not all(isinstance(ref, str) for ref in evidence):
        raise ValueError("claim --evidence must include at least one reference")
    if counter_evidence is not None and (
        not isinstance(counter_evidence, list) or not all(isinstance(ref, str) for ref in counter_evidence)
    ):
        raise ValueError("claim --counter must contain references")
    if support not in (None, "") and (not isinstance(support, str) or support not in CLAIM_SUPPORTS):
        raise ValueError("claim --support must be single, dual, or contested")
    if gaps is not None and (not isinstance(gaps, list) or not all(isinstance(gap_id, str) for gap_id in gaps)):
        raise ValueError("claim --gaps must contain gap ids")
    path = Path(state_path)
    with FileLock(_state_lock_path(path)):
        state = _read_state_unlocked(path)
        if node_id not in _node_index(state):
            raise ValueError(f"unknown node: {node_id}")
        used = list(dict.fromkeys([*(evidence or []), *(counter_evidence or [])]))
        bad_refs = _bad_review_refs(state, node_id, used)
        if bad_refs:
            raise ValueError("bad_refs: " + json.dumps(bad_refs, ensure_ascii=False))
        gap_ids = list(dict.fromkeys(gaps or []))
        known_gap_ids = {
            str(gap.get("id"))
            for node in state["nodes"]
            for gap in (node.get("review", {}).get("gaps") or [])
            if isinstance(gap, dict) and gap.get("id")
        }
        missing_gaps = [gap_id for gap_id in gap_ids if gap_id not in known_gap_ids]
        if missing_gaps:
            raise ValueError("unknown gap id(s): " + ", ".join(missing_gaps))
        state["claim_seq"] = int(state.get("claim_seq") or 0) + 1
        claim = {
            "id": f"c{state['claim_seq']}",
            "node_id": node_id,
            "text": claim_text,
            "evidence": list(dict.fromkeys(evidence or [])),
            "counter_evidence": list(dict.fromkeys(counter_evidence or [])),
            "version": version,
            "as_of": as_of,
            "support": support or "",
            "gaps": gap_ids,
            "created_at": _now(),
        }
        state["claims"].append(claim)
        _write_state_unlocked(path, state)
    return read_state(path)


def remove_claim(state_path: Path | str, claim_id: str) -> dict[str, Any]:
    path = Path(state_path)
    with FileLock(_state_lock_path(path)):
        state = _read_state_unlocked(path)
        remaining = [claim for claim in state["claims"] if claim.get("id") != claim_id]
        if len(remaining) == len(state["claims"]):
            raise ValueError(f"unknown claim: {claim_id}")
        state["claims"] = remaining
        _write_state_unlocked(path, state)
    return read_state(path)


def claims_data(state: dict[str, Any], node_id: str | None = None) -> list[dict[str, Any]]:
    claims = state.get("claims") or []
    return [claim for claim in claims if node_id is None or claim.get("node_id") == node_id]


def claims_text(claims: list[dict[str, Any]]) -> str:
    if not claims:
        return "（无）"
    rows = []
    for claim in claims:
        row = (
            f"[{claim.get('id')}] {claim.get('node_id')}: {claim.get('text')}"
            f" | 证据 {','.join(claim.get('evidence') or []) or '-'}"
            f" | 反证 {','.join(claim.get('counter_evidence') or []) or '-'}"
            f" | 版本 {claim.get('version') or '-'} | 日期 {claim.get('as_of') or '-'}"
        )
        annotations = []
        if claim.get("support"):
            annotations.append(f"支撑={claim['support']}")
        if claim.get("gaps"):
            annotations.append(f"关联缺口={','.join(claim['gaps'])}")
        if annotations:
            row += " | " + "｜".join(annotations)
        rows.append(row)
    return "\n".join(rows)


def _status_symbol(status: str) -> str:
    return {
        "answered": "●",
        "partial": "◐",
        "open": "○",
        "not_found": "○",
        "closed_unresolved": "✕",
        "awaiting_review": "◇",
    }.get(status, "○")


def progress_data(state: dict[str, Any], state_path: str | Path | None = None) -> dict[str, Any]:
    state = _validate_state(state)
    nodes = state["nodes"]
    current_layers = max((int(node.get("depth") or 0) + 1 for node in nodes), default=0)
    count_through = max(current_layers, MIN_LAYERS)
    counts = {str(layer): 0 for layer in range(1, count_through + 1)}
    for node in nodes:
        layer = int(node.get("depth") or 0) + 1
        counts[str(layer)] = counts.get(str(layer), 0) + 1

    parent_ids = {str(node.get("parent")) for node in nodes if node.get("parent")}
    pending_split_nodes = sorted(
        (
            {"id": node["id"], "layer": int(node.get("depth") or 0) + 1, "status": node["status"]}
            for node in nodes
            if node.get("status") in {"partial", "not_found"}
            and int(node.get("depth") or 0) + 1 < MIN_LAYERS
            and node["id"] not in parent_ids
            and not (node.get("review") or {}).get("early_close")
        ),
        key=lambda entry: (entry["layer"], int(entry["id"][1:])),
    )
    pending_split = [node["id"] for node in pending_split_nodes]
    open_nodes = [node["id"] for node in nodes if node.get("status") == "open"]
    pending_review = [node["id"] for node in nodes if node.get("status") == "awaiting_review"]
    unmet_reasons = []
    if current_layers < MIN_LAYERS:
        unmet_reasons.append({
            "code": "layers_below_minimum",
            "detail": f"{current_layers}/{MIN_LAYERS} layers",
        })
    if pending_split:
        unmet_reasons.append({"code": "pending_split_nodes", "count": len(pending_split)})
    if open_nodes:
        unmet_reasons.append({"code": "open_nodes", "count": len(open_nodes)})
    if pending_review:
        unmet_reasons.append({"code": "awaiting_review_nodes", "count": len(pending_review)})

    items_by_node: dict[str, list[dict[str, Any]]] = {}
    for item in state.get("items", []) or []:
        for ref in item.get("node_refs") or []:
            if isinstance(ref, dict) and ref.get("node_id"):
                items_by_node.setdefault(str(ref["node_id"]), []).append(item)
    pdf_candidates = []
    for node in nodes:
        if node.get("status") not in {"open", "awaiting_review"}:
            continue
        pending_pdf = 0
        for item in items_by_node.get(node["id"], []):
            if int(item.get("body_chars") or 0) > 0:
                continue
            if _looks_like_pdf(str(item.get("url") or item.get("canonical") or "")):
                pending_pdf += 1
        if pending_pdf:
            pdf_candidates.append({"id": node["id"], "count": pending_pdf})

    scroll_candidates = []
    for node in nodes:
        hint_total = 0
        for item in items_by_node.get(node["id"], []):
            hints = item.get("expand_hints")
            if not isinstance(hints, dict) or not hints or item.get("scroll"):
                continue
            hint_total += sum(int(value) for value in hints.values() if isinstance(value, int))
        if hint_total >= 3:
            scroll_candidates.append({
                "id": node["id"],
                "layer": int(node.get("depth") or 0) + 1,
                "hints": hint_total,
                "scroll": int(node.get("scroll") or 0),
            })

    path_arg = _shell_path(state_path) if state_path is not None else '"<state>"'
    suggested_commands = []
    if pending_review:
        suggested_commands.append(
            f"python research.py show --state {path_arg} --node {pending_review[0]}"
        )
    if open_nodes:
        suggested_commands.append(f"python research.py run --state {path_arg} --ready --json")
    if pending_split_nodes:
        suggested_commands.append(
            f"python research.py prompt --state {path_arg} --node {pending_split_nodes[0]['id']}"
        )
    if scroll_candidates and not scroll_candidates[0]["scroll"]:
        suggested_commands.append(
            f"python research.py run --state {path_arg} --node {scroll_candidates[0]['id']} --scroll 3"
        )
    if not suggested_commands:
        suggested_commands.append(f"python research.py digest --state {path_arg}")

    return {
        "layers": {
            "current": current_layers,
            "required": MIN_LAYERS,
            "met": current_layers >= MIN_LAYERS,
            "counts": counts,
            "missing": max(0, MIN_LAYERS - current_layers),
        },
        "unmet_reasons": unmet_reasons,
        "pending_split": pending_split,
        "pending_split_nodes": pending_split_nodes,
        "open_nodes": open_nodes,
        "pending_review": pending_review,
        "gap_rounds": len(_gap_rounds(state)),
        "open_gaps": [
            {"id": gap.get("id"), "what": gap.get("what"), "next_question": gap.get("next_question"),
             "node_id": gap.get("node_id")}
            for gap in _all_gaps(state) if gap.get("status") == "open"
        ],
        "alias_mentions": (
            topic_brief.mentions(state.get("topic_brief"), state.get("items") or [])
            if state.get("topic_brief") else None
        ),
        "scroll_candidates": scroll_candidates,
        "pdf_candidates": pdf_candidates,
        "next_step": (
            "先处理 open_gaps：派生节点补齐，或标 covered/dropped 并说明原因"
            if any(gap.get("status") == "open" for gap in _all_gaps(state)) else
            "先处理 awaiting_review 节点" if pending_review else
            "运行 run 采集 open 节点" if open_nodes else
            "对最浅的 partial/not_found 节点运行 prompt 补 children" if pending_split else
            "可收口"
        ),
        "suggested_commands": suggested_commands,
        "next_command": suggested_commands[0],
    }


def _progress_summary_line(state: dict[str, Any]) -> str:
    progress = progress_data(state)
    layers = progress["layers"]
    layer_status = "已达标" if layers["met"] else "未达标"
    pending = ", ".join(
        f"{node['id']}(第{node['layer']}层,{node['status']})"
        for node in progress["pending_split_nodes"]
    ) or "-"
    next_step = (
        "先处理 awaiting_review 节点" if progress["pending_review"] else
        "运行 run 采集 open 节点" if progress["open_nodes"] else
        "对最浅的 partial/not_found 节点运行 prompt 补 children"
        if progress["pending_split_nodes"] else "可收口"
    )
    return (
        f"层数: {layers['current']}/{layers['required']}（{layer_status}）"
        f"｜待拆节点: {pending}｜可滚节点: "
        f"{', '.join(entry['id'] for entry in progress['scroll_candidates']) or '-'}"
        f"｜下一步: {next_step}"
    )


def status_text(data: dict[str, Any]) -> str:
    layers = data["layers"]
    counts = "，".join(f"第{layer}层={count}" for layer, count in layers["counts"].items())
    pdf_text = ", ".join(
        str(entry["id"]) + "(" + str(entry["count"]) + "个)" for entry in data.get("pdf_candidates", [])
    ) or "-"
    scroll_text = ", ".join(
        str(entry["id"]) + "(" + str(entry["hints"]) + "处展开提示)"
        for entry in data["scroll_candidates"]
    ) or "-"
    reasons = "; ".join(
        f"{reason['code']}" + (f"({reason.get('count')})" if "count" in reason else "")
        for reason in data["unmet_reasons"]
    ) or "无"
    return "\n".join([
        f"层数: {layers['current']}/{layers['required']}（{'已达标' if layers['met'] else '未达标'}）",
        f"各层节点数: {counts}",
        f"未达标原因: {reasons}",
        f"待拆节点: {', '.join(data['pending_split']) or '-'}",
        f"open 节点: {', '.join(data['open_nodes']) or '-'}",
        f"awaiting_review 节点: {', '.join(data['pending_review']) or '-'}",
        f"可考虑滚动节点: {scroll_text}",
        f"未抓 PDF: {pdf_text}",
        f"下一步建议命令: {data['next_command']}",
    ])


def report_text(state: dict[str, Any], *, include_claims: bool = True) -> str:
    state = _validate_state(state)
    nodes = _node_index(state)
    children: dict[str, list[str]] = {}
    for edge in state["edges"]:
        if edge.get("from") in nodes and edge.get("to") in nodes:
            children.setdefault(edge["from"], []).append(edge["to"])
    item_index = {item["id"]: item for item in state["items"]}
    latest_task_run: dict[str, dict[str, Any]] = {}
    for task_run in state.get("task_runs", []):
        latest_task_run[task_run["node_id"]] = task_run
    lines: list[str] = []

    def render(node_id: str, prefix: str, last: bool, root: bool = False) -> None:
        node = nodes[node_id]
        evidence = [item_index[item_id] for item_id in node["evidence_ids"] if item_id in item_index]
        body_lengths = [_item_body_chars(item) for item in evidence if _item_has_body(item)]
        label = (
            f"{_status_symbol(node['status'])} {node['question']}（{node['status']}）"
            f" | 证据 {len(evidence)}，已抓正文 {len(body_lengths)}"
            f"（字数 {'/'.join(map(str, body_lengths)) or '-'}）"
        )
        unresolved = node.get("review", {}).get("unresolved") or []
        if unresolved:
            label += "——未解：" + "；".join(unresolved)
        stats = node.get("fetch_stats")
        if stats is not None:
            label += (
                f" | fetch tried={int(stats.get('tried') or 0)}"
                f" ok={int(stats.get('ok') or 0)}"
                f" failed={int(stats.get('failed') or 0)}"
            )
            fetched_chars = stats.get("chars") or []
            label += f" 字数分布={'/'.join(map(str, fetched_chars)) or '-'}"
        label += f" | 丢弃无效记录 {int(node.get('dropped') or 0)}"
        label += f" | 丢弃垃圾源 {int(node.get('dropped_spam') or 0)}"
        if node.get("zero_result_retry"):
            label += f" | 零结果重试: 是（web，得到 {int(node.get('retry_rows') or 0)} 条）"
        lines.append(label if root else prefix + ("└─ " if last else "├─ ") + label)
        task_run = latest_task_run.get(node_id)
        if task_run is not None:
            worker_note = task_run["worker_note"].strip() or "-"
            lines[-1] += (
                f" | 返回=returned | 来源={task_run['source_count']}"
                f" | 正文={task_run['body_count']}/{task_run['source_count']}"
                f" | 字数={task_run['body_chars_total']} | 备注={worker_note}"
            )
        evidence_prefix = "  " if root else prefix + ("   " if last else "│  ")
        for item in evidence:
            published = item.get("published") or item.get("published_raw")
            lines.append(
                f"{evidence_prefix}- {item.get('id')} {item.get('title') or '(无标题)'}"
                f" | 发布时间：{published or '-'} | {_version_age_note(state, published)}"
            )
        child_ids = children.get(node_id, [])
        next_prefix = prefix if root else prefix + ("   " if last else "│  ")
        for index, child_id in enumerate(child_ids):
            render(child_id, next_prefix, index == len(child_ids) - 1)

    render(state["root"], "", True, root=True)
    if include_claims:
        lines.extend(["", "结论账本"])
        lines.append(claims_text(claims_data(state)))
    lines.append(_progress_summary_line(state))
    return _safe_text("\n".join(lines))


def _gap_claims(state: dict[str, Any], node: dict[str, Any], gap: dict[str, Any]) -> list[dict[str, Any]]:
    target = str(gap.get("target") or "")
    return [
        claim for claim in claims_data(state)
        if gap.get("id") in (claim.get("gaps") or [])
        or claim.get("id") == target
        or (
            claim.get("node_id") == node.get("id")
            and (
                target in (claim.get("evidence") or [])
                or target in (claim.get("counter_evidence") or [])
            )
        )
    ]


def _format_gap_markdown(node: dict[str, Any], gap: dict[str, Any]) -> list[str]:
    lines = [
        f"- `{node.get('id')}` [{gap.get('id')}] `{gap.get('type')}`，target={gap.get('target')}：{gap.get('why')}"
    ]
    lines.extend(f"  - 待补：{question}" for question in gap.get("questions") or [])
    return lines


def digest_text(state: dict[str, Any]) -> str:
    state = _validate_state(state)
    claims = claims_data(state)
    nodes = state["nodes"]
    open_gaps = [gap for gap in _all_gaps(state) if gap.get("status") == "open"]
    lines = []
    if open_gaps:
        lines.extend([
            "## 未处理的缺口",
            *[f"- {gap.get('id')}：{gap.get('what')}（下一步：{gap.get('next_question')}）" for gap in open_gaps],
            "- 这些缺口还没有补，结论里要当作已知空白写清楚，不能当成已经查过。",
            "",
        ])
    lines = lines + [
        "# 调研汇总骨架",
        "",
        "## 1. 调研目的与交付物要求",
        f"- 调研目的：{state.get('purpose', '').strip() or '（未设置）'}",
        f"- 交付物要求：{state.get('deliverable', '').strip() or '（未设置）'}",
        "",
        "## 2. 结论清单",
    ]
    if not claims:
        lines.append("（无）")
    for claim in claims:
        lines.extend([
            f"- **[{claim.get('id')}]** {claim.get('text', '')}",
            f"  - 证据：{', '.join(claim.get('evidence') or []) or '（无）'}；反证：{', '.join(claim.get('counter_evidence') or []) or '（无）'}",
            f"  - 支撑：{claim.get('support') or '（未标注）'}；版本：{claim.get('version') or '（未设置）'}；日期：{claim.get('as_of') or '（未设置）'}；缺口：{', '.join(claim.get('gaps') or []) or '（无）'}",
        ])
    lines.extend(["", "## 3. 分项细节骨架"])
    content_nodes = [
        node for node in nodes
        if node.get("status") == "answered" and node.get("kind", "content") == "content"
    ]
    verification_nodes = [node for node in nodes if node.get("kind") == "verification"]
    if not content_nodes:
        lines.append("（无 content 类型已答节点）" if verification_nodes else "（无已答节点）")
    for node in content_nodes:
        review = node.get("review") or {}
        citations = review.get("citations") or []
        unresolved = review.get("unresolved") or []
        lines.extend([
            "",
            f"### {node.get('id')}：{node.get('question', '')}",
            f"- 结论：{str(node.get('result') or '').strip() or '（未填写）'}",
            f"- 证据引用：{', '.join(citations) or '（未标注）'}",
            f"- 未解决：{'；'.join(unresolved) or '（无）'}",
            f"- 版本/日期：{state.get('version_label') or '（未设置）'}｜{', '.join(state.get('version_dates') or []) or '（未设置）'}",
        ])
    if verification_nodes:
        lines.extend(["", "### 核实记录"])
        for node in verification_nodes:
            review = node.get("review") or {}
            citations = review.get("citations") or []
            lines.extend([
                f"- 问题：{node.get('question', '')}",
                f"  - 结论：{node.get('result', '')}",
                f"  - 引用：{', '.join(citations)}",
            ])
    lines.extend(["", "## 4. 量化数据"])
    quant_gaps = [
        (node, gap) for node in nodes
        for gap in (node.get("review", {}).get("gaps") or [])
        if isinstance(gap, dict) and gap.get("type") == "quant"
    ]
    if not quant_gaps:
        lines.append("（无）")
    for node, gap in quant_gaps:
        lines.extend(_format_gap_markdown(node, gap))
        related = _gap_claims(state, node, gap)
        lines.append("  - 相关 claim：" + (", ".join(claim.get("id", "") for claim in related) or "（无）"))
    lines.extend(["", "## 5. 矛盾与不确定"])
    conflict_gaps = [
        (node, gap) for node in nodes
        for gap in (node.get("review", {}).get("gaps") or [])
        if isinstance(gap, dict) and gap.get("type") == "conflict"
    ]
    contested_claims = [claim for claim in claims if claim.get("support") == "contested"]
    if not conflict_gaps:
        lines.append("- conflict 缺口：（无）")
    else:
        lines.append("- conflict 缺口：")
        for node, gap in conflict_gaps:
            lines.extend(_format_gap_markdown(node, gap))
    if not contested_claims:
        lines.append("- contested claim：（无）")
    else:
        lines.append("- contested claim：")
        lines.extend(f"  - [{claim.get('id')}] {claim.get('text', '')}" for claim in contested_claims)
    lines.extend(["", "## 6. 未解决"])
    unresolved_nodes = [
        node for node in nodes if node.get("status") in {"not_found", "closed_unresolved"}
    ]
    if not unresolved_nodes:
        lines.append("（无）")
    for node in unresolved_nodes:
        lines.append(f"- `{node.get('id')}` [{node.get('status')}] {node.get('question', '')}")
        unresolved = node.get("review", {}).get("unresolved") or []
        if unresolved:
            lines.extend(f"  - {item}" for item in unresolved)
        else:
            lines.append("  - unresolved：（无）")
    lines.extend(["", "## 7. 来源清单"])
    unique_sources: dict[str, dict[str, Any]] = {}
    for item in state["items"]:
        raw_url = str(item.get("url") or item.get("canonical") or "")
        try:
            canonical = canonicalize_url(str(item.get("canonical") or raw_url))
        except ValueError:
            canonical = raw_url.strip()
        if not canonical:
            continue
        current = unique_sources.get(canonical)
        if current is None or _item_body_chars(item) > _item_body_chars(current):
            unique_sources[canonical] = item
    if not unique_sources:
        lines.append("（无）")
    for canonical, item in sorted(unique_sources.items()):
        domain = _host(canonical) or "-"
        published = item.get("published") or item.get("published_raw") or "-"
        lines.append(
            f"- {canonical}｜标题：{item.get('title') or '（无标题）'}｜域名：{domain}"
            f"｜字数：{_item_body_chars(item)}｜日期：{published}"
        )
    body_count = sum(_item_has_body(item) for item in unique_sources.values())
    lines.extend([
        "",
        f"节点: {len(nodes)}｜证据: {len(unique_sources)}｜正文: {body_count}｜结论: {len(claims)}",
        _progress_summary_line(state),
    ])
    return _safe_text("\n".join(lines))


def show_data(state: dict[str, Any], node_id: str, chars: int = 1500) -> dict[str, Any]:
    if chars < 0:
        raise ValueError("--chars cannot be negative")
    node = _node_index(state).get(node_id)
    if node is None:
        raise ValueError(f"unknown node: {node_id}")
    item_index = {str(item.get("id")): item for item in state["items"]}
    question_tokens = _question_tokens(str(node.get("question") or ""))
    ranked = []
    for position, item_id in enumerate(node.get("evidence_ids", [])):
        item = item_index.get(str(item_id))
        if item is None:
            continue
        body = str(item.get("body") or "")
        overlap = len(question_tokens & _question_tokens(str(item.get("title") or "")))
        ranked.append((-min(len(body), 100000), not bool(body), -overlap, position, item, body))
    ranked.sort(key=lambda entry: entry[:4])
    evidence = []
    for _negative_len, _has_no_body, _negative_overlap, _position, item, body in ranked:
        row = {
            "id": item.get("id"),
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "domain": _host(str(item.get("canonical") or "")),
            "published": item.get("published") or item.get("published_raw"),
            "version_note": _version_age_note(
                state, item.get("published") or item.get("published_raw")
            ),
            "chars": len(body),
            "method": item.get("method", ""),
            "body_preview": body[:chars],
            "body_truncated": len(body) > chars,
        }
        for metric in ("quality", "noise_ratio"):
            if metric in item:
                row[metric] = item[metric]
        evidence.append(row)
    result = {
        "node_id": node_id,
        "question": node.get("question", ""),
        "status": node.get("status", ""),
        "evidence_count": len(evidence),
        "evidence": evidence,
    }
    node_claims = claims_data(state, node_id)
    if node_claims:
        result["claims"] = node_claims
    gaps = node.get("review", {}).get("gaps") or []
    if gaps:
        result["gaps"] = gaps
    return result


def _body_status(item: dict[str, Any]) -> str:
    if item["chars"] == 0:
        return "[无正文]"
    if item["body_truncated"]:
        return f"[截断 {item['chars']}→{len(item['body_preview'])}]"
    return f"[正文 {item['chars']} 字]"


def show_text(data: dict[str, Any]) -> str:
    lines = [
        f"{data['node_id']} [{data['status']}] {data['question']}",
        f"证据 {data['evidence_count']} 条",
    ]
    if not data["evidence"]:
        lines.append("（没有证据）")
    for index, item in enumerate(data["evidence"], 1):
        lines.append(f"{index}. {item['title']} {_body_status(item)}")
        details = [
            f"域名：{item['domain'] or '-'}",
            f"发布时间：{item['published'] or '-'}",
            f"版本时效：{item['version_note']}",
            f"chars={item['chars']}",
            f"method={item['method'] or '-'}",
        ]
        for metric in ("quality", "noise_ratio"):
            if metric in item:
                details.append(f"{metric}={item[metric]}")
        lines.append("   " + " | ".join(details))
        preview = item["body_preview"]
        if item["body_truncated"]:
            preview += "…"
        lines.append("   正文：" + (preview.replace("\n", "\n         ") if preview else "（无正文）"))
    if data.get("gaps"):
        lines.append("审查缺口")
        for gap in data["gaps"]:
            questions = "; ".join(gap.get("questions") or [])
            lines.append(
                f"[{gap.get('id')}] {gap.get('type')}｜target={gap.get('target')}"
                f"｜why={gap.get('why')}｜待补：{questions}"
            )
    if data.get("claims"):
        lines.append("结论账本")
        lines.append(claims_text(data["claims"]))
    return _safe_text("\n".join(lines))


def prompt_text(
    state: dict[str, Any], node_id: str, chars: int = 1200, *, include_citation_map: bool = False,
) -> str:
    data = show_data(state, node_id, chars=chars)
    node = _node_index(state)[node_id]
    context = node.get("context")
    context = context.strip() if isinstance(context, str) else ""
    acceptance = node.get("acceptance")
    acceptance = [value for value in acceptance if isinstance(value, str)] \
        if isinstance(acceptance, list) else []

    lines = [
        "# 调研任务书",
        "",
        "## 调研目的",
        state.get("purpose", "").strip() or "（未设置）",
        "",
        "## 交付物要求",
        state.get("deliverable", "").strip() or "（未设置）",
        "",
        *_brief_task_lines(state.get("topic_brief")),
        "",
        "## 问题",
        str(data["question"] or ""),
        "",
        "## 背景",
        context or "无",
        "",
        "## 树层",
        f"当前第 {int(node.get('depth') or 0) + 1} 层，最低要求 {MIN_LAYERS} 层。",
        "",
        "## 完成标准",
    ]
    lines.extend(f"- {value}" for value in acceptance or ["证据能直接回答问题"])
    lines.extend([
        "",
        "## 你的角色",
        "你是执行这次调研的 agent。你只能依据下面列出的证据作答；证据不足必须直说，禁止用常识或记忆补。",
        "",
        f"## 证据（共 {data['evidence_count']} 条，正文优先、标题相关度其次）",
    ])
    if not data["evidence"]:
        lines.append("（无证据，先报告 not_found 并给出 1-3 个可执行的子问题）")
    else:
        for display_index, item in enumerate(data["evidence"], 1):
            index = display_index
            body = item["body_preview"]
            body = "\n".join(line.strip() for line in body.splitlines() if line.strip())
            if item["body_truncated"]:
                body += f"…（已截断，全文 {item['chars']} 字）"
            lines.extend([
                f"[E{index}] {item['title']} {_body_status(item)}",
                *([f"E{index} → {item['id']}"] if include_citation_map else []),
                f"域名：{item['domain'] or '-'}｜时间：{item['published'] or '-'}｜版本时效：{item['version_note']}｜字数：{item['chars']}｜抓取方式：{item['method']}",
                body,
            ])
            if display_index < data["evidence_count"]:
                lines.append("")
    lines.extend([
        "",
        "## 输出要求（只输出 JSON，不要解释）",
        "{",
        f'  "node_id": {json.dumps(node_id, ensure_ascii=False)},',
        '  "verdict": "answered | partial | not_found",',
        '  "result": "3-5 句结论，必须能追溯到证据编号",',
        '  "citations": ["E1", "E3"],',
        '  "unresolved": ["还差什么；answered 时可为空"],',
        '  "early_close_reason": "仅在显式使用 --allow-early-close 时说明提前封口理由",',
        '  "children": [',
        '    {"question": "能独立执行的子问题", "context": "为什么需要它", "acceptance": ["怎样算答到了"], "scope_delta": "相对父问题新增了什么范围"}',
        "  ]",
        "}",
        "",
        "判定口径：",
        "- answered：结论被证据直接支持，且关键数字/事实至少有 2 个独立域名；",
        "- partial：答了一部分，仍缺关键项，必须在 unresolved 里写清缺什么；",
        "- not_found：证据里没有能回答的内容。",
        "",
        f"children 规则：partial / not_found 且当前层数小于 {MIN_LAYERS} 时至少给 1 个；否则可不给；最多 3 个。每个子问题必须自包含（换一个 agent 只看这一条也能执行）；不得与父问题或祖先重复。",
        "提前封口必须由调用者显式添加 --allow-early-close，并在 early_close_reason 写明理由。",
        "",
        "## 禁止",
        "- 不得引用证据清单以外的来源；",
        "- 不得把标题、摘要当成正文证据；",
        "- 不得凭记忆或常识补事实；",
        "- 不得编造 citations（引用的编号必须真实存在）；",
        "- 不得输出与问题无关的内容。",
    ])
    return _safe_text("\n".join(lines))


def emit_tasks(state_path: Path | str, out_dir: Path | str) -> dict[str, Any]:
    state = read_state(state_path)
    target_dir = Path(out_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    tasks = []
    for node in state["nodes"]:
        if node.get("status") != "open":
            continue
        node_id = str(node["id"])
        parent = node.get("parent") or "(root)"
        file_path = target_dir / f"{node_id}.md"
        content = (
            f"# Node {node_id}\n\nParent: {parent}\nDepth: {int(node.get('depth') or 0)}\n\n"
            + prompt_text(state, node_id, include_citation_map=True)
            + "\n"
        )
        file_path.write_text(content, encoding="utf-8", newline="\n")
        tasks.append({
            "node_id": node_id,
            "depth": int(node.get("depth") or 0),
            "file": str(file_path),
            "evidence_count": len(show_data(state, node_id)["evidence"]),
        })
    return {"tasks": tasks, "suggested_concurrency": min(4, len(tasks))}


def _task_document(
    state: dict[str, Any], node_id: str, task_id: str, state_path: Path,
) -> str:
    node = _node_index(state).get(node_id)
    if node is None:
        raise ValueError(f"unknown node: {node_id}")
    evidence = show_data(state, node_id, chars=1500)["evidence"]
    context = node.get("context") if isinstance(node.get("context"), str) else ""
    acceptance = node.get("acceptance")
    acceptance = acceptance if isinstance(acceptance, list) else []
    lock_path = _browser_lock_path(state_path).resolve()

    def physical_lines(value: Any) -> list[str]:
        return str(value or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")

    lines = [
        f"# Node {node_id} collection task",
        "",
        "## 调研目的",
        state.get("purpose", "").strip() or "（未设置）",
        "",
        "## 交付物要求",
        state.get("deliverable", "").strip() or "（未设置）",
        "",
        *_brief_task_lines(state.get("topic_brief")),
        "",
        "## Task identity",
        f"- task_id: {task_id}",
        f"- state_path: {state_path}",
        f"- node_id: {node_id}",
        f"- depth: {int(node.get('depth') or 0)}",
        "- question:",
        "- background:",
        "- acceptance criteria:",
    ]
    question_index = lines.index("- question:") + 1
    lines[question_index:question_index] = [f"  {line}" for line in physical_lines(node.get("question"))]
    background_index = lines.index("- background:") + 1
    lines[background_index:background_index] = [f"  {line}" for line in physical_lines(context or "(none)")]
    acceptance_values = [criterion for criterion in acceptance if isinstance(criterion, str)]
    for criterion in acceptance_values:
        parts = physical_lines(criterion)
        lines.append(f"  - {parts[0]}")
        lines.extend(f"    {line}" for line in parts[1:])
    if not acceptance_values:
        lines.append("  - Collect direct evidence relevant to the question.")
    lines.extend([
        "",
        "## Permissions",
        "Only collect information and read evidence. Do not review, decide a verdict, or change the tree structure.",
        f'- Allowed: `python research.py run --state "{state_path}" --node {node_id} --fetch N`',
        f'- Allowed: `python research.py show --state "{state_path}" --node {node_id}`',
        f'- Allowed: `python research.py report --state "{state_path}"`',
        "- Forbidden: `apply-review`, `reset`, `claim`, `init`, `set-node`.",
        "",
        "## Evidence",
        "Only cite the [E#] identifiers listed below. Do not cite sources outside this list, and never treat a title as a conclusion.",
    ])
    if not evidence:
        lines.append("(No evidence is currently attached to this node.)")
    for index, item in enumerate(evidence, 1):
        lines.extend([
            "",
            f"[E{index}] {item['title'] or '(untitled)'}",
            f"URL: {item['url'] or '-'}",
            f"body_chars: {item['chars']} | method: {item['method'] or '-'}",
            "Body preview:",
            *[f"  {line}" for line in physical_lines(item["body_preview"] or "(no body)")],
        ])
    lines.extend([
        "",
        "## Return format",
        "Return only this JSON shape. Do not include a verdict or conclusion:",
        "```json",
        "{",
        f'  "task_id": {json.dumps(task_id, ensure_ascii=False)},',
        f'  "node_id": {json.dumps(node_id, ensure_ascii=False)},',
        '  "queries_run": [],',
        '  "sources": [{"title": "", "url": "", "body_chars": 0, "method": ""}],',
        '  "bodies": 0,',
        '  "diagnostics": []',
        "}",
        "```",
        "",
        "## Concurrency and browser lock",
        "Only one child agent may work on this node at a time.",
        f"OPENCLI_LOCK_PATH={lock_path}",
        "Browser-channel workers must share this OPENCLI_LOCK_PATH value; do not create or use another browser lock path.",
    ])
    return _safe_text("\n".join(lines) + "\n")


def emit_task(state_path: Path | str, node_id: str, out_file: Path | str) -> dict[str, Any]:
    resolved_state_path = Path(state_path).resolve()
    state = read_state(resolved_state_path)
    node = _node_index(state).get(node_id)
    if node is None:
        raise ValueError(f"unknown node: {node_id}")
    task_id = f"task-{uuid.uuid4().hex}"
    file_path = Path(out_file).resolve()
    if file_path == resolved_state_path:
        raise ValueError("--out must not overwrite the state file")
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(
        _task_document(state, node_id, task_id, resolved_state_path),
        encoding="utf-8", newline="\n",
    )
    return {
        "task_id": task_id,
        "node_id": node_id,
        "depth": int(node.get("depth") or 0),
        "state_path": str(resolved_state_path),
        "file": str(file_path),
        "mode": "collect_only",
        "evidence_count": len(show_data(state, node_id)["evidence"]),
    }


def record_task_run(
    state_path: Path | str, task_file: Path | str, *, worker: str = "unspecified", note: str = "",
) -> dict[str, Any]:
    payload = json.loads(Path(task_file).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("task report must be a JSON object")
    task_id = payload.get("task_id")
    node_id = payload.get("node_id")
    sources = payload.get("sources")
    diagnostics = payload.get("diagnostics", [])
    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError("task report requires task_id")
    if not isinstance(node_id, str) or not node_id.strip():
        raise ValueError("task report requires node_id")
    if not isinstance(sources, list) or not all(isinstance(source, dict) for source in sources):
        raise ValueError("task report sources must be a list of objects")
    if not isinstance(diagnostics, list) or not all(isinstance(value, str) for value in diagnostics):
        raise ValueError("task report diagnostics must be a list of strings")
    body_lengths = []
    for source in sources:
        body_chars = source.get("body_chars")
        if type(body_chars) is not int or body_chars < 0:
            raise ValueError("each task report source requires non-negative integer body_chars")
        body_lengths.append(body_chars)
    path = Path(state_path)
    with FileLock(_state_lock_path(path)):
        state = _read_state_unlocked(path)
        if node_id not in _node_index(state):
            raise ValueError(f"unknown node: {node_id}")
        entry = {
            "task_id": task_id,
            "node_id": node_id,
            "worker": worker,
            "returned_at": _now(),
            "source_count": len(sources),
            "body_count": sum(body_chars > 0 for body_chars in body_lengths),
            "body_chars_total": sum(body_lengths),
            "diagnostics": list(diagnostics),
            "worker_note": note,
        }
        state["task_runs"] = [row for row in state["task_runs"] if row["task_id"] != task_id]
        state["task_runs"].append(entry)
        _write_state_unlocked(path, state)
    return entry


def _console_utf8() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass
    if hasattr(sys.stderr, "reconfigure"):
        try:
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def _parse_platforms(raw: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(part.strip().lower() for part in raw.split(",") if part.strip()))


def _parse_channels(raw: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(part.strip().lower() for part in raw.split(",") if part.strip()))


def _parse_refs(raw: str) -> list[str]:
    return list(dict.fromkeys(part.strip() for part in raw.split(",") if part.strip()))


def _version_date_argument(value: str) -> str:
    try:
        return _iso_date(value, "--version-date")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _doctor_check(name: str, status: str, detail: str, hint: str) -> dict[str, str]:
    return {"name": name, "status": status, "detail": _safe_text(detail), "hint": hint}


def _dotenv_value(name: str) -> str:
    return config.get(name)


def _dotenv_has_value(name: str) -> bool:
    return bool(_dotenv_value(name))


def _doctor_opencli_command() -> list[str]:
    if os.name == "nt" and OPENCLI_NODE.is_file() and OPENCLI_ENTRY.is_file():
        return [str(OPENCLI_NODE), str(OPENCLI_ENTRY)]
    return [str(OPENCLI_PATH)]


def login_session_checks() -> list[dict[str, str]]:
    if os.name != "nt":
        chrome_check = _doctor_check(
            "chrome_running", "warn", "unsupported",
            "Chrome process detection uses tasklist and is supported on Windows only.",
        )
    else:
        try:
            completed = subprocess.run(
                ["tasklist"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=15, check=False,
            )
            chrome_running = completed.returncode == 0 and bool(
                re.search(r"(?im)^\s*chrome\.exe\s", completed.stdout or "")
            )
            detail = "Chrome.exe process found" if chrome_running else "Chrome.exe process not found"
            if completed.returncode != 0:
                detail = f"tasklist exit={completed.returncode}: {_safe_text(completed.stderr or completed.stdout)[:200]}"
            chrome_check = _doctor_check(
                "chrome_running", "ok" if chrome_running else "fail", detail,
                "Open Chrome and confirm it is signed in to the target site." if not chrome_running else "No action needed.",
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            chrome_check = _doctor_check(
                "chrome_running", "fail", f"tasklist failed: {_safe_text(exc)}",
                "Open Chrome and confirm it is signed in to the target site.",
            )

    opencli_command = [*_doctor_opencli_command(), "browser", "main", "eval", "1+1"]
    try:
        completed = subprocess.run(
            opencli_command, cwd=SCRIPT_DIR, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=15, check=False,
        )
        output = (completed.stdout or "").strip()
        try:
            decoded_output = json.loads(output)
        except json.JSONDecodeError:
            decoded_output = output
        if decoded_output == 2 or decoded_output == "2":
            output = "2"
        elif isinstance(decoded_output, str):
            output = decoded_output.strip()
        elif isinstance(decoded_output, dict):
            result_value = decoded_output.get("result")
            output = "2" if result_value == 2 or result_value == "2" else output
        elif output.startswith('"'):
            try:
                decoded = json.loads(output)
            except json.JSONDecodeError:
                decoded = output
            if isinstance(decoded, str):
                output = decoded.strip()
        session_ok = completed.returncode == 0 and output == "2"
        detail = f"eval exit={completed.returncode}; output={_safe_text(output)[:120] or '(empty)'}"
        opencli_check = _doctor_check(
            "opencli_session", "ok" if session_ok else "fail", detail,
            "Open Chrome, confirm the target site is signed in, and rerun doctor." if not session_ok else "No action needed.",
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        opencli_check = _doctor_check(
            "opencli_session", "fail", f"eval failed: {_safe_text(exc)}",
            "Open Chrome, confirm the target site is signed in, and rerun doctor.",
        )
    return [chrome_check, opencli_check]



def _human_requirements(capability_rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    """Build the user-facing requirement list. One ready search provider satisfies both."""
    pending_human = [
        row for row in capability_rows
        if row["state"] == "missing" and row["tier"] in {"required", "user_key"}
    ]
    search_keys = {"TAVILY_API_KEY", "SEARXNG_URL"}
    search_ready = any(row["state"] == "ready" and row["key"] in search_keys for row in capability_rows)
    if search_ready:
        pending_human = [row for row in pending_human if row["key"] not in search_keys]
    search_missing = {row["key"] for row in pending_human} >= search_keys
    if search_missing:
        pending_human = [row for row in pending_human if row["key"] not in search_keys]
        requirements = [{
            "key": "SEARCH_PROVIDER",
            "required": True,
            "tier": "required",
            "status": "missing",
            "asked_as": "需要你给我一个搜索 key，二选一即可：Tavily（tavily.com 免费注册）或一个 SearXNG 地址（自己 docker 起也行）。",
            "how_to_fix": "echo '{\"TAVILY_API_KEY\":\"...\"}' | research config set --stdin  或设置 SEARXNG_URL",
            "missing_effect": "两个都没有则无法搜索，调研无法开始。",
            "can_decline": False,
            "decline_command": "",
        }]
    else:
        requirements = []
    requirements += [
        {
            "key": row["key"],
            "required": row["tier"] == "required",
            "tier": row["tier"],
            "status": "missing",
            "asked_as": row["asked_as"],
            "how_to_fix": row["how_to_get"],
            "missing_effect": row["missing_effect"],
            "can_decline": True,
            "decline_command": f"research decline-placeholder --key {row['key']}",
        }
        for row in pending_human
    ]
    for row in requirements:
        if row["can_decline"]:
            row["decline_command"] = f"research config decline --key {row['key']} --reason \"用户无法提供\""
    return requirements, bool(pending_human) or search_missing


def doctor_data() -> dict[str, Any]:
    checks = []
    python_ok = sys.version_info >= (3, 10)
    checks.append(_doctor_check(
        "python_version", "ok" if python_ok else "fail", platform.python_version(),
        "Install Python 3.10 or newer and rerun doctor." if not python_ok else "No action needed.",
    ))

    script_exists = SCRIPT_DIR.is_dir()
    checks.append(_doctor_check(
        "script_directory", "ok" if script_exists else "fail", str(SCRIPT_DIR),
        "No action needed." if script_exists else "Restore the scripts directory or run research.py from its installed location.",
    ))
    state_dir_ok = False
    data_dir = config.data_dir()
    state_dir_detail = str(data_dir)
    probe_path = None
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=data_dir, prefix=".research-doctor-", delete=False,
        ) as handle:
            probe_path = Path(handle.name)
            handle.write("ok")
        probe_path.unlink()
        state_dir_ok = True
    except OSError as exc:
        state_dir_detail = f"{data_dir}: {_safe_text(exc)}"
        if probe_path is not None:
            probe_path.unlink(missing_ok=True)
    checks.append(_doctor_check(
        "state_directory_writable", "ok" if state_dir_ok else "fail", state_dir_detail,
        "No action needed." if state_dir_ok else "Grant write access to the default state directory or choose a writable --state path.",
    ))

    for filename in ("fetch_smart.py", "collect.py", "collect_browser.py"):
        script_path = SCRIPT_DIR / filename
        if not script_path.is_file():
            checks.append(_doctor_check(
                filename, "fail", "script is missing", f"Restore {filename} beside research.py.",
            ))
            continue
        try:
            completed = subprocess.run(
                [sys.executable, str(script_path), "--help"],
                cwd=SCRIPT_DIR, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=15, check=False,
            )
            started = completed.returncode == 0
            detail = f"present; --help exit={completed.returncode}"
            if not started:
                detail += ": " + _safe_text((completed.stderr or completed.stdout or "").strip())[:240]
        except (OSError, subprocess.TimeoutExpired) as exc:
            started = False
            detail = f"present; --help failed: {_safe_text(exc)}"
        checks.append(_doctor_check(
            filename, "ok" if started else "fail", detail,
            "No action needed." if started else "Repair the script's Python dependencies or restore the script, then rerun doctor.",
        ))

    searxng_url = (os.environ.get("SEARXNG_URL", "").strip() or _dotenv_value("SEARXNG_URL"))
    tavily_key = bool(os.environ.get("TAVILY_API_KEY", "").strip()) or _dotenv_has_value("TAVILY_API_KEY")
    search_ok = tavily_key or bool(searxng_url)
    checks.append(_doctor_check(
        "search_provider",
        "ok" if search_ok else "fail",
        "tavily" if tavily_key else ("searxng" if searxng_url else "none"),
        "Set TAVILY_API_KEY or SEARXNG_URL; at least one search provider is required." if not search_ok else "No action needed.",
    ))
    checks.append(_doctor_check(
        "TAVILY_API_KEY", "ok" if tavily_key else "warn",
        "set" if tavily_key else "not set",
        "Optional when SEARXNG_URL is configured." if not tavily_key else "No action needed.",
    ))
    for name in ("GROQ_API_KEY", "AGNES_API_KEY"):
        present = bool(os.environ.get(name, "").strip()) or _dotenv_has_value(name)
        checks.append(_doctor_check(
            name, "ok" if present else "warn", "set" if present else "not set",
            f"Optional: set {name} to enable its provider features." if not present else "No action needed.",
        ))

    opencli_command = _doctor_opencli_command()
    opencli_paths_exist = all(Path(part).is_file() for part in opencli_command)
    checks.append(_doctor_check(
        "opencli_path", "ok" if opencli_paths_exist else "warn",
        " + ".join(opencli_command) if opencli_paths_exist else "OpenCLI executable or entry point is missing",
        "No action needed." if opencli_paths_exist else "Install OpenCLI and its Node entry point at the configured path.",
    ))
    if opencli_paths_exist:
        try:
            completed = subprocess.run(
                [*opencli_command, "--help"], cwd=SCRIPT_DIR, capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=15, check=False,
            )
            opencli_started = completed.returncode == 0
            opencli_detail = f"--help exit={completed.returncode}"
            if not opencli_started:
                opencli_detail += ": " + _safe_text((completed.stderr or completed.stdout or "").strip())[:240]
        except (OSError, subprocess.TimeoutExpired) as exc:
            opencli_started = False
            opencli_detail = f"--help failed: {_safe_text(exc)}"
    else:
        opencli_started = False
        opencli_detail = "not run because the configured OpenCLI path is missing"
    checks.append(_doctor_check(
        "opencli_help", "ok" if opencli_started else "warn", opencli_detail,
        "No action needed." if opencli_started else "Run OpenCLI --help directly and repair the Node installation or configured path.",
    ))
    checks.extend(login_session_checks())

    lock_override = os.environ.get("OPENCLI_LOCK_PATH", "").strip()
    lock_detail = f"default={{state_path}}.browser.lock; override={'set' if lock_override else 'unset'}"
    checks.append(_doctor_check(
        "OPENCLI_LOCK_PATH", "ok", lock_detail,
        "No action needed; browser workers use the state-specific lock by default.",
    ))

    notebook_url = os.environ.get("OPEN_NOTEBOOK_URL", "").strip() or _dotenv_value("OPEN_NOTEBOOK_URL")
    if not notebook_url:
        checks.append(_doctor_check(
            "OPEN_NOTEBOOK_URL", "ok", "unset",
            "Set OPEN_NOTEBOOK_URL to enable Open Notebook text extraction.",
        ))
    else:
        request_slot = None
        notebook_status = "warn"
        notebook_detail = "GET /api/notebooks failed"
        try:
            try:
                from . import fetch_smart
            except ImportError:
                import fetch_smart

            request_slot = fetch_smart._acquire_notebook_slot(time.monotonic() + 5.0)
            response = requests.get(
                f"{notebook_url.rstrip('/')}/api/notebooks", timeout=5,
            )
            status_code = int(getattr(response, "status_code", 0) or 0)
            notebook_status = "ok" if 200 <= status_code < 300 else "warn"
            notebook_detail = f"GET /api/notebooks returned HTTP {status_code}"
        except Exception as exc:
            notebook_detail = f"GET /api/notebooks failed ({exc.__class__.__name__})"
        finally:
            if request_slot is not None:
                try:
                    request_slot.release()
                except Exception:
                    pass
        checks.append(_doctor_check(
            "OPEN_NOTEBOOK_URL", notebook_status, notebook_detail,
            "No action needed." if notebook_status == "ok" else "Check that Open Notebook is running and OPEN_NOTEBOOK_URL is correct.",
        ))
    capability_rows = capabilities.statuses()
    requirements, needs_user = _human_requirements(capability_rows)
    pending_agent = [
        row for row in capability_rows
        if row["state"] == "missing" and row["tier"] in {"agent_install", "local_service"}
    ]
    return {
        "ok": all(check["status"] != "fail" for check in checks) and capabilities.search_ready(),
        "checks": checks,
        "capabilities": capability_rows,
        "pending_agent_setup": [
            {"key": row["key"], "tier": row["tier"], "how_to_fix": row["how_to_get"],
             "missing_effect": row["missing_effect"]}
            for row in pending_agent
        ],
        "needs_user": needs_user,
        "requirements": requirements,
        "config": config.describe(),
    }


def build_parser() -> ResearchArgumentParser:
    parser = ResearchArgumentParser(description="Tree-based research with explicit agent review.")
    subparsers = parser.add_subparsers(dest="command", required=True, parser_class=ResearchArgumentParser)

    init_parser = subparsers.add_parser("init", help="create a research tree")
    init_parser.add_argument("question")
    init_parser.add_argument("--state", metavar="FILE")
    init_parser.add_argument("--max-depth", type=int, default=DEFAULT_MAX_DEPTH)
    init_parser.add_argument(
        "--version-date", action="append", type=_version_date_argument, metavar="YYYY-MM-DD",
        help="研究基准日（判断信息时效性的锚点），不是标准发布/实施日；可重复传多个",
    )
    init_parser.add_argument("--version-label", help="给这组基准日起个名字，如 标准版本或调研批次")
    init_parser.add_argument("--purpose", default="", metavar="TEXT")
    init_parser.add_argument("--deliverable", default="", metavar="TEXT")
    init_parser.add_argument("--brief", metavar="FILE", help="主题解析卡 JSON（research brief --topic 生成）")
    init_parser.add_argument("--platforms", default=",".join(DEFAULT_PLATFORMS))
    init_parser.add_argument("--channels", default=",".join(DEFAULT_CHANNELS))
    init_parser.add_argument("--detail", type=int, default=0)
    init_parser.add_argument("--images", type=int, default=0)
    init_parser.add_argument("--vision", type=int, default=0)
    init_parser.add_argument("--json", action="store_true", dest="as_json")

    run_parser = subparsers.add_parser("run", help="collect evidence for node(s)")
    run_parser.add_argument("--state", required=True, metavar="FILE")
    selection = run_parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--node", metavar="ID")
    selection.add_argument("--ready", action="store_true")
    run_parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="hard timeout in seconds per node")
    run_parser.add_argument(
        "--fetch", type=int, default=DEFAULT_FETCH_COUNT,
        help="fetch bodies for up to N items ranked by title overlap",
    )
    run_parser.add_argument("--no-retry", action="store_true", help="disable the one-time web retry for zero-result nodes")
    run_parser.add_argument(
        "--scroll", type=int, default=None, metavar="N",
        help="scroll N screens (0-10) in the logged-in browser when fetching bodies",
    )
    run_parser.add_argument("--json", action="store_true", dest="as_json")
    run_parser.add_argument("--full", action="store_true", help="print the entire state instead of a node summary")

    config_parser = subparsers.add_parser("config", help="inspect or write configuration without echoing secrets")
    config_actions = config_parser.add_subparsers(dest="config_action", required=True)
    config_show = config_actions.add_parser("where", help="show config file locations")
    config_show.add_argument("--json", action="store_true", dest="as_json")
    config_decline = config_actions.add_parser("decline", help="mark a capability as unavailable for this user")
    config_decline.add_argument("--key", required=True)
    config_decline.add_argument("--reason", default="")
    config_decline.add_argument("--json", action="store_true", dest="as_json")
    config_set = config_actions.add_parser("set", help="write key=value pairs from stdin JSON")
    config_set.add_argument("--stdin", action="store_true", required=True)
    config_set.add_argument("--json", action="store_true", dest="as_json")

    setup_parser = subparsers.add_parser("setup", help="list or walk through every configurable capability")
    setup_parser.add_argument("--json", action="store_true", dest="as_json")
    setup_parser.add_argument("--interactive", action="store_true")
    setup_parser.add_argument("--only", default="", help="comma separated capability keys")

    brief_parser = subparsers.add_parser("brief", help="build or check the topic brief before writing queries")
    brief_parser.add_argument("--topic", default="", help="用户原话主题，用于生成空白模板")
    brief_parser.add_argument("--file", default="", help="已有 brief JSON")
    brief_parser.add_argument("--check", action="store_true", help="只做结构校验")
    brief_parser.add_argument("--matrix", action="store_true", help="生成机械查询候选")
    brief_parser.add_argument("--facet", action="append", default=[], help="子问题或检索面，可重复")
    brief_parser.add_argument("--budget", type=int, default=8, help="最多返回多少条候选")
    brief_parser.add_argument("--out", metavar="FILE")
    brief_parser.add_argument("--json", action="store_true", dest="as_json")

    gaps_parser = subparsers.add_parser("gaps", help="declare, spawn and resolve research gaps after a wave")
    gaps_parser.add_argument("--state", required=True, metavar="FILE")
    gaps_parser.add_argument("--file", metavar="FILE", help="本轮缺口 JSON")
    gaps_parser.add_argument("--spawn", action="store_true", help="把记录过的开放缺口变成子节点")
    gaps_parser.add_argument("--gap", action="append", default=[], help="只派生指定缺口，可重复")
    gaps_parser.add_argument("--resolve", metavar="GAP_ID")
    gaps_parser.add_argument("--status", default="", help="covered 或 dropped")
    gaps_parser.add_argument("--note", default="")
    gaps_parser.add_argument("--json", action="store_true", dest="as_json")

    clean_parser = subparsers.add_parser("clean", help="delete stale *.lock files left by previous runs")
    clean_parser.add_argument("--state", required=True, metavar="FILE")
    clean_parser.add_argument("--older-than", type=float, default=600.0, metavar="SECONDS")
    clean_parser.add_argument("--json", action="store_true", dest="as_json")

    reset_parser = subparsers.add_parser("reset", help="reopen node(s) for collection")
    reset_parser.add_argument("--state", required=True, metavar="FILE")
    reset_parser.add_argument("--node", required=True, action="append", dest="nodes", metavar="ID")
    reset_parser.add_argument("--json", action="store_true", dest="as_json")

    review_parser = subparsers.add_parser("apply-review", help="apply the main agent's verdicts")
    review_parser.add_argument("--state", required=True, metavar="FILE")
    review_parser.add_argument("--file", required=True, metavar="REVIEWS.json")
    review_parser.add_argument("--allow-early-close", action="store_true")
    review_parser.add_argument("--json", action="store_true", dest="as_json")

    show_parser = subparsers.add_parser("show", help="show a node's evidence")
    show_parser.add_argument("--state", required=True, metavar="FILE")
    show_parser.add_argument("--node", required=True, metavar="ID")
    show_parser.add_argument("--chars", type=int, default=1500)
    show_parser.add_argument("--json", action="store_true", dest="as_json")

    prompt_parser = subparsers.add_parser("prompt", help="render a node task brief")
    prompt_parser.add_argument("--state", required=True, metavar="FILE")
    prompt_parser.add_argument("--node", required=True, metavar="ID")
    prompt_parser.add_argument("--chars", type=int, default=1200)
    prompt_parser.add_argument("--out", metavar="FILE")
    prompt_parser.add_argument("--json", action="store_true", dest="as_json")

    tasks_parser = subparsers.add_parser("emit-tasks", help="write one task brief for each open node")
    tasks_parser.add_argument("--state", required=True, metavar="FILE")
    tasks_parser.add_argument("--out", required=True, metavar="DIR")
    tasks_parser.add_argument("--json", action="store_true", dest="as_json")

    task_parser = subparsers.add_parser("emit-task", help="write one collect-only task brief")
    task_parser.add_argument("--state", required=True, metavar="FILE")
    task_parser.add_argument("--node", required=True, metavar="ID")
    task_parser.add_argument("--out", required=True, metavar="FILE")
    task_parser.add_argument("--json", action="store_true", dest="as_json")

    task_run_parser = subparsers.add_parser("task-run", help="record a returned child-agent task report")
    task_run_parser.add_argument("--state", required=True, metavar="FILE")
    task_run_parser.add_argument("--task", required=True, metavar="FILE")
    task_run_parser.add_argument("--worker", default="unspecified", metavar="NAME")
    task_run_parser.add_argument("--note", default="", metavar="TEXT")
    task_run_parser.add_argument("--json", action="store_true", dest="as_json")

    set_node_parser = subparsers.add_parser("set-node", help="set node-level collection channels and queries")
    set_node_parser.add_argument("--state", required=True, metavar="FILE")
    set_node_parser.add_argument("--node", required=True, metavar="ID")
    set_node_parser.add_argument("--kind", choices=sorted(NODE_KINDS), default="content")
    set_node_parser.add_argument("--channels")
    set_node_parser.add_argument("--queries", nargs="*", action="extend")
    set_node_parser.add_argument("--counter-queries", nargs="*")
    set_node_parser.add_argument("--scroll", type=int, default=None, metavar="N",
                                 help="scroll N screens (0-10) when fetching this node's bodies")
    set_node_parser.add_argument("--json", action="store_true", dest="as_json")

    claim_parser = subparsers.add_parser("claim", help="add or remove a conclusion claim")
    claim_actions = claim_parser.add_subparsers(
        dest="claim_action", required=True, parser_class=ResearchArgumentParser,
    )
    claim_add_parser = claim_actions.add_parser("add", help="add a conclusion claim")
    claim_add_parser.add_argument("--state", required=True, metavar="FILE")
    claim_add_parser.add_argument("--node", required=True, metavar="ID")
    claim_add_parser.add_argument("--text", required=True)
    claim_add_parser.add_argument("--evidence", required=True)
    claim_add_parser.add_argument("--counter", default="")
    claim_add_parser.add_argument("--version")
    claim_add_parser.add_argument("--as-of")
    claim_add_parser.add_argument("--support", choices=sorted(CLAIM_SUPPORTS), default="")
    claim_add_parser.add_argument("--gaps", default="", metavar="G1,G2")
    claim_add_parser.add_argument("--json", action="store_true", dest="as_json")
    claim_delete_parser = claim_actions.add_parser("delete", aliases=["remove", "rm"], help="delete a conclusion claim")
    claim_delete_parser.add_argument("--state", required=True, metavar="FILE")
    claim_delete_parser.add_argument("--id", required=True, metavar="CLAIM_ID")
    claim_delete_parser.add_argument("--json", action="store_true", dest="as_json")

    claims_parser = subparsers.add_parser("claims", help="list conclusion claims")
    claims_parser.add_argument("--state", required=True, metavar="FILE")
    claims_parser.add_argument("--json", action="store_true", dest="as_json")

    report_parser = subparsers.add_parser("report", help="print the research tree")
    report_parser.add_argument("--state", required=True, metavar="FILE")
    report_parser.add_argument("--json", action="store_true", dest="as_json")

    status_parser = subparsers.add_parser("status", help="summarize tree progress and next steps")
    status_parser.add_argument("--state", required=True, metavar="FILE")
    status_parser.add_argument("--json", action="store_true", dest="as_json")

    digest_parser = subparsers.add_parser("digest", help="render a markdown summary skeleton")
    digest_parser.add_argument("--state", required=True, metavar="FILE")
    digest_parser.add_argument("--out", metavar="FILE")
    digest_parser.add_argument("--json", action="store_true", dest="as_json")
    digest_parser.add_argument("--allow-open-gaps", action="store_true",
                               help="明知有未处理缺口仍要出稿；缺口会写在汇总顶部")

    doctor_parser = subparsers.add_parser("doctor", help="check local research prerequisites")
    doctor_parser.add_argument("--json", action="store_true", dest="as_json")

    spec_parser = subparsers.add_parser("spec", help="print the machine-readable CLI contract")
    spec_parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _argument_specs(parser: argparse.ArgumentParser) -> list[dict[str, Any]]:
    specs = []
    for action in parser._actions:
        if action.dest == "help" or isinstance(action, argparse._SubParsersAction):
            continue
        is_option = bool(action.option_strings)
        spec: dict[str, Any] = {
            "name": action.option_strings[0] if is_option else action.dest,
            "required": bool(action.required) if is_option else action.nargs not in ("?", "*"),
        }
        if is_option and len(action.option_strings) > 1:
            spec["aliases"] = action.option_strings[1:]
        if action.choices is not None:
            spec["choices"] = sorted(action.choices)
        if action.default is not None and action.default is not argparse.SUPPRESS:
            spec["default"] = action.default
        specs.append(spec)
    return specs


def _required_groups(parser: argparse.ArgumentParser) -> list[list[str]]:
    groups = []
    for group in parser._mutually_exclusive_groups:
        if not group.required:
            continue
        names = [
            action.option_strings[0] if action.option_strings else action.dest
            for action in group._group_actions
        ]
        groups.append(names)
    return groups


def _command_specs(parser: argparse.ArgumentParser) -> dict[str, dict[str, Any]]:
    top_action = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
    commands: dict[str, dict[str, Any]] = {}
    for name, command_parser in top_action.choices.items():
        if command_parser.prog.rsplit(" ", 1)[-1] != name:
            continue
        nested = next(
            (action for action in command_parser._actions if isinstance(action, argparse._SubParsersAction)), None,
        )
        if nested is None:
            arguments = _argument_specs(command_parser)
            commands[name] = {
                "arguments": arguments,
                "required_groups": _required_groups(command_parser),
                "supports_json": any(argument["name"] == "--json" for argument in arguments),
            }
            continue
        for child_name, child_parser in nested.choices.items():
            canonical = child_parser.prog.rsplit(" ", 1)[-1]
            if child_name != canonical:
                continue
            aliases = [alias for alias, candidate in nested.choices.items() if candidate is child_parser and alias != canonical]
            arguments = _argument_specs(child_parser)
            commands[f"{name} {canonical}"] = {
                "arguments": arguments,
                "required_groups": _required_groups(child_parser),
                "supports_json": any(argument["name"] == "--json" for argument in arguments),
                **({"aliases": aliases} if aliases else {}),
            }
    return commands


def contract_data(parser: argparse.ArgumentParser | None = None) -> dict[str, Any]:
    parser = parser or build_parser()
    commands = _command_specs(parser)
    claim_arguments = commands["claim add"]["arguments"]
    return {
        "schema_version": SCHEMA_VERSION,
        "commands": commands,
        "collection": {
            "platforms": sorted(SUPPORTED_PLATFORMS),
            "channels": sorted(RESEARCH_CHANNELS),
            "default_platforms": list(DEFAULT_PLATFORMS),
            "default_channels": list(DEFAULT_CHANNELS),
            "channel_table": {
                "web": {
                    "applies_to": "通用网页搜索",
                    "login_required": False,
                    "default_timeout_seconds": 24,
                    "timeout_scope": "search_dual overall timeout; each provider request defaults to 20 seconds",
                },
                "bili": {
                    "applies_to": "Bilibili 搜索与视频条目",
                    "login_required": False,
                    "default_timeout_seconds": 30,
                    "timeout_scope": "collect.py channel timeout",
                },
                "youtube": {
                    "applies_to": "YouTube 搜索与视频条目",
                    "login_required": False,
                    "default_timeout_seconds": 30,
                    "timeout_scope": "collect.py channel timeout",
                },
                "hn": {
                    "applies_to": "Hacker News 搜索",
                    "login_required": False,
                    "default_timeout_seconds": 30,
                    "timeout_scope": "collect.py channel timeout",
                },
                "github": {
                    "applies_to": "GitHub 仓库搜索",
                    "login_required": False,
                    "default_timeout_seconds": 30,
                    "timeout_scope": "collect.py channel timeout",
                },
                "rss": {
                    "applies_to": "RSS feed 搜索",
                    "login_required": False,
                    "default_timeout_seconds": 30,
                    "timeout_scope": "collect.py channel timeout",
                },
                "stats": {
                    "applies_to": "结构化统计数据搜索与抓取",
                    "login_required": False,
                    "default_timeout_seconds": 75,
                    "timeout_scope": "collect.py stats channel timeout",
                },
                "browser": {
                    "applies_to": "小红书、知乎与 Reddit 的登录态浏览器搜索",
                    "login_required": True,
                    "default_timeout_seconds": 90,
                    "timeout_scope": "collect_browser.py total timeout; each platform defaults to 30 seconds",
                },
            },
            "channel_resolution": [
                "use non-empty node.channels",
                "otherwise use non-empty state.channels",
                "otherwise use web and mark a default-channel fallback",
            ],
        },
        "node_statuses": sorted(NODE_STATUSES),
        "status_transitions": {
            name: {
                "from": sorted(transition["from"]),
                "to": sorted(transition["to"]),
                "trigger": {
                    "run": "run changes open to awaiting_review",
                    "apply-review": "apply-review changes awaiting_review to a terminal verdict",
                    "reset": "reset changes any node status to open",
                }[name],
            }
            for name, transition in STATUS_TRANSITIONS.items()
        },
        "review_schema": {
            "layer_rule": {
                "minimum_layers": MIN_LAYERS,
                "applies_to_verdicts": ["partial", "not_found"],
                "requires_child_below_minimum": True,
                "early_close_flag": "--allow-early-close",
                "early_close_reason_field": "early_close_reason",
            },
            "accepted_payload_forms": [
                "one review object with node_id (or id)",
                "a list of review objects",
                "an object with a reviews list",
                "an object keyed by node id with review objects as values",
            ],
            "fields": {
                "node_id": {"required": True, "aliases": ["id"]},
                "verdict": {"required": True, "enum": sorted(VERDICTS)},
                "result": {"required": False, "type": "string"},
                "rationale": {"required": False, "type": "string"},
                "citations": {"required": False, "type": "array of E# strings"},
                "unresolved": {"required": False, "type": "array of strings"},
                "children": {
                    "required": False,
                    "type": "array of objects",
                    "fields": {
                        "question": {"required": True, "type": "non-empty self-contained string"},
                        "scope_delta": {"required": True, "type": "non-empty string"},
                        "context": {"required": False, "type": "string"},
                        "acceptance": {"required": False, "type": "array of strings"},
                    },
                    "maximum": 3,
                },
                "early_close_reason": {"required": False, "type": "string"},
                "gaps": {"required": False, "type": "array of gap objects"},
            },
            "citations": {
                "required": False,
                "format": "E#",
                "rules": [
                    "each reference must belong to the reviewed node's current displayed evidence",
                    "references are also extracted from result, rationale, and child text other than question",
                    "references belonging to another node or absent from this node are rejected",
                ],
            },
            "gaps": {
                "required": False,
                "fields": {
                    "id": {"required": True, "pattern": "g\\d+", "unique_within_review": True},
                    "type": {"required": True, "enum": sorted(GAP_TYPES)},
                    "why": {"required": True, "type": "non-empty string"},
                    "target": {
                        "required": True,
                        "type": "existing local E# evidence reference or existing c# claim id",
                    },
                    "questions": {
                        "required": True,
                        "type": "array of strings with at least one non-empty value",
                    },
                },
            },
        },
        "claim_schema": {
            "arguments": claim_arguments,
            "rules": {
                "--text": "must be non-empty",
                "--evidence": "one or more comma-separated E# references local to --node",
                "--counter": "optional comma-separated E# references local to --node",
                "--version": "optional version label",
                "--as-of": "optional date label",
                "--support": {"enum": sorted(CLAIM_SUPPORTS)},
                "--gaps": "optional comma-separated existing g# ids",
            },
        },
        "evidence_ref_rule": {
            "format": "E#",
            "meaning": "display sequence for evidence attached to this node",
            "stability": "may change when evidence is re-sorted or collection is rerun",
            "instruction": "use only identifiers from the task brief for the current run",
        },
        "exit_codes": {str(code): meaning for code, meaning in EXIT_CODES.items()},
        "result_contract": {
            "state_document": {
                "fields": ["state_version", "root", "channels", "nodes", "items", "claims"],
                "node_fields": ["id", "status", "question", "channels", "evidence_ids"],
            },
            "doctor": {
                "format": "direct JSON object (not the standard command envelope)",
                "fields": {"ok": "boolean", "checks": "array of name/status/detail/hint objects"},
                "check_statuses": ["ok", "warn", "fail"],
                "exit_codes": {"0": "no failed checks", "1": "one or more checks failed"},
            },
            "init": {"data_fields": ["state_path", "root_id", "max_depth", "min_layers"]},
            "set-node": {"data_fields": ["id", "status", "channels", "kind"]},
            "run": {
                "state_document": "same fields as result_contract.state_document",
                "data_fields": [
                    "channels_used", "channels_by_node", "fallback_by_node", "fallback_default", "errors",
                    "layers", "pending_review", "pending_split",
                ],
                "errors": "array of node-prefixed collection or fetch diagnostics; failures also set error.errors",
            },
            "status": {
                "data_fields": [
                    "layers", "unmet_reasons", "pending_split", "pending_split_nodes", "open_nodes",
                    "pending_review", "next_step", "suggested_commands", "next_command",
                ],
            },
            "show": {
                "data_fields": ["node_id", "question", "status", "evidence_count", "evidence"],
                "evidence_fields": ["id", "title", "url", "domain", "published", "chars", "method", "body_preview"],
                "evidence_ids": "display references are E1, E2, ... in evidence order",
            },
            "apply-review": {
                "data": "updated state_document; node status follows status_transitions",
                "node_fields": ["id", "status"],
            },
            "claim add": {
                "data_fields": ["id", "node_id", "text", "evidence", "counter_evidence", "support", "gaps"],
            },
            "digest": {"data_fields": ["text", "output_path"], "section_count": 7},
        },
        "output_envelope": {
            "success": {
                "ok": True, "schema_version": SCHEMA_VERSION, "command": "<command>", "data": "<command data>",
            },
            "failure": {
                "ok": False,
                "schema_version": SCHEMA_VERSION,
                "command": "<command>",
                "error": {
                    "code": "<exit code>", "message": "<error>", "hint": "<actionable repair>",
                    "next_command": "<executable command>", "errors": "optional collection diagnostics",
                    "channels_used": "optional run channel summary", "fallback_default": "optional run fallback flag",
                },
            },
        },
    }


def spec_summary(contract: dict[str, Any]) -> str:
    return "\n".join([
        f"Research CLI contract schema v{SCHEMA_VERSION}",
        f"Commands: {', '.join(contract['commands'])}",
        f"Statuses: {', '.join(contract['node_statuses'])}",
        f"Transitions: run open→awaiting_review; apply-review awaiting_review→terminal; reset→open",
        f"Exit codes: {', '.join(f'{code}={meaning}' for code, meaning in contract['exit_codes'].items())}",
        "Full contract: python research.py spec --json",
    ])


def _command_name(args: argparse.Namespace | None, argv: list[str], parser: argparse.ArgumentParser) -> str:
    if args is not None and getattr(args, "command", None):
        if args.command == "claim":
            action = getattr(args, "claim_action", "")
            return "claim delete" if action in {"delete", "remove", "rm"} else f"claim {action}".strip()
        return str(args.command)
    top_action = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
    for index, token in enumerate(argv):
        command_parser = top_action.choices.get(token)
        if command_parser is None:
            continue
        if token == "claim":
            nested = next(action for action in command_parser._actions if isinstance(action, argparse._SubParsersAction))
            for action_token in argv[index + 1:]:
                if action_token in nested.choices:
                    action_name = nested.choices[action_token].prog.rsplit(" ", 1)[-1]
                    return f"claim {action_name}"
        return token
    return "research"


def _json_requested(args: argparse.Namespace | None, argv: list[str]) -> bool:
    return bool(getattr(args, "as_json", False)) or "--json" in argv


def _print_success(command: str, data: Any) -> None:
    print(json.dumps({
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "data": _safe_tree(data),
    }, ensure_ascii=False, indent=2))


def _shell_path(value: str | Path | None) -> str:
    text = _safe_text(str(value or ""))
    return f'"{text}"'


def _help_command(command: str) -> str:
    return "python research.py --help" if command == "research" else f"python research.py {command} --help"


def _review_node_id(args: argparse.Namespace | None) -> str:
    if args is not None and getattr(args, "node", None):
        return str(args.node)
    if args is not None and getattr(args, "file", None):
        try:
            reviews = _unpack_reviews(json.loads(Path(args.file).read_text(encoding="utf-8")))
            if reviews:
                return str(reviews[0].get("node_id") or reviews[0].get("id") or "n0")
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    return "n0"


def _error_record(
    exc: Exception, args: argparse.Namespace | None, command: str,
) -> dict[str, Any]:
    state_path = getattr(args, "state", None) if args is not None else None
    review_node_id = _review_node_id(args)
    state = None
    if state_path:
        try:
            state = read_state(state_path)
        except (OSError, ValueError):
            pass
    message = _safe_text(exc)

    if isinstance(exc, ResearchCLIError) and exc.code != 2:
        return {
            "code": exc.code,
            "message": message,
            "hint": exc.hint or "Inspect the saved research state and retry after resolving the reported issue.",
            "next_command": exc.next_command or f"python research.py report --state {_shell_path(state_path)}",
        }

    status_match = re.search(r"node (n\d+) is not awaiting_review", message)
    if status_match:
        node_id = status_match.group(1)
        current = next((node for node in (state or {}).get("nodes", []) if node.get("id") == node_id), None)
        status = str(current.get("status")) if current else "unknown"
        path_arg = _shell_path(state_path)
        report = f"python research.py report --state {path_arg}"
        reset = f"python research.py reset --state {path_arg} --node {node_id}"
        run = f"python research.py run --state {path_arg} --node {node_id}"
        return {
            "code": 3,
            "message": message,
            "hint": f"当前状态为 {status}。查看状态：{report}；重开后重采集：{reset}，然后 {run}。",
            "next_command": report,
        }

    if "bad_refs:" in message:
        refs = list(dict.fromkeys(re.findall(r'"ref"\s*:\s*"([^"]+)"', message)))
        node_id = _review_node_id(args)
        path_arg = _shell_path(state_path)
        prompt = f"python research.py prompt --state {path_arg} --node {node_id}"
        invalid = ", ".join(refs) if refs else "unknown reference"
        return {
            "code": 2,
            "message": message,
            "hint": f"无效引用编号：{invalid}。重新查看该节点当前展示的证据编号，再修正 review/claim：{prompt}。",
            "next_command": prompt,
        }

    layer_match = re.search(r"未达到\s*(\d+)\s*层", message)
    if layer_match:
        required_layers = int(layer_match.group(1))
        node_id = _review_node_id(args)
        current = next((node for node in (state or {}).get("nodes", []) if node.get("id") == node_id), None)
        current_layer = int(current.get("depth") or 0) + 1 if current else 1
        path_arg = _shell_path(state_path)
        prompt = f"python research.py prompt --state {path_arg} --node {node_id}"
        file_arg = _shell_path(getattr(args, "file", None) if args is not None else None)
        early_close = (
            f"python research.py apply-review --state {path_arg} --file {file_arg} --allow-early-close"
        )
        return {
            "code": 2,
            "message": message,
            "hint": (
                f"当前第 {current_layer} 层，最低要求 {required_layers} 层。继续拆分请查看任务书：{prompt}；"
                f"若需提前结束，请在 review 中填写 early_close_reason 后执行：{early_close}。"
            ),
            "next_command": prompt,
        }

    if isinstance(exc, ResearchCLIError):
        return {
            "code": exc.code,
            "message": message,
            "hint": exc.hint or "检查参数、文件路径和输入 JSON 后重试。",
            "next_command": exc.next_command or _help_command(command),
        }

    if command == "run" and isinstance(exc, OSError):
        return {
            "code": 2,
            "message": message,
            "hint": "检查采集器、网络和配置后重试；状态文件保留了本次任务结果。",
            "next_command": f"python research.py report --state {_shell_path(state_path)}",
        }
    return {
        "code": 2,
        "message": message,
        "hint": "检查命令参数、状态文件和输入 JSON 后重试。",
        "next_command": _help_command(command),
    }


def _print_error(exc: Exception, args: argparse.Namespace | None, command: str, as_json: bool) -> int:
    error = _error_record(exc, args, command)
    if isinstance(exc, ResearchCLIError) and exc.errors:
        error["errors"] = [_safe_text(value) for value in exc.errors]
    if isinstance(exc, ResearchCLIError):
        error.update(_safe_tree(exc.metadata))
    if as_json:
        error_payload = dict(error)
        for field in ("message", "hint", "next_command"):
            error_payload[field] = _safe_text(error_payload[field])
        print(json.dumps({
            "ok": False,
            "schema_version": SCHEMA_VERSION,
            "command": command,
            "error": error_payload,
        }, ensure_ascii=False, indent=2))
    else:
        print(f"research: {_safe_text(error['message'])}", file=sys.stderr)
        print(f"提示: {_safe_text(error['hint'])}", file=sys.stderr)
        print(f"下一步: {_safe_text(error['next_command'])}", file=sys.stderr)
    return int(error["code"])


def _run_collection_failure(
    state: dict[str, Any], node_ids: list[str]
) -> tuple[ResearchCLIError | None, list[str]]:
    nodes = _node_index(state)
    evidence_with_body = {
        str(item.get("id"))
        for item in state.get("items", []) or []
        if int(item.get("body_chars") or 0) > 0
    }
    failures = []
    warnings: list[str] = []
    errors = []
    for node_id in node_ids:
        node = nodes.get(node_id)
        if node is None or node.get("status") != "awaiting_review":
            continue
        run_errors = node.get("run_errors") or []
        fetch_errors = node.get("fetch_errors") or []
        if run_errors or fetch_errors:
            details = [f"{node_id}: {_safe_text(detail)}" for detail in [*run_errors, *fetch_errors]]
            usable = [item_id for item_id in node.get("evidence_ids") or [] if str(item_id) in evidence_with_body]
            if usable:
                warnings.extend(details)
                continue
            failures.append(node_id)
            errors.extend(details)
            continue
        result = str(node.get("result") or "")
        if result.startswith("采集诊断：") or fetch_errors:
            failures.append(node_id)
    for node_id in failures:
        if not any(detail.startswith(f"{node_id}:") for detail in errors):
            result = str(nodes[node_id].get("result") or "")
            if result:
                errors.append(f"{node_id}: {_safe_text(result)}")
    if not failures:
        return None, warnings
    return ResearchCLIError(
        f"collection or network failure for node(s): {', '.join(failures)}",
        code=4,
        hint="采集诊断和已保存证据可通过 report/show 检查；修复渠道、配置或网络后执行 reset 再 run。",
        next_command=f"python research.py report --state {_shell_path(state.get('_path'))}",
        errors=errors,
        metadata={
            "channels_used": state.get("channels_used", []),
            "channels_by_node": state.get("channels_by_node", {}),
            "fallback_default": state.get("fallback_default", False),
        },
    ), warnings


def clean_stale_locks(state_path: Path | str, older_than: float = 600.0) -> list[str]:
    """Remove leftover *.lock files for a state that are older than `older_than` seconds."""
    base = Path(state_path)
    cutoff = time.time() - older_than
    removed: list[str] = []
    for path in base.parent.glob(base.name + "*.lock"):
        try:
            if path.stat().st_mtime <= cutoff and path.is_file():
                path.unlink()
                removed.append(path.name)
        except OSError:
            continue
    return removed


def _run_view(state: dict[str, Any]) -> dict[str, Any]:
    """Compact run/apply-review payload: node statuses and counts, not whole bodies."""
    body_by_node: dict[str, int] = {}
    for item in state.get("items", []) or []:
        chars = int(item.get("body_chars") or 0)
        if chars <= 0:
            continue
        for ref in item.get("node_refs") or []:
            node_id = str(ref.get("node_id")) if isinstance(ref, dict) else ""
            if node_id:
                body_by_node[node_id] = body_by_node.get(node_id, 0) + 1
    nodes = []
    for node in state.get("nodes", []) or []:
        node_id = str(node.get("id"))
        nodes.append({
            "id": node_id,
            "status": node.get("status"),
            "depth": int(node.get("depth") or 0),
            "evidence": len(node.get("evidence_ids") or []),
            "bodies": body_by_node.get(node_id, 0),
            "fetch_errors": len(node.get("fetch_errors") or []),
        })
    return {
        "nodes": nodes,
        "counts": {
            "nodes": len(nodes),
            "layers": max((int(node.get("depth") or 0) + 1 for node in state.get("nodes", [])), default=0),
            "evidence": len(state.get("items", []) or []),
            "claims": len(state.get("claims", []) or []),
        },
        "warnings": state.get("warnings", []),
        "errors": state.get("errors", []),
        "channels_used": state.get("channels_used", []),
        "channels_by_node": state.get("channels_by_node", {}),
        "fallback_by_node": state.get("fallback_by_node", {}),
        "fallback_default": state.get("fallback_default", False),
        "layers": progress_data(state)["layers"],
        "pending_review": progress_data(state)["pending_review"],
        "pending_split": progress_data(state)["pending_split"],
    }


def main(argv: list[str] | None = None) -> int:
    _console_utf8()
    argv_values = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args: argparse.Namespace | None = None
    try:
        args = parser.parse_args(argv_values)
    except ResearchCLIError as exc:
        command = _command_name(None, argv_values, parser)
        return _print_error(exc, None, command, _json_requested(None, argv_values))

    command = _command_name(args, argv_values, parser)
    try:
        if args.command == "doctor":
            data = doctor_data()
            if args.as_json:
                print(json.dumps(data, ensure_ascii=False, indent=2))
            else:
                for check in data["checks"]:
                    print(f"{check['status'].upper()} {check['name']}: {check['detail']} | {check['hint']}")
            if data["needs_user"]:
                return 6
            return 0 if data["ok"] else 1
        if args.command == "spec":
            contract = contract_data(parser)
            if args.as_json:
                _print_success("spec", contract)
            else:
                print(spec_summary(contract))
            return 0
        if args.command == "gaps":
            if args.resolve:
                if not args.status:
                    raise ResearchCLIError("--resolve 需要 --status covered|dropped", code=2)
                data = {"gap": resolve_gap(args.state, args.resolve, args.status, args.note)}
            elif args.file or args.spawn:
                data = {}
                if args.file:
                    data.update(record_gaps(args.state, _load_gaps_file(args.file)))
                if args.spawn:
                    data.update(spawn_gaps(args.state, args.gap or None))
                if not data:
                    raise ResearchCLIError("--spawn 没有可派生的缺口", code=2)
            else:
                data = gap_observations(read_state(args.state))
            if args.as_json:
                _print_success("gaps", data)
            else:
                print(json.dumps(data, ensure_ascii=False, indent=2))
            return 0
        if args.command == "brief":
            brief = _load_brief_file(args.file) if args.file else None
            if args.matrix:
                if brief is None:
                    raise ResearchCLIError("--matrix 需要 --file", code=2)
                data = topic_brief.matrix(brief, args.facet, max(1, args.budget))
            elif args.check:
                if brief is None:
                    raise ResearchCLIError("--check 需要 --file", code=2)
                data = topic_brief.validate(brief)
            else:
                if not args.topic.strip():
                    raise ResearchCLIError("生成模板需要 --topic", code=2)
                data = topic_brief.template(args.topic)
            if args.out:
                Path(args.out).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            if args.as_json:
                _print_success(command, data)
            else:
                print(json.dumps(data, ensure_ascii=False, indent=2))
            return 0 if data.get("ok", True) else 2
        if args.command == "init":
            if args.max_depth < MIN_LAYERS - 1:
                print(
                    f"警告：--max-depth={args.max_depth} 小于 {MIN_LAYERS - 1}；"
                    "partial/not_found 提前封口仍需 --allow-early-close 和理由。",
                    file=sys.stderr,
                )
            state = init_state(
                args.question,
                args.state,
                args.max_depth,
                _parse_platforms(args.platforms),
                {"detail": args.detail, "images": args.images, "vision": args.vision},
                _parse_channels(args.channels) if args.channels is not None else None,
                args.version_date,
                args.version_label,
                args.purpose,
                args.deliverable,
                _load_brief_file(args.brief) if args.brief else None,
            )
            state_path = Path(args.state) if args.state else _default_state_path(args.question)
            data = {
                "state_path": str(state_path.resolve()),
                "root_id": state["root"],
                "max_depth": state["max_depth"],
                "min_layers": MIN_LAYERS,
            }
            if args.as_json:
                _print_success(command, data)
            else:
                print(report_text(state, include_claims=False))
            return 0
        if args.command == "run":
            if args.node:
                node_ids = [args.node]
            else:
                node_ids = [node["id"] for node in read_state(args.state)["nodes"] if node.get("status") == "open"]
            if args.scroll is not None:
                os.environ["FETCH_SCROLL"] = str(args.scroll)
            state = run_nodes(
                args.state, node_ids, timeout=args.timeout, fetch=args.fetch,
                retry_zero_results=not args.no_retry,
            )
            failure, run_warnings = _run_collection_failure({**state, "_path": args.state}, node_ids)
            if run_warnings:
                declined = capabilities.declined()
                state["warnings"] = [
                    warning for warning in run_warnings
                    if not any(key in str(warning) for key in declined)
                ]
            if failure:
                raise failure
            if args.as_json:
                _print_success(command, state if args.full else _run_view(state))
            else:
                print(report_text(state, include_claims=False))
            return 0
        if args.command == "setup":
            rows = capabilities.statuses()
            if args.only:
                wanted = {item.strip() for item in args.only.split(",") if item.strip()}
                rows = [row for row in rows if row["key"] in wanted]
            if args.interactive:
                for row in rows:
                    if row["state"] == "ready":
                        print(f"[已就绪] {row['label']}")
                        continue
                    print(f"[待配置] {row['label']}（{row['key']}）")
                    print(f"  用途：{row['why']}")
                    print(f"  获取：{row['how_to_get']}")
                    print(f"  不配的后果：{row['missing_effect']}")
                    answer = input("  粘贴配置值（直接回车=跳过，填 skip=以后不再问）：").strip()
                    if answer.lower() == "skip":
                        capabilities.decline(row["key"], "user skipped in setup")
                        print("  已标记为放弃，后续不再提示。")
                    elif answer:
                        config.write_value(row["key"], answer)
                        print("  已写入用户配置。")
                return 0
            pending_rows = [row for row in rows if row["state"] != "ready"]
            data = {"pending": pending_rows, "ready": [row["key"] for row in rows if row["state"] == "ready"]}
            if args.as_json:
                _print_success(command, data)
            else:
                for row in pending_rows:
                    print(f"[{row['state']}] {row['label']} ({row['key']})：{row['asked_as']}")
            return 0
        if args.command == "config":
            if args.config_action == "decline":
                capabilities.decline(args.key, args.reason)
                data = {"declined": sorted(capabilities.declined())}
                if args.as_json:
                    _print_success(command, data)
                else:
                    print(json.dumps(data, ensure_ascii=False, indent=2))
                return 0
            if args.config_action == "where":
                data = config.describe()
            else:
                raw = sys.stdin.read()
                values = json.loads(raw or "{}")
                if not isinstance(values, dict) or not values:
                    raise ResearchCLIError("stdin must be a JSON object with at least one key", code=2)
                written = []
                for key, value in values.items():
                    if not isinstance(key, str) or not isinstance(value, str) or not key.strip():
                        raise ResearchCLIError("keys and values must be non-empty strings", code=2)
                    config.write_value(key.strip(), value.strip())
                    written.append(key.strip())
                data = {"written": written, "config_file": str(config.config_file())}
            if args.as_json:
                _print_success(command, data)
            else:
                print(json.dumps(data, ensure_ascii=False, indent=2))
            return 0
        if args.command == "clean":
            removed = clean_stale_locks(args.state, args.older_than)
            data = {"removed": removed, "count": len(removed)}
            if args.as_json:
                _print_success(command, data)
            else:
                print(f"清理 {len(removed)} 个过期锁文件")
            return 0
        if args.command == "reset":
            state = reset_nodes(args.state, args.nodes)
            if args.as_json:
                _print_success(command, state)
            else:
                print(report_text(state, include_claims=False))
            return 0
        if args.command == "apply-review":
            payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
            state = apply_reviews(args.state, payload, allow_early_close=args.allow_early_close)
            if args.as_json:
                _print_success(command, state)
            else:
                print(report_text(state, include_claims=False))
            return 0
        if args.command == "show":
            data = show_data(read_state(args.state), args.node, chars=args.chars)
            if args.as_json:
                _print_success(command, data)
            else:
                print(show_text(data))
            return 0
        if args.command == "prompt":
            rendered = prompt_text(read_state(args.state), args.node, chars=args.chars)
            if args.out:
                with Path(args.out).open("w", encoding="utf-8", newline="\n") as handle:
                    handle.write(rendered + "\n")
            if args.as_json:
                _print_success(command, {
                    "node_id": args.node,
                    "output_path": str(Path(args.out).resolve()) if args.out else None,
                    "text": rendered if not args.out else None,
                })
            elif not args.out:
                print(rendered)
            return 0
        if args.command == "emit-tasks":
            data = emit_tasks(args.state, args.out)
            if args.as_json:
                _print_success(command, data)
            else:
                print(json.dumps(_safe_tree(data), ensure_ascii=False))
            return 0
        if args.command == "emit-task":
            data = emit_task(args.state, args.node, args.out)
            if args.as_json:
                _print_success(command, data)
            else:
                print(json.dumps(_safe_tree(data), ensure_ascii=False))
            return 0
        if args.command == "task-run":
            data = record_task_run(args.state, args.task, worker=args.worker, note=args.note)
            if args.as_json:
                _print_success(command, data)
            else:
                print(json.dumps(_safe_tree(data), ensure_ascii=False))
            return 0
        if args.command == "set-node":
            state = set_node(
                args.state, args.node,
                channels=_parse_channels(args.channels) if args.channels is not None else None,
                queries=args.queries,
                counter_queries=args.counter_queries,
                kind=args.kind,
                scroll=args.scroll,
            )
            data = _node_index(state)[args.node]
            if args.as_json:
                _print_success(command, data)
            else:
                print(json.dumps(_safe_tree(data), ensure_ascii=False, indent=2))
            return 0
        if args.command == "claim":
            if args.claim_action == "add":
                state = add_claim(
                    args.state, args.node, args.text, _parse_refs(args.evidence),
                    _parse_refs(args.counter), args.version, args.as_of,
                    args.support, _parse_refs(args.gaps),
                )
                data = state["claims"][-1]
            else:
                remove_claim(args.state, args.id)
                data = {"deleted": args.id}
            if args.as_json:
                _print_success(command, data)
            else:
                print(json.dumps(_safe_tree(data), ensure_ascii=False, indent=2))
            return 0
        if args.command == "claims":
            data = claims_data(read_state(args.state))
            if args.as_json:
                _print_success(command, data)
            else:
                print(claims_text(data))
            return 0
        if args.command == "status":
            data = progress_data(read_state(args.state), args.state)
            if args.as_json:
                _print_success(command, data)
            else:
                print(status_text(data))
            return 0
        if args.command == "digest":
            state_path = Path(args.state).resolve()
            digest_state = read_state(state_path)
            open_gaps = [gap for gap in _all_gaps(digest_state) if gap.get("status") == "open"]
            if open_gaps and not args.allow_open_gaps:
                listing = "; ".join(f"{gap.get('id')}: {gap.get('what')}" for gap in open_gaps)
                raise ResearchCLIError(
                    "还有未处理的缺口：" + listing + "。先 gaps --spawn 补齐或 --resolve 标 covered/dropped；"
                    "确实要带缺口出稿，加 --allow-open-gaps。", code=3,
                )
            rendered = digest_text(digest_state)
            output_path = None
            if args.out:
                out_path = Path(args.out).resolve()
                if out_path == state_path:
                    raise ValueError("--out must not overwrite the state file")
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(rendered + "\n", encoding="utf-8", newline="\n")
                output_path = str(out_path)
            if args.as_json:
                _print_success(command, {"text": rendered, "output_path": output_path})
            else:
                print(rendered)
            return 0
        state = read_state(args.state)
        if args.as_json:
            _print_success(command, state)
        else:
            print(report_text(state))
        return 0
    except (OSError, ValueError, json.JSONDecodeError, ResearchCLIError) as exc:
        return _print_error(exc, args, command, _json_requested(args, argv_values))


if __name__ == "__main__":
    raise SystemExit(main())


def _load_gaps_file(path: str | Path) -> list[dict[str, Any]]:
    target = Path(path)
    try:
        payload = json.loads(target.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ValueError(f"gaps file not found: {target}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"gaps file is not valid JSON: {target}: {exc}") from exc
    if isinstance(payload, dict):
        payload = payload.get("gaps")
    if not isinstance(payload, list):
        raise ValueError("gaps file must contain a list, or an object with a 'gaps' list")
    return payload
