"""Fetch a URL using Tavily, direct HTTP, then headless Chrome as needed."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

try:  # optional dependency: PDF support degrades, the rest of the tool keeps working
    import fitz
except ImportError:  # pragma: no cover - exercised only on installs without PyMuPDF
    fitz = None  # type: ignore[assignment]

try:
    from . import config
except ImportError:
    import config
import requests
from bs4 import BeautifulSoup


TAVILY_EXTRACT_URL = "https://api.tavily.com/extract"
_MISSING = Path("/__missing__/not-installed")
CHROME_PATH = config.chrome_path() or _MISSING
OPENCLI_PATH = config.resolve_program(
    "OPENCLI_PATH", "opencli",
    candidates=(str(Path.home() / "AppData" / "Local" / "hermes" / "node" / "opencli"),),
) or _MISSING
OPENCLI_NODE = OPENCLI_PATH.parent / "node.exe"
OPENCLI_ENTRY = OPENCLI_PATH.parent / "node_modules/@jackwener/opencli/dist/src/main.js"
HTTP_TIMEOUT_SECONDS = 20
BROWSER_TIMEOUT_SECONDS = 45
FETCH_BUDGET_SECONDS = 30.0
PDF_DOWNLOAD_LIMIT_BYTES = 25 * 1024 * 1024
PDF_PAGE_LIMIT = 200
PDF_CHAR_LIMIT = 120_000
PDF_OCR_PAGE_LIMIT = 8
OPENCLI_TIMEOUT_SECONDS = 60.0
OPENCLI_LOCK_TIMEOUT_SECONDS = 120.0
NOTEBOOK_STATE_PATH = config.data_dir() / ".notebook_state.json"
NOTEBOOK_STATE_LOCK_PATH = config.data_dir() / ".notebook_state.lock"
NOTEBOOK_REQUEST_LOCK_STEM = config.data_dir() / ".notebook_request"
NOTEBOOK_BUDGET_SECONDS = 18.0
NOTEBOOK_DELETE_RESERVE_SECONDS = 1.0
NOTEBOOK_MIN_REMAINING_SECONDS = 5.0
NOTEBOOK_MAX_CONCURRENT = 2
YT_DLP_PATH = config.resolve_program(
    "YT_DLP_PATH", "yt-dlp",
    candidates=(str(Path.home() / ".agent-reach-venv" / "Scripts" / "yt-dlp.exe"),),
) or _MISSING
YT_DLP_PROXY = config.proxy_url()
LOGIN_WALL_MARKERS = (
    "微信登录",
    "请先登录",
    "请登录后查看",
    "登录后查看全文",
    "扫码登录",
    "立即登录",
    "此内容需要登录",
)
CHROME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


class FetchError(RuntimeError):
    """Raised when none of the configured retrieval methods meets the text threshold."""

    def __init__(self, url: str, reasons: list[str]):
        self.url = url
        self.reasons = reasons
        details = "; ".join(reasons) if reasons else "no retrieval method returned content"
        super().__init__(f"Could not fetch usable text from {url}: {details}")


class NotebookError(RuntimeError):
    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        super().__init__(f"{reason}: {detail}" if detail else reason)


_ACTIVE_NOTEBOOK_SOURCES: set[str] = set()
_ACTIVE_NOTEBOOK_SOURCES_LOCK = threading.Lock()
_HTTP_CACHE: dict[str, Any] = {}


@dataclass
class _FetchedText:
    title: str
    text: str
    method: str
    truncated: bool = False
    truncate_reason: str = ""

    def __iter__(self):
        yield self.title
        yield self.text


def _load_local_env() -> None:
    env_path = config.find_env_file()
    if env_path is None:
        return
    try:
        lines = env_path.read_text(encoding="utf-8-sig").splitlines()
    except (FileNotFoundError, OSError):
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


def _required_tavily_key() -> str:
    value = os.environ.get("TAVILY_API_KEY", "").strip()
    if not value:
        raise RuntimeError(
            "Missing required configuration TAVILY_API_KEY; set it in "
            f"{Path(__file__).resolve().parent.parent / '.env'} or the environment."
        )
    return value


def _clean_text(value: str) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(
        r"data:image/[^\s\"'<>;,]+;base64,[A-Za-z0-9+/=_-]+",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    lines = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            lines.append(line)
        elif lines and lines[-1] != "":
            lines.append("")
    while lines and not lines[-1]:
        lines.pop()
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


class _MeasuredText(str):
    def __new__(
        cls,
        value: str,
        noise_ratio: float,
        image_data_count: int,
        base64_chars: int,
    ):
        result = super().__new__(cls, value)
        result.noise_ratio = noise_ratio
        result.image_data_count = image_data_count
        result.base64_chars = base64_chars
        return result


def _effective_chars(text: str) -> int:
    return sum(not char.isspace() for char in text)


def _base64_char_count(text: str) -> int:
    return sum(
        len(match)
        for match in re.findall(r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/=_-]{80,}(?![A-Za-z0-9+/=_-])", text)
    )


def _anchor_noise_ratio(source: str, text: str) -> float:
    labels = []
    labels.extend(re.findall(r"<a\b[^>]*>(.*?)</a\s*>", source, flags=re.IGNORECASE | re.DOTALL))
    labels.extend(re.findall(r"\[([^\]]+)\]\([^)]*\)", source))
    anchor_chars = sum(_effective_chars(_clean_text(re.sub(r"<[^>]+>", " ", label))) for label in labels)
    return anchor_chars / max(_effective_chars(text), 1)


def _measured_text(raw_text: str, noise_ratio: float | None = None) -> _MeasuredText:
    raw_text = str(raw_text or "")
    image_data_count = len(re.findall(r"data:image/", raw_text, flags=re.IGNORECASE))
    base64_chars = _base64_char_count(raw_text)
    text = _clean_text(raw_text)
    if noise_ratio is None:
        noise_ratio = _anchor_noise_ratio(raw_text, text)
    return _MeasuredText(
        text,
        max(0.0, min(1.0, float(noise_ratio))),
        image_data_count,
        base64_chars,
    )


def _quality(text: str) -> float:
    if not text:
        return 0.0
    return sum(char.isalnum() for char in text) / len(text)


READER_PREAMBLE_PREFIXES = (
    "title:",
    "url source:",
    "published time:",
    "markdown content:",
    "warning: target url returned error",
)


def _strip_reader_preamble(text: str) -> str:
    """Drop Jina/Reader-style metadata headers that leak into extracted bodies."""
    lines = str(text or "").splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line:
            index += 1
            continue
        if line.lower().startswith(READER_PREAMBLE_PREFIXES):
            index += 1
            continue
        if line == "X":
            index += 1
            continue
        break
    return "\n".join(lines[index:]) if index else str(text or "")


def _result(
    url: str,
    method: str,
    title: str,
    text: str,
    noise_ratio: float | None = None,
) -> dict[str, Any]:
    raw_text = text
    image_data_count = getattr(raw_text, "image_data_count", None)
    base64_chars = getattr(raw_text, "base64_chars", None)
    if noise_ratio is None:
        noise_ratio = getattr(raw_text, "noise_ratio", None)
    raw_text = _strip_reader_preamble(str(raw_text or ""))
    if image_data_count is None:
        image_data_count = len(re.findall(r"data:image/", raw_text, flags=re.IGNORECASE))
    if base64_chars is None:
        base64_chars = _base64_char_count(raw_text)
    text = _clean_text(raw_text)
    if noise_ratio is None:
        noise_ratio = _anchor_noise_ratio(raw_text, text)
    noise_ratio = max(0.0, min(1.0, float(noise_ratio)))
    quality = _quality(text)
    if noise_ratio > 0.35:
        quality_reason = "link_dense"
    elif image_data_count > 2 or base64_chars > 500:
        quality_reason = "base64_residue"
    elif len(text) < 800:
        quality_reason = "short"
    elif (
        len(text.splitlines()) > 40
        and sum(text.count(term) for term in ("\u4e3b\u7ad9", "\u5546\u57ce", "\u8bba\u575b", "\u767b\u5f55", "\u6ce8\u518c", "\u9996\u9875")) >= 6
    ):
        quality_reason = "template"
    else:
        quality_reason = "ok"
    low_quality = quality_reason != "ok"
    result_method = f"{method}:low" if low_quality and not method.endswith(":low") else method
    hints = {
        label: text.count(marker)
        for label, marker in (
            ("read_more", "阅读全文"),
            ("load_more", "加载更多"),
            ("expand", "展开更多"),
            ("view_more", "查看更多"),
        )
    }
    hints = {key: value for key, value in hints.items() if value}
    return {
        "url": url,
        "method": result_method,
        "chars": len(text),
        "title": str(title or "").strip(),
        "text": text,
        "quality": round(quality, 3),
        "noise_ratio": round(noise_ratio, 4),
        "quality_reason": quality_reason,
        "login_wall": any(marker in text for marker in LOGIN_WALL_MARKERS),
        "low_quality": low_quality,
        "expand_hints": hints,
    }


def _extracted_result(url: str, default_method: str, extracted: Any) -> dict[str, Any]:
    title, text = extracted
    method = str(getattr(extracted, "method", default_method))
    result = _result(url, method, str(title or ""), str(text or ""))
    if method in {"pdf", "pdf-ocr"}:
        result["method"] = method
        if getattr(extracted, "truncated", False):
            result["truncated"] = True
            result["truncate_reason"] = str(getattr(extracted, "truncate_reason", "") or "")
            reasons = getattr(extracted, "truncate_reasons", None)
            if reasons:
                result["truncate_reasons"] = list(reasons)
    return result


def _request_timeout(timeout: float) -> tuple[float, float]:
    connect_timeout = min(5.0, timeout * 0.25)
    read_timeout = max(0.1, min(15.0, timeout - connect_timeout))
    return connect_timeout, read_timeout


def _extract_html(html_text: str | bytes) -> tuple[str, str]:
    soup = BeautifulSoup(html_text, "html.parser")
    title = ""
    if soup.title:
        title = soup.title.get_text(" ", strip=True)
    if not title:
        meta_title = soup.find("meta", attrs={"property": "og:title"})
        if meta_title:
            title = str(meta_title.get("content", "")).strip()

    for breadcrumb in soup.select(".spanlist_1"):
        wrapper = breadcrumb.find_parent("div", class_="title")
        (wrapper or breadcrumb).decompose()

    for node in soup.select(
        "script, style, nav, header, footer, aside, noscript, form, "
        ".nav, .footer, .sidebar, .recommend, .breadcrumb, .breadcrumbs, .crumb, .location, "
        ".time, .intem, .pin, .floating-button-container, .qr-popup, .post, .warp_g"
    ):
        node.decompose()

    selectors = (
        "article",
        "main",
        ".article-content",
        ".rich_media_content",
        "#content",
        ".content",
        ".post-content",
        ".detail-content",
    )
    candidates = []
    for selector in selectors:
        candidates.extend((node.get_text("\n", strip=True), node) for node in soup.select(selector))
    candidates = [(candidate, node) for candidate, node in candidates if _clean_text(candidate)]
    if candidates:
        raw_text, content_node = max(candidates, key=lambda row: len(_clean_text(row[0])))
        anchor_chars = sum(
            _effective_chars(anchor.get_text(" ", strip=True))
            for anchor in content_node.find_all("a")
        )
    else:
        paragraph_nodes = [node for node in soup.select("p") if _clean_text(node.get_text(" ", strip=True))]
        raw_text = "\n\n".join(node.get_text(" ", strip=True) for node in paragraph_nodes)
        anchor_chars = sum(
            _effective_chars(anchor.get_text(" ", strip=True))
            for paragraph in paragraph_nodes
            for anchor in paragraph.find_all("a")
        )
    clean_text = _clean_text(raw_text)
    noise_ratio = anchor_chars / max(_effective_chars(clean_text), 1)
    return title, _measured_text(raw_text, noise_ratio)


def _tavily_extract(url: str, timeout: float) -> tuple[str, str]:
    response = requests.post(
        TAVILY_EXTRACT_URL,
        json={"api_key": _required_tavily_key(), "urls": [url]},
        timeout=_request_timeout(timeout),
    )
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"Tavily extract returned HTTP {response.status_code}")
    payload = response.json()
    rows = payload.get("results", [])
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("Tavily extract returned no results")
    row = rows[0]
    if not isinstance(row, dict):
        raise RuntimeError("Tavily extract returned an invalid result")
    title = str(row.get("title") or "").strip()
    return title, _tavily_body(title, str(row.get("raw_content") or ""))


def _response_chunks(response: Any):
    iter_content = getattr(response, "iter_content", None)
    if callable(iter_content):
        yield from iter_content(chunk_size=64 * 1024)
        return
    body = bytes(getattr(response, "content", b"") or b"")
    for offset in range(0, len(body), 64 * 1024):
        yield body[offset:offset + 64 * 1024]


def _set_response_read_timeout(response: Any, timeout: float) -> None:
    raw = getattr(response, "raw", None)
    try:
        sock = raw._fp.fp.raw._sock
        sock.settimeout(max(0.05, timeout))
    except (AttributeError, OSError, ValueError):
        pass


def _read_http_body(response: Any, deadline: float) -> tuple[bytes, bool, str]:
    content_type = str(getattr(response, "headers", {}).get("Content-Type", ""))
    is_pdf = "application/pdf" in content_type.casefold()
    body = bytearray()
    truncate_reason = ""
    chunks = iter(_response_chunks(response))
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if is_pdf or body[:5] == b"%PDF-":
                is_pdf = True
                truncate_reason = "time_budget"
                break
            raise TimeoutError("HTTP response exceeded the fetch time budget")
        _set_response_read_timeout(response, remaining)
        try:
            chunk = next(chunks)
        except StopIteration:
            break
        except requests.exceptions.Timeout:
            if is_pdf or body[:5] == b"%PDF-":
                is_pdf = True
                truncate_reason = "time_budget"
                break
            raise
        if not chunk:
            continue
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        if not is_pdf and len(body) < 5:
            prefix_length = min(5 - len(body), len(chunk))
            is_pdf = bytes(body) + chunk[:prefix_length] == b"%PDF-"
        if is_pdf and len(body) + len(chunk) > PDF_DOWNLOAD_LIMIT_BYTES:
            body.extend(chunk[:PDF_DOWNLOAD_LIMIT_BYTES - len(body)])
            truncate_reason = "download_limit"
            break
        body.extend(chunk)
    return bytes(body), is_pdf, truncate_reason


def _first_pdf_line(page_texts: list[str]) -> str:
    if not page_texts:
        return ""
    for line in page_texts[0].splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def _ocr_pdf_pages(image_paths: list[Path], timeout: float) -> list[str]:
    runner = (
        "import json, subprocess, sys\n"
        "from pathlib import Path\n"
        "from ocr_local import ocr_images\n"
        "try:\n"
        "    rows = ocr_images([Path(p) for p in sys.argv[2:]], "
        "timeout_seconds=max(0.05, float(sys.argv[1]) - 0.5))\n"
        "    print(json.dumps([row.get('text', '') for row in rows], ensure_ascii=False))\n"
        "except subprocess.TimeoutExpired:\n"
        "    print(json.dumps({'timed_out': True}))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", runner, str(timeout), *[str(path) for path in image_paths]],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=max(0.05, timeout),
        cwd=str(Path(__file__).resolve().parent),
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip()
        raise RuntimeError(f"local OCR exited with code {completed.returncode}" + (f": {detail[:500]}" if detail else ""))
    rows = json.loads(completed.stdout or "[]")
    if isinstance(rows, dict) and rows.get("timed_out"):
        raise subprocess.TimeoutExpired("local OCR", timeout)
    if not isinstance(rows, list):
        raise RuntimeError("local OCR returned an invalid result")
    return [str(row or "") for row in rows]


def _extract_pdf(data: bytes, deadline: float, download_reason: str = "") -> _FetchedText:
    if fitz is None:
        raise RuntimeError("PyMuPDF is not installed; PDF text extraction is unavailable")
    try:
        document = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise RuntimeError(f"could not open PDF: {str(exc) or exc.__class__.__name__}") from exc

    reasons = [download_reason] if download_reason else []
    try:
        if document.needs_pass:
            raise RuntimeError("encrypted PDF requires a password")
        page_count = document.page_count
        if page_count < 1:
            raise RuntimeError("PDF contains no pages")
        if page_count > PDF_PAGE_LIMIT:
            reasons.append("page_limit")

        page_texts: list[str] = []
        scanned_pages = 0
        page_limit = min(page_count, PDF_PAGE_LIMIT)
        for index in range(page_limit):
            if time.monotonic() >= deadline:
                reasons.append("time_budget")
                break
            page_text = document.load_page(index).get_text("text") or ""
            page_texts.append(page_text)
            valid_chars = sum(char.isprintable() and not char.isspace() for char in page_text)
            if valid_chars < 20:
                scanned_pages += 1
            if time.monotonic() >= deadline:
                reasons.append("time_budget")
                if index + 1 < page_limit:
                    break

        metadata = document.metadata or {}
        title = str(metadata.get("title") or "").strip()
        page_title = _first_pdf_line(page_texts)
        scanned = bool(page_texts) and scanned_pages / len(page_texts) >= 0.7

        if scanned:
            ocr_page_count = min(len(page_texts), PDF_OCR_PAGE_LIMIT)
            if page_count > PDF_OCR_PAGE_LIMIT:
                reasons.append("ocr_page_limit")
            if time.monotonic() >= deadline:
                reasons.append("time_budget")
                text = "\n\n".join(page_texts)
                method = "pdf"
            else:
                image_paths: list[Path] = []
                try:
                    with tempfile.TemporaryDirectory(prefix="fetch-pdf-ocr-") as temp_dir:
                        scale = 150 / 72
                        matrix = fitz.Matrix(scale, scale)
                        for index in range(ocr_page_count):
                            if time.monotonic() >= deadline:
                                reasons.append("time_budget")
                                break
                            pixmap = document.load_page(index).get_pixmap(matrix=matrix, alpha=False)
                            image_path = Path(temp_dir) / f"page-{index + 1:03d}.png"
                            pixmap.save(str(image_path))
                            image_paths.append(image_path)
                        if "time_budget" in reasons:
                            text = "\n\n".join(page_texts)
                            method = "pdf"
                        else:
                            try:
                                ocr_rows = _ocr_pdf_pages(image_paths, deadline - time.monotonic())
                            except subprocess.TimeoutExpired:
                                reasons.append("time_budget")
                                text = "\n\n".join(page_texts)
                                method = "pdf"
                            else:
                                text = "\n\n".join(ocr_rows)
                                method = "pdf-ocr"
                                if not title:
                                    page_title = _first_pdf_line(ocr_rows)
                finally:
                    image_paths.clear()
        else:
            text = "\n\n".join(page_texts)
            method = "pdf"

        if len(text) > PDF_CHAR_LIMIT:
            text = text[:PDF_CHAR_LIMIT]
            reasons.append("char_limit")
        text = _clean_text(text)
        if not title:
            title = page_title
        reason_order = ("download_limit", "time_budget", "page_limit", "ocr_page_limit", "char_limit")
        truncate_reason = next((reason for reason in reason_order if reason in reasons), "")
        result = _FetchedText(
            title=title,
            text=text,
            method=method,
            truncated=bool(reasons),
            truncate_reason=truncate_reason,
        )
        if reasons:
            result.truncate_reasons = list(dict.fromkeys(reasons))
        return result
    finally:
        document.close()


def _http_extract(url: str, timeout: float) -> tuple[str, str] | _FetchedText:
    if url in _HTTP_CACHE:
        cached = _HTTP_CACHE[url]
        if isinstance(cached, str):
            raise RuntimeError(cached)
        return cached

    deadline = time.monotonic() + min(FETCH_BUDGET_SECONDS, max(0.05, timeout))
    try:
        response = requests.get(
            url,
            headers={"User-Agent": CHROME_USER_AGENT},
            timeout=_request_timeout(timeout),
            stream=True,
        )
    except requests.exceptions.RequestException as exc:
        reason = f"HTTP fetch failed: {str(exc) or exc.__class__.__name__}"
        _HTTP_CACHE[url] = reason
        raise RuntimeError(reason) from exc

    try:
        if not 200 <= response.status_code < 300:
            reason = f"HTTP fetch returned HTTP {response.status_code}"
            _HTTP_CACHE[url] = reason
            raise RuntimeError(reason)
        try:
            body, is_pdf, download_reason = _read_http_body(response, deadline)
        except (requests.exceptions.RequestException, TimeoutError) as exc:
            reason = f"HTTP response read failed: {str(exc) or exc.__class__.__name__}"
            _HTTP_CACHE[url] = reason
            raise RuntimeError(reason) from exc
        if is_pdf:
            try:
                outcome = _extract_pdf(body, deadline, download_reason)
            except Exception as exc:
                reason = f"PDF extraction failed: {str(exc) or exc.__class__.__name__}"
                _HTTP_CACHE[url] = reason
                raise RuntimeError(reason) from exc
            _HTTP_CACHE[url] = outcome
            return outcome
        try:
            outcome = _extract_html(body)
        except Exception as exc:
            reason = f"HTTP content extraction failed: {str(exc) or exc.__class__.__name__}"
            _HTTP_CACHE[url] = reason
            raise RuntimeError(reason) from exc
        _HTTP_CACHE[url] = outcome
        return outcome
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()


def _markdown_plain_text(value: str) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", value)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"(?m)^\s*\|[\s|:-]*\|\s*$", "", text)
    return text


def _tavily_body(title: str, raw_text: str) -> str:
    text = str(raw_text or "")
    base_title = re.split(r"\s*(?:[_|｜]|\s+-\s+)\s*", str(title or ""), maxsplit=1)[0].strip()

    def normalized(value: str) -> str:
        return "".join(char.casefold() for char in value if char.isalnum())

    wanted = normalized(base_title)
    if wanted:
        for match in re.finditer(r"(?m)^#{1,6}\s+(.+?)\s*$", text):
            heading = normalized(match.group(1))
            if heading and (wanted.startswith(heading) or heading.startswith(wanted)):
                text = text[match.start():]
                break

    stop = re.search(
        r"(?im)^\s*(?:\[\s*免责声明|#{1,6}\s*(?:热门视频|相关推荐|相关阅读|猜你喜欢|相关攻略))",
        text,
    )
    if stop:
        text = text[:stop.start()]
    plain_text = _markdown_plain_text(text)
    noise_ratio = _anchor_noise_ratio(text, _clean_text(plain_text))
    return _measured_text(plain_text, noise_ratio)


def _browser_extract(url: str, timeout: float) -> tuple[str, str]:
    if not CHROME_PATH.is_file():
        raise RuntimeError(f"Chrome executable not found at {CHROME_PATH}")
    command = [
        str(CHROME_PATH),
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--virtual-time-budget=10000",
        "--dump-dom",
        url,
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Chrome headless timed out after {BROWSER_TIMEOUT_SECONDS}s") from exc
    except OSError as exc:
        raise RuntimeError(f"could not start Chrome: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip()
        if len(detail) > 500:
            detail = detail[:500] + "..."
        raise RuntimeError(
            f"Chrome headless exited with code {completed.returncode}"
            + (f": {detail}" if detail else "")
        )
    if not completed.stdout.strip():
        detail = (completed.stderr or "").strip()
        raise RuntimeError("Chrome headless returned an empty DOM" + (f": {detail[:500]}" if detail else ""))
    return _extract_html(completed.stdout)


class _NotebookFileLock:
    """Cross-process byte-range lock reserved for Notebook state and request slots."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.handle: Any = None

    def acquire(self, timeout: float, poll: float = 0.05) -> bool:
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
                if time.monotonic() - started >= timeout:
                    self.handle.close()
                    self.handle = None
                    return False
                time.sleep(min(poll, max(0.0, timeout - (time.monotonic() - started))))

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


_NOTEBOOK_DISABLED = False


def _notebook_base_url() -> str:
    if _NOTEBOOK_DISABLED:
        return ""
    return os.environ.get("OPEN_NOTEBOOK_URL", "").strip().rstrip("/")


def _notebook_timeout(deadline: float) -> tuple[float, float]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise NotebookError("notebook_timeout", "Notebook time budget exhausted")
    connect_timeout = min(1.0, remaining * 0.25)
    read_timeout = max(0.05, remaining - connect_timeout)
    return connect_timeout, read_timeout


def _notebook_request(method: str, endpoint: str, deadline: float, **kwargs: Any) -> Any:
    base_url = _notebook_base_url()
    if not base_url:
        raise NotebookError("notebook_unavailable", "OPEN_NOTEBOOK_URL is not set")
    try:
        response = getattr(requests, method)(
            f"{base_url}/api/{endpoint.lstrip('/')}",
            timeout=_notebook_timeout(deadline),
            **kwargs,
        )
    except requests.exceptions.ConnectionError as exc:
        raise NotebookError("notebook_unavailable", str(exc) or "connection failed") from exc
    except requests.exceptions.Timeout as exc:
        raise NotebookError("notebook_failed", str(exc) or "request timed out") from exc
    except requests.exceptions.RequestException as exc:
        raise NotebookError("notebook_failed", str(exc) or exc.__class__.__name__) from exc
    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code >= 500:
        raise NotebookError("notebook_failed", f"HTTP {status_code}")
    if not 200 <= status_code < 300:
        raise NotebookError("notebook_failed", f"HTTP {status_code}")
    return response


def _notebook_state_unlocked() -> dict[str, Any]:
    try:
        data = json.loads(NOTEBOOK_STATE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"notebook_id": "", "pending_sources": []}
    except (OSError, json.JSONDecodeError) as exc:
        raise NotebookError("notebook_failed", f"could not read Notebook state: {exc}") from exc
    if not isinstance(data, dict):
        raise NotebookError("notebook_failed", "Notebook state must be a JSON object")
    pending = data.get("pending_sources", [])
    if not isinstance(pending, list):
        pending = []
    normalized = []
    for item in pending:
        if isinstance(item, str) and item:
            normalized.append({"id": item, "pid": 0})
        elif isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]:
            try:
                pid = int(item.get("pid", 0))
            except (TypeError, ValueError):
                pid = 0
            normalized.append({"id": item["id"], "pid": pid})
    return {"notebook_id": str(data.get("notebook_id") or ""), "pending_sources": normalized}


def _write_notebook_state_unlocked(data: dict[str, Any]) -> None:
    NOTEBOOK_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=NOTEBOOK_STATE_PATH.parent,
            prefix=".notebook-state-", suffix=".tmp", delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(data, handle, ensure_ascii=False, indent=2)
        temp_path.replace(NOTEBOOK_STATE_PATH)
    except OSError as exc:
        raise NotebookError("notebook_failed", f"could not write Notebook state: {exc}") from exc
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _with_notebook_state_lock(deadline: float) -> _NotebookFileLock:
    lock = _NotebookFileLock(NOTEBOOK_STATE_LOCK_PATH)
    try:
        acquired = lock.acquire(max(0.0, deadline - time.monotonic()))
    except OSError as exc:
        raise NotebookError("notebook_failed", f"Notebook state lock failed: {exc}") from exc
    if not acquired:
        raise NotebookError("notebook_timeout", "Notebook state lock timed out")
    return lock


def _notebook_slot_paths() -> list[Path]:
    return [Path(f"{NOTEBOOK_REQUEST_LOCK_STEM}.{index}.lock") for index in range(NOTEBOOK_MAX_CONCURRENT)]


def _acquire_notebook_slot(deadline: float) -> _NotebookFileLock:
    paths = _notebook_slot_paths()
    while time.monotonic() < deadline:
        for path in paths:
            lock = _NotebookFileLock(path)
            try:
                if lock.acquire(0):
                    return lock
            except OSError as exc:
                raise NotebookError("notebook_failed", f"Notebook request lock failed: {exc}") from exc
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.05, remaining))
    raise NotebookError("notebook_timeout", "Notebook request slot wait timed out")


def _notebook_source_delete(source_id: str, deadline: float) -> bool:
    try:
        base_url = _notebook_base_url()
        if not base_url:
            return False
        response = requests.delete(
            f"{base_url}/api/sources/{source_id}",
            timeout=_notebook_timeout(deadline),
        )
        status_code = int(getattr(response, "status_code", 0) or 0)
        return 200 <= status_code < 300 or status_code == 404
    except (requests.exceptions.RequestException, NotebookError, OSError):
        return False


def _notebook_remove_pending(source_id: str, deadline: float) -> None:
    try:
        lock = _with_notebook_state_lock(deadline)
    except NotebookError:
        return
    try:
        state = _notebook_state_unlocked()
        state["pending_sources"] = [
            item for item in state["pending_sources"] if item["id"] != source_id
        ]
        _write_notebook_state_unlocked(state)
    except NotebookError:
        pass
    finally:
        lock.release()


def _notebook_process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (ProcessLookupError, OSError):
        return False


def _notebook_cleanup_residuals(deadline: float) -> None:
    try:
        lock = _with_notebook_state_lock(deadline)
    except NotebookError:
        return
    try:
        state = _notebook_state_unlocked()
        with _ACTIVE_NOTEBOOK_SOURCES_LOCK:
            active_sources = set(_ACTIVE_NOTEBOOK_SOURCES)
        stale = [
            item for item in state["pending_sources"]
            if item["id"] not in active_sources and (
                item["pid"] == os.getpid() or not _notebook_process_is_running(item["pid"])
            )
        ]
        for item in stale:
            if time.monotonic() >= deadline:
                break
            if _notebook_source_delete(item["id"], deadline):
                state["pending_sources"] = [
                    candidate for candidate in state["pending_sources"] if candidate["id"] != item["id"]
                ]
                _write_notebook_state_unlocked(state)
    except NotebookError:
        pass
    finally:
        lock.release()


def _notebook_id(deadline: float) -> str:
    lock = _with_notebook_state_lock(deadline)
    try:
        state = _notebook_state_unlocked()
        if state["notebook_id"]:
            return state["notebook_id"]
        response = _notebook_request(
            "post", "notebooks", deadline,
            json={"name": "fetch_smart", "description": "Dedicated notebook for clean webpage text extraction."},
        )
        try:
            notebook_id = str(response.json().get("id") or "").strip()
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise NotebookError("notebook_failed", "notebook creation returned invalid JSON") from exc
        if not notebook_id:
            raise NotebookError("notebook_failed", "notebook creation returned no id")
        state["notebook_id"] = notebook_id
        _write_notebook_state_unlocked(state)
        return notebook_id
    finally:
        lock.release()


def _notebook_reset_id(deadline: float) -> None:
    lock = _with_notebook_state_lock(deadline)
    try:
        state = _notebook_state_unlocked()
        state["notebook_id"] = ""
        _write_notebook_state_unlocked(state)
    finally:
        lock.release()


def _notebook_track_source(source_id: str, deadline: float) -> None:
    lock = _with_notebook_state_lock(deadline)
    try:
        state = _notebook_state_unlocked()
        if not any(item["id"] == source_id for item in state["pending_sources"]):
            state["pending_sources"].append({"id": source_id, "pid": os.getpid()})
            _write_notebook_state_unlocked(state)
    finally:
        lock.release()


def _notebook_create_source(notebook_id: str, url: str, deadline: float) -> Any:
    return _notebook_request(
        "post", "sources", deadline,
        files={
            "type": (None, "link"),
            "notebook_id": (None, notebook_id),
            "url": (None, url),
            "embed": (None, "false"),
            "async_processing": (None, "true"),
        },
    )


def _notebook_missing_notebook(exc: NotebookError) -> bool:
    detail = str(exc)
    return "HTTP 400" in detail or "HTTP 404" in detail


def _notebook_extract(url: str, total_deadline: float) -> tuple[str, str]:
    started = time.monotonic()
    available = total_deadline - started
    if available < NOTEBOOK_MIN_REMAINING_SECONDS:
        raise NotebookError("notebook_skipped_budget", "less than five seconds remain")
    stage_deadline = min(total_deadline, started + NOTEBOOK_BUDGET_SECONDS)
    cleanup_deadline = stage_deadline
    operation_deadline = max(started, stage_deadline - NOTEBOOK_DELETE_RESERVE_SECONDS)
    slot = _acquire_notebook_slot(operation_deadline)
    source_id = ""
    title = ""
    text = ""
    try:
        _notebook_cleanup_residuals(operation_deadline)
        notebook_id = _notebook_id(operation_deadline)
        try:
            response = _notebook_create_source(notebook_id, url, operation_deadline)
        except NotebookError as exc:
            if not _notebook_missing_notebook(exc):
                raise
            _notebook_reset_id(operation_deadline)
            notebook_id = _notebook_id(operation_deadline)
            response = _notebook_create_source(notebook_id, url, operation_deadline)
        try:
            source_id = str(response.json().get("id") or "").strip()
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise NotebookError("notebook_failed", "source creation returned invalid JSON") from exc
        if not source_id:
            raise NotebookError("notebook_failed", "source creation returned no id")
        with _ACTIVE_NOTEBOOK_SOURCES_LOCK:
            _ACTIVE_NOTEBOOK_SOURCES.add(source_id)
        _notebook_track_source(source_id, operation_deadline)

        intervals = (0.5, 1.0, 2.0)
        interval_index = 0
        while time.monotonic() < operation_deadline:
            status_response = _notebook_request(
                "get", f"sources/{source_id}/status", operation_deadline,
            )
            try:
                status = str(status_response.json().get("status") or "").strip().lower()
            except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise NotebookError("notebook_failed", "source status returned invalid JSON") from exc
            if status == "failed":
                raise NotebookError("notebook_failed", "source processing failed")
            if status == "completed":
                source_response = _notebook_request(
                    "get", f"sources/{source_id}", operation_deadline,
                )
                try:
                    source = source_response.json()
                except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise NotebookError("notebook_failed", "source returned invalid JSON") from exc
                if not isinstance(source, dict):
                    raise NotebookError("notebook_failed", "source response must be an object")
                title = str(source.get("title") or "").strip()
                text = _clean_text(str(source.get("full_text") or ""))
                if not text:
                    raise NotebookError("notebook_empty", "completed source has no full_text")
                if any(marker in text for marker in LOGIN_WALL_MARKERS):
                    raise NotebookError("notebook_login_wall", "source text contains a login wall")
                return title, text
            if status != "running":
                raise NotebookError("notebook_failed", f"unexpected source status {status!r}")
            delay = intervals[min(interval_index, len(intervals) - 1)]
            interval_index += 1
            remaining = operation_deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(delay, remaining))
        raise NotebookError("notebook_timeout", "source did not complete within the Notebook budget")
    finally:
        try:
            if source_id:
                try:
                    if _notebook_source_delete(source_id, cleanup_deadline):
                        _notebook_remove_pending(source_id, cleanup_deadline)
                except Exception:
                    pass
                finally:
                    with _ACTIVE_NOTEBOOK_SOURCES_LOCK:
                        _ACTIVE_NOTEBOOK_SOURCES.discard(source_id)
        finally:
            slot.release()


class _OpenCLILock:
    """Cross-process byte-range lock for the shared OpenCLI browser session."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.handle: Any = None

    def acquire(self, timeout: float, poll: float = 0.05) -> bool:
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
                if time.monotonic() - started >= timeout:
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


def _opencli_command() -> list[str]:
    if os.name == "nt" and OPENCLI_NODE.is_file() and OPENCLI_ENTRY.is_file():
        return [str(OPENCLI_NODE), str(OPENCLI_ENTRY)]
    if not OPENCLI_PATH.is_file():
        raise RuntimeError(f"opencli executable not found at {OPENCLI_PATH}")
    return [str(OPENCLI_PATH)]


def _opencli_timeout() -> float:
    value = os.environ.get("OPENCLI_TIMEOUT", "").strip()
    if not value:
        return OPENCLI_TIMEOUT_SECONDS
    try:
        timeout = float(value)
    except ValueError as exc:
        raise RuntimeError("OPENCLI_TIMEOUT must be a positive number") from exc
    if timeout <= 0:
        raise RuntimeError("OPENCLI_TIMEOUT must be a positive number")
    return timeout


def _run_opencli(args: list[str], timeout: float, stage: str) -> str:
    try:
        completed = subprocess.run(
            _opencli_command() + args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(0.1, timeout),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"opencli {stage} timed out") from exc
    except OSError as exc:
        raise RuntimeError(f"opencli {stage} could not start: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        if len(detail) > 500:
            detail = detail[:500] + "..."
        raise RuntimeError(
            f"opencli {stage} failed: {detail or f'exit code {completed.returncode}'}"
        )
    return completed.stdout or ""


def _parse_opencli_text(output: str) -> tuple[str, str]:
    text = (output or "").strip()
    if not text:
        return "", ""
    try:
        value: Any = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return "", text
    title = ""
    for _ in range(4):
        if isinstance(value, str):
            return title, value.strip()
        if isinstance(value, dict):
            title = title or str(value.get("title") or "").strip()
            nested = next(
                (value[key] for key in ("text", "result", "output", "data", "value") if key in value),
                None,
            )
            if nested is None:
                return title, text
            value = nested
            continue
        return title, text
    return title, text


def _opencli_extract_unlocked(url: str, timeout: float) -> tuple[str, str]:
    scale = timeout / OPENCLI_TIMEOUT_SECONDS
    open_timeout = 25.0 * scale
    sleep_seconds = 5.0 * scale
    eval_timeout = 30.0 * scale
    deadline = time.monotonic() + timeout
    _run_opencli(
        ["browser", "main", "open", url],
        min(open_timeout, deadline - time.monotonic()),
        "open",
    )
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeError("opencli sleep timed out")
    time.sleep(min(sleep_seconds, remaining))
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeError("opencli eval timed out")
    output = _run_opencli(
        ["browser", "main", "eval", "(() => document.body.innerText)()"],
        min(eval_timeout, remaining),
        "eval",
    )
    title, text = _parse_opencli_text(output)
    if not text:
        raise RuntimeError("opencli eval returned empty page text")
    return title, text


OPENCLI_SCROLL_JS = """(() => {
  const pick = () => {
    let best = null, bestDiff = 0;
    for (const el of document.querySelectorAll('div,main,section,article')) {
      const diff = el.scrollHeight - el.clientHeight;
      if (diff > bestDiff && diff > 400) { bestDiff = diff; best = el; }
    }
    const docDiff = document.documentElement.scrollHeight - window.innerHeight;
    if (!best || docDiff >= bestDiff) return document.scrollingElement || document.documentElement;
    return best;
  };
  const s = pick();
  const before = s.scrollTop;
  s.scrollTop = before + Math.round(window.innerHeight * 0.9);
  return JSON.stringify({before: before, top: s.scrollTop, chars: document.body.innerText.length, height: document.documentElement.scrollHeight});
})()"""

OPENCLI_MORE_JS = """(() => {
  const words = ['加载更多','查看更多','展开更多','显示更多','展开阅读全文','Load more','Show more'];
  let clicked = 0;
  for (const el of document.querySelectorAll('button,a,div[role=button]')) {
    const label = (el.innerText || '').trim();
    if (label && label.length <= 10 && words.includes(label)) {
      try { el.click(); clicked += 1; } catch (e) {}
      if (clicked >= 2) break;
    }
  }
  return String(clicked);
})()"""


def _scroll_screens(value: int | None = None) -> int:
    """Resolve scroll screens: explicit value wins, then FETCH_SCROLL env, else 0."""
    if value is None:
        raw = (os.environ.get("FETCH_SCROLL") or os.environ.get("FETCH_SMART_SCROLL") or "").strip()
        try:
            value = int(raw) if raw else 0
        except ValueError:
            value = 0
    return max(0, min(10, int(value)))


def _parse_scroll_state(output: str) -> dict[str, int]:
    for line in reversed([item.strip() for item in output.splitlines() if item.strip()]):
        if not line.startswith("{"):
            continue
        try:
            data = __import__("json").loads(line)
        except ValueError:
            continue
        return {
            "before": int(data.get("before", 0)),
            "top": int(data.get("top", 0)),
            "chars": int(data.get("chars", 0)),
        }
    return {"before": 0, "top": 0, "chars": 0}


def _opencli_extract_scrolled(
    url: str, timeout: float, screens: int
) -> tuple[str, str, dict[str, Any]]:
    """Open a page in the logged-in browser, scroll it a few screens, then read the text."""
    scale = timeout / OPENCLI_TIMEOUT_SECONDS
    open_timeout = 25.0 * scale
    sleep_seconds = 5.0 * scale
    eval_timeout = 30.0 * scale
    deadline = time.monotonic() + timeout
    _run_opencli(
        ["browser", "main", "open", url],
        min(open_timeout, deadline - time.monotonic()),
        "open",
    )
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeError("opencli sleep timed out")
    time.sleep(min(sleep_seconds, remaining))

    used = 0
    stopped = "limit"
    stagnant = 0
    chars_before = 0
    previous = {"before": 0, "top": 0, "chars": 0}
    for index in range(screens):
        remaining = deadline - time.monotonic()
        if remaining < 4.0:
            stopped = "time_budget"
            break
        try:
            _run_opencli(
                ["browser", "main", "eval", OPENCLI_MORE_JS],
                min(eval_timeout, remaining),
                "eval",
            )
        except Exception:
            pass
        remaining = deadline - time.monotonic()
        if remaining < 3.0:
            stopped = "time_budget"
            break
        try:
            output = _run_opencli(
                ["browser", "main", "eval", OPENCLI_SCROLL_JS],
                min(eval_timeout, remaining),
                "eval",
            )
        except Exception:
            stopped = "scroll_failed"
            break
        state = _parse_scroll_state(output)
        used += 1
        if index == 0:
            chars_before = state["chars"]
        moved = state["top"] != previous["top"] or state["chars"] != previous["chars"]
        stagnant = 0 if moved else stagnant + 1
        previous = state
        if stagnant >= 2:
            stopped = "no_more_content"
            break
        pause = min(1.5 * scale, max(0.0, deadline - time.monotonic()))
        if pause:
            time.sleep(pause)

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("opencli eval timed out")
    output = _run_opencli(
        ["browser", "main", "eval", "(() => document.body.innerText)()"],
        min(eval_timeout, remaining),
        "eval",
    )
    title, text = _parse_opencli_text(output)
    if not text:
        raise RuntimeError("opencli eval returned empty page text")
    meta = {
        "screens": used,
        "stopped": stopped,
        "chars_before": chars_before,
        "chars_after": len(text),
    }
    return title, text, meta


def _logged_in_extract(
    url: str, timeout: float | None, screens: int
) -> tuple[str, str, dict[str, Any]]:
    if screens <= 0:
        title, text = _opencli_extract(url, timeout=timeout)
        return title, text, {}
    timeout = min(_opencli_timeout(), timeout) if timeout is not None else _opencli_timeout()
    if timeout <= 0:
        raise TimeoutError(f"fetch timed out after {FETCH_BUDGET_SECONDS:g}s")
    lock_path = os.environ.get("OPENCLI_LOCK_PATH", "").strip()
    lock = _OpenCLILock(lock_path) if lock_path else None
    if lock is not None:
        try:
            acquired = lock.acquire(min(OPENCLI_LOCK_TIMEOUT_SECONDS, timeout))
        except OSError as exc:
            raise RuntimeError(f"opencli lock failed: {exc}") from exc
        if not acquired:
            raise RuntimeError("opencli_lock_timeout")
    try:
        return _opencli_extract_scrolled(url, timeout, screens)
    finally:
        if lock is not None:
            lock.release()


def _opencli_extract(url: str, timeout: float | None = None) -> tuple[str, str]:
    timeout = min(_opencli_timeout(), timeout) if timeout is not None else _opencli_timeout()
    if timeout <= 0:
        raise TimeoutError(f"fetch timed out after {FETCH_BUDGET_SECONDS:g}s")
    deadline = time.monotonic() + timeout
    lock_path = os.environ.get("OPENCLI_LOCK_PATH", "").strip()
    lock = _OpenCLILock(lock_path) if lock_path else None
    if lock is not None:
        try:
            acquired = lock.acquire(min(OPENCLI_LOCK_TIMEOUT_SECONDS, timeout))
        except OSError as exc:
            raise RuntimeError(f"opencli lock failed: {exc}") from exc
        if not acquired:
            raise RuntimeError("opencli_lock_timeout")
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"fetch timed out after {FETCH_BUDGET_SECONDS:g}s")
        return _opencli_extract_unlocked(url, remaining)
    finally:
        if lock is not None:
            lock.release()


def _opencli_result(url: str, title: str, text: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    result = _result(url, "opencli", title, text)
    result["method"] = "opencli"
    if meta:
        result["scroll"] = meta
    return result


def _logged_in_failure_message(url: str, error: Exception) -> str:
    try:
        import research

        checks = research.login_session_checks()
        check_text = "; ".join(
            f"{check['name']}={check['status']} ({check['detail']})" for check in checks
        )
    except Exception as diagnostic_error:
        check_text = f"检查失败 ({diagnostic_error})"
    safe_url = url.replace('"', "%22")
    fallback = f'python fetch_smart.py "{safe_url}" --route auto --json'
    return (
        f"opencli failed: {str(error) or error.__class__.__name__}; "
        f"当前检查结果: {check_text}。需要用户打开 Chrome 并确认已登录目标站点；"
        f"改用普通抓取: {fallback}"
    )


def _subtitle_text(path: Path) -> str:
    try:
        content = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""
    lines = []
    seen = set()
    for line in content.splitlines():
        line = line.strip()
        if (
            not line
            or line.upper() == "WEBVTT"
            or line.startswith(("NOTE", "Kind:", "Language:"))
            or re.fullmatch(r"\d+", line)
            or "-->" in line
        ):
            continue
        line = re.sub(r"<[^>]+>", "", line)
        line = re.sub(r"\{\\[^}]+\}", "", line)
        line = _clean_text(line)
        if line and line not in seen:
            lines.append(line)
            seen.add(line)
    return "\n".join(lines)


def _remaining(deadline: float, cap: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(f"fetch timed out after {FETCH_BUDGET_SECONDS:g}s")
    return min(cap, remaining)


def _meta_unavailable(url: str, method: str, title: str = "") -> dict[str, Any]:
    return {
        "url": url,
        "method": f"{method}:failed",
        "chars": 0,
        "title": str(title or "").strip(),
        "text": "",
        "quality": 0.0,
        "noise_ratio": 0.0,
        "quality_reason": "meta_unavailable",
        "login_wall": False,
        "low_quality": True,
    }


def _video_meta_extract(url: str, platform: str) -> dict[str, Any]:
    method = f"{platform}-meta"
    if not YT_DLP_PATH.is_file():
        return _meta_unavailable(url, method)

    deadline = time.monotonic() + FETCH_BUDGET_SECONDS
    description = ""
    title = ""
    uploader = ""
    upload_date = ""
    print_template = (
        "%(title)s\n%(uploader)s\n%(upload_date)s\n%(description)s"
        if platform == "youtube"
        else "%(description)s"
    )
    command = [
        str(YT_DLP_PATH),
        "--proxy",
        YT_DLP_PROXY,
        "--skip-download",
        "--print",
        print_template,
        url,
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_remaining(deadline, 24.0),
            check=False,
        )
        if completed.returncode == 0:
            if platform == "youtube":
                output = (completed.stdout or "").replace("\r\n", "\n").replace("\r", "\n")
                fields = output.split("\n", 3)
                fields += [""] * (4 - len(fields))
                title, uploader, upload_date, description = fields[:4]
                title, uploader, upload_date = [
                    "" if value.strip() in {"", "NA", "None"} else value.strip()
                    for value in (title, uploader, upload_date)
                ]
            else:
                description = completed.stdout
            description = str(description or "").strip()
            if description in {"NA", "None"}:
                description = ""
    except Exception:
        pass

    captions = ""
    temp_root = Path(tempfile.gettempdir())
    temp_prefix = f"fetch_smart_video_{uuid.uuid4().hex}_"
    output_template = str(temp_root / f"{temp_prefix}%(id)s.%(ext)s")
    try:
        subtitle_command = [
            str(YT_DLP_PATH),
            "--proxy",
            YT_DLP_PROXY,
            "--write-auto-sub",
            "--sub-lang",
            "zh-Hans,zh,en",
            "--skip-download",
            "--sub-format",
            "vtt",
            "-o",
            output_template,
            url,
        ]
        try:
            completed = subprocess.run(
                subtitle_command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_remaining(deadline, 24.0),
                check=False,
            )
            if completed.returncode == 0:
                caption_files = sorted(temp_root.glob(f"{temp_prefix}*.vtt"))
                captions = _clean_text("\n".join(_subtitle_text(path) for path in caption_files))
        except Exception:
            pass
    finally:
        for path in temp_root.glob(f"{temp_prefix}*"):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    if platform == "youtube":
        parts = [title]
        if uploader:
            parts.append(f"Uploader: {uploader}")
        if upload_date:
            parts.append(f"Date: {upload_date}")
        if description:
            parts.append(f"Description:\n{description}")
        if captions:
            parts.append(f"Subtitles:\n{captions}")
    else:
        parts = [description, captions]
    body = "\n\n".join(part for part in parts if part)[:6000]
    if not (description or captions):
        return _meta_unavailable(url, method, title)
    result = _result(url, method, title, body)
    if platform == "youtube":
        result["method"] = method
    return result


def _bilibili_extract(url: str) -> dict[str, Any]:
    return _video_meta_extract(url, "bilibili")


def _youtube_extract(url: str) -> dict[str, Any]:
    return _video_meta_extract(url, "youtube")


def _notebook_fallback(url: str, min_chars: int, reasons: list[str]) -> dict[str, Any]:
    """Run the regular chain when the Notebook route cannot deliver a body."""
    global _NOTEBOOK_DISABLED
    _NOTEBOOK_DISABLED = True
    try:
        result = fetch_smart(url, min_chars=min_chars, route="auto")
    finally:
        _NOTEBOOK_DISABLED = False
    combined = list(reasons) + list(result.get("reasons") or [])
    if combined:
        result["reasons"] = combined
    return result


def fetch_smart(url: str, min_chars: int = 800, route: str = "auto", scroll: int | None = None) -> dict[str, Any]:
    """Fetch the best available article text within a bounded time budget."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url must be a non-empty string")
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ValueError("url must be an absolute http or https URL")
    if min_chars < 0:
        raise ValueError("min_chars must be zero or greater")
    if route not in {"auto", "logged-in", "notebook"}:
        raise ValueError("route must be auto, logged-in, or notebook")

    clean_url = url.strip()
    screens = _scroll_screens(scroll)
    if screens > 0 and route == "notebook":
        # Notebook 抽取不滚动；要滚动就只能走已登录浏览器。
        route = "logged-in"
    hostname = (parts.hostname or "").lower()
    if route == "logged-in":
        _load_local_env()
        deadline = time.monotonic() + FETCH_BUDGET_SECONDS
        try:
            title, text, meta = _logged_in_extract(
                clean_url, _remaining(deadline, FETCH_BUDGET_SECONDS), screens
            )
        except Exception as exc:
            raise RuntimeError(_logged_in_failure_message(clean_url, exc)) from exc
        return _opencli_result(clean_url, title, text, meta)
    if route == "notebook":
        _load_local_env()
        deadline = time.monotonic() + FETCH_BUDGET_SECONDS
        try:
            title, text = _notebook_extract(clean_url, deadline)
        except NotebookError as exc:
            if exc.reason == "notebook_login_wall":
                try:
                    title, text, meta = _logged_in_extract(
                        clean_url, _remaining(deadline, OPENCLI_TIMEOUT_SECONDS), screens
                    )
                    result = _opencli_result(clean_url, title, text, meta)
                    result["reasons"] = [exc.reason]
                    return result
                except Exception as opencli_error:
                    detail = str(opencli_error) or opencli_error.__class__.__name__
                    raise FetchError(clean_url, [exc.reason, f"opencli: {detail}"]) from opencli_error
            return _notebook_fallback(clean_url, min_chars, [exc.reason])
        except Exception as exc:
            return _notebook_fallback(clean_url, min_chars, [f"notebook_failed: {exc.__class__.__name__}"])
        nb_result = _result(clean_url, "notebook", title, text)
        try:
            plain_extracted = _http_extract(clean_url, _remaining(deadline, HTTP_TIMEOUT_SECONDS))
            plain = _extracted_result(clean_url, "http", plain_extracted)
            plain_rank = (not plain["low_quality"], plain["chars"] * plain["quality"], plain["quality"])
            nb_rank = (not nb_result["low_quality"], nb_result["chars"] * nb_result["quality"], nb_result["quality"])
            if plain["text"] and plain_rank > nb_rank:
                plain["reasons"] = ["notebook_body_smaller"]
                return plain
        except Exception:
            pass
        return nb_result

    if (
        (hostname == "youtube.com" or hostname.endswith(".youtube.com"))
        and parts.path.rstrip("/").lower() == "/watch"
    ) or (hostname == "youtu.be" and bool(parts.path.strip("/"))):
        return _youtube_extract(clean_url)
    if (
        (hostname == "bilibili.com" or hostname.endswith(".bilibili.com"))
        and re.fullmatch(r"/video/BV[0-9A-Za-z]+/?", parts.path, flags=re.IGNORECASE)
    ):
        return _bilibili_extract(clean_url)

    _load_local_env()
    reasons = []
    best: dict[str, Any] | None = None
    usable_body_found = False
    low_quality_candidate_seen = False
    login_wall_detected = False
    notebook_login_wall_detected = False
    deadline = time.monotonic() + FETCH_BUDGET_SECONDS
    methods = (
        ("http", _http_extract),
        ("tavily", _tavily_extract),
        ("browser", _browser_extract),
    )
    for method, extractor in methods:
        try:
            timeout = _remaining(deadline, BROWSER_TIMEOUT_SECONDS if method == "browser" else HTTP_TIMEOUT_SECONDS)
            extracted = extractor(clean_url, timeout)
            result = _extracted_result(clean_url, method, extracted)
            if result.get("truncate_reason") == "time_budget":
                if reasons:
                    result["reasons"] = reasons
                return result
            low_quality_candidate_seen = (
                low_quality_candidate_seen or result["low_quality"] or result["quality"] < 0.5
            )
            login_wall_detected = login_wall_detected or result["login_wall"]
            if result["text"] and result["chars"] >= min_chars:
                usable_body_found = True
            rank = (not result["low_quality"], result["chars"] * result["quality"], result["quality"])
            best_rank = (
                (not best["low_quality"], best["chars"] * best["quality"], best["quality"])
                if best is not None else None
            )
            if result["text"] and (best_rank is None or rank > best_rank):
                best = result
            if (
                result["chars"] >= min_chars and result["quality"] >= 0.5
                and not result["low_quality"] and not result["login_wall"]
            ):
                return result
            reasons.append(
                f"{method}: {result['chars']} chars at quality {result['quality']:.3f} "
                f"(minimum {min_chars} chars and quality 0.500; {result['quality_reason']})"
            )
        except Exception as exc:
            reasons.append(f"{method}: {str(exc) or exc.__class__.__name__}")
            if time.monotonic() >= deadline:
                break
    if (
        _notebook_base_url()
        and not login_wall_detected
        and (not usable_body_found or low_quality_candidate_seen)
        and deadline - time.monotonic() >= NOTEBOOK_MIN_REMAINING_SECONDS
    ):
        try:
            title, text = _notebook_extract(clean_url, deadline)
            result = _result(clean_url, "notebook", title, text)
            rank = (not result["low_quality"], result["chars"] * result["quality"], result["quality"])
            best_rank = (
                (not best["low_quality"], best["chars"] * best["quality"], best["quality"])
                if best is not None else None
            )
            if result["text"] and (best_rank is None or rank > best_rank):
                if reasons:
                    result["reasons"] = reasons
                return result
            if reasons and best is not None:
                best["reasons"] = reasons
        except NotebookError as exc:
            reasons.append(exc.reason)
            notebook_login_wall_detected = exc.reason == "notebook_login_wall"
        except Exception:
            reasons.append("notebook_failed")
    if not usable_body_found or login_wall_detected or notebook_login_wall_detected:
        try:
            title, text, meta = _logged_in_extract(
                clean_url, _remaining(deadline, OPENCLI_TIMEOUT_SECONDS), screens
            )
            result = _opencli_result(clean_url, title, text, meta)
            if reasons:
                result["reasons"] = reasons
            return result
        except Exception as exc:
            reasons.append(f"opencli: {str(exc) or exc.__class__.__name__}")
    if best is not None:
        if reasons:
            best["reasons"] = reasons
        return best
    raise FetchError(clean_url, reasons)


def _configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except (OSError, ValueError):
                pass


def main(argv: list[str] | None = None) -> int:
    _configure_console()
    parser = argparse.ArgumentParser(description="Fetch readable page text with smart fallbacks.")
    parser.add_argument("url")
    parser.add_argument("--min-chars", type=int, default=800)
    parser.add_argument("--route", choices=("auto", "logged-in", "notebook"), default="auto")
    parser.add_argument("--scroll", type=int, default=None, metavar="N",
                        help="scroll N screens before reading (logged-in route; default from FETCH_SCROLL)")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--out", metavar="FILE")
    args = parser.parse_args(argv)
    try:
        payload = fetch_smart(args.url, min_chars=args.min_chars, route=args.route, scroll=args.scroll)
    except Exception as exc:
        print(f"fetch_smart: {exc}", file=sys.stderr)
        return 3

    if args.out:
        try:
            Path(args.out).write_text(payload["text"], encoding="utf-8")
        except OSError as exc:
            print(f"fetch_smart: could not write {args.out}: {exc}", file=sys.stderr)
            return 3
    if args.as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(
            f"method={payload['method']} chars={payload['chars']} "
            f"title={payload['title']}"
        )
        print(payload["text"][:800])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
