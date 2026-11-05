"""Collect search results from logged-in browser sessions through OpenCLI."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin


try:
    from . import config
except ImportError:
    import config

OPENCLI_PATH = config.resolve_program(
    "OPENCLI_PATH", "opencli",
    candidates=(str(Path.home() / "AppData" / "Local" / "hermes" / "node" / "opencli"),),
) or Path(os.path.abspath("/__missing__/not-installed"))
SCRIPT_DIR = Path(__file__).resolve().parent
OPENCLI_NODE = OPENCLI_PATH.parent / "node.exe"
OPENCLI_ENTRY = OPENCLI_PATH.parent / "node_modules/@jackwener/opencli/dist/src/main.js"
PLATFORMS = ("xhs", "zhihu", "reddit")
PLATFORM_LABELS = {"xhs": "小红书", "zhihu": "知乎", "reddit": "Reddit"}
TOTAL_TIMEOUT = 90.0
PLATFORM_TIMEOUT = 30.0
PAGE_WAIT = 5.5


class OpenCLIError(RuntimeError):
    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage


def _opencli_command() -> list[str]:
    if os.name == "nt" and OPENCLI_NODE.is_file() and OPENCLI_ENTRY.is_file():
        return [str(OPENCLI_NODE), str(OPENCLI_ENTRY)]
    return [str(OPENCLI_PATH)]


def _script_for(platform: str) -> str:
    if platform == "xhs":
        return """JSON.stringify(Array.from(document.querySelectorAll('.note-item')).map((node) => {
  const link = node.querySelector('a[href*="xsec_token"]') || node.querySelector('a[href*="/explore/"]');
  if (!link) return null;
  const lines = (node.innerText || '').split(/\\n/).map((part) => part.trim()).filter(Boolean);
  return {href: link.getAttribute('href'), title: lines[0] || link.innerText.trim(), author: lines[1] || null,
    published: lines[2] || null, likes: lines[3] || null};
}).filter(Boolean))"""
    if platform == "zhihu":
        return """JSON.stringify(Array.from(document.querySelectorAll('.SearchResult-Card')).map((node) => {
  const link = node.querySelector('.ContentItem-title a');
  if (!link) return null;
  return {href: link.getAttribute('href'), title: (link.innerText || link.textContent || '').trim()};
}).filter(Boolean))"""
    return """JSON.stringify(Array.from(document.querySelectorAll('a[href*="/r/"][href*="/comments/"]')).map((link) => {
  return {href: link.getAttribute('href'), title: (link.innerText || link.textContent || '').trim()};
}))"""


def _search_url(platform: str, query: str) -> str:
    encoded = quote(query, safe="")
    if platform == "xhs":
        return f"https://www.xiaohongshu.com/search_result?keyword={encoded}"
    if platform == "zhihu":
        return f"https://www.zhihu.com/search?type=content&q={encoded}"
    return f"https://www.reddit.com/search/?q={encoded}"


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
        label = "open failed" if stage == "open" else "eval no output"
        raise OpenCLIError(stage, f"{label}: timed out") from exc
    except OSError as exc:
        label = "open failed" if stage == "open" else "eval no output"
        raise OpenCLIError(stage, f"{label}: {exc}") from exc
    output = completed.stdout or ""
    if completed.returncode != 0:
        detail = (completed.stderr or output).strip()
        label = "open failed" if stage == "open" else "eval failed"
        raise OpenCLIError(stage, f"{label}: {detail[:300] or f'exit code {completed.returncode}'}")
    if stage == "eval" and not output.strip():
        raise OpenCLIError(stage, "eval no output")
    return output.strip()


def _parse_eval_output(output: str) -> list[dict[str, Any]]:
    try:
        payload: Any = json.loads(output)
        if isinstance(payload, str):
            payload = json.loads(payload)
        if isinstance(payload, dict):
            for key in ("result", "output", "data", "value"):
                nested = payload.get(key)
                if isinstance(nested, str):
                    payload = json.loads(nested)
                    break
                if isinstance(nested, list):
                    payload = nested
                    break
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("invalid JSON returned by eval") from exc
    if not isinstance(payload, list):
        raise ValueError("eval JSON was not an array")
    return [row for row in payload if isinstance(row, dict)]


def _parse_rows(platform: str, rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    items = []
    seen: set[str] = set()
    for row in rows:
        href = str(row.get("href") or "").strip()
        title = " ".join(str(row.get("title") or "").split())
        if not href or not title:
            continue
        base = "https://www.xiaohongshu.com" if platform == "xhs" else (
            "https://www.zhihu.com" if platform == "zhihu" else "https://www.reddit.com"
        )
        url = urljoin(base, href)
        if platform == "reddit":
            if url in seen:
                continue
            seen.add(url)
        metrics: dict[str, Any] = {}
        if platform == "xhs" and row.get("likes"):
            likes = str(row["likes"]).strip()
            for prefix in ("点赞", "赞"):
                if likes.startswith(prefix):
                    likes = likes[len(prefix):].strip()
                    break
            if likes:
                metrics["likes"] = likes
        item = {
            "platform": platform,
            "title": title,
            "url": url,
            "published": row.get("published") or None if platform == "xhs" else None,
            "metrics": metrics,
            "summary": "",
            "source_id": f"{platform}-{len(items) + 1}",
        }
        if platform == "xhs" and row.get("author"):
            item["author"] = str(row["author"]).strip()
        items.append(item)
        if len(items) >= limit:
            break
    return items


_DETAIL_SCRIPT = """(() => {
  const s = window.__INITIAL_STATE__;
  const nd = s && s.note && s.note.noteDetailMap;
  if (!nd) return JSON.stringify({ok: false, reason: 'no state'});
  const key = Object.keys(nd)[0];
  const note = nd[key] && nd[key].note;
  if (!note) return JSON.stringify({ok: false, reason: 'no note'});
  return JSON.stringify({ok: true, title: note.title || '', desc: note.desc || '',
    author: (note.user && note.user.nickname) || '', time: note.time || null,
    images: (note.imageList || []).map(function (img) { return img.urlDefault || img.url || ''; }).filter(Boolean)});
})()"""


def _parse_eval_object(output: str) -> dict[str, Any]:
    text = (output or "").strip()
    if not text:
        return {}
    start = min((pos for pos in (text.find("{"), text.find("[")) if pos >= 0), default=-1)
    if start > 0:
        text = text[start:]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if isinstance(payload, dict):
        for key in ("result", "output", "data", "value"):
            nested = payload.get(key)
            if isinstance(nested, str):
                try:
                    return json.loads(nested)
                except json.JSONDecodeError:
                    continue
    return payload if isinstance(payload, dict) else {}


def _fetch_note_detail(url: str, deadline: float) -> dict[str, Any]:
    remaining = deadline - time.monotonic()
    if remaining <= 2:
        return {}
    _run_opencli(["browser", "main", "open", url], max(1.0, remaining - PAGE_WAIT), "open")
    time.sleep(min(PAGE_WAIT, max(0.0, deadline - time.monotonic() - 0.5)))
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return {}
    output = _run_opencli(["browser", "main", "eval", _DETAIL_SCRIPT], remaining, "eval")
    data = _parse_eval_object(output)
    return data if data.get("ok") else {}


def _vision_describe(url: str) -> str:
    try:
        from vision_agnes import describe

        return describe(url).strip()
    except Exception:
        return ""


def _download_image(url: str, dest: Path, timeout: float = 15.0) -> bool:
    import urllib.request

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36",
            "Referer": "https://www.xiaohongshu.com/",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            dest.write_bytes(resp.read())
        return dest.stat().st_size > 1024
    except Exception:
        return False


def _ocr_note_images(urls: list[str], limit: int, deadline: float) -> str:
    if limit <= 0 or not urls:
        return ""
    import tempfile

    from ocr_local import ocr_images

    with tempfile.TemporaryDirectory(prefix="collect-ocr-", dir=str(SCRIPT_DIR)) as tmp:
        saved: list[Path] = []
        for idx, url in enumerate(urls[:limit], start=1):
            if time.monotonic() >= deadline - 5:
                break
            dest = Path(tmp) / f"img{idx}.webp"
            if _download_image(url, dest):
                saved.append(dest)
        if not saved:
            return ""
        try:
            rows = ocr_images(saved)
        except Exception:
            return ""
    texts = [row["text"].strip() for row in rows if row.get("text", "").strip()]
    joined = " ".join(texts)
    return joined[:1200]


def _collect_platform(platform: str, query: str, limit: int, deadline: float) -> list[dict[str, Any]]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise OpenCLIError("open", "open failed: platform timeout reached")
    _run_opencli(["browser", "main", "open", _search_url(platform, query)], remaining, "open")
    remaining = deadline - time.monotonic()
    if remaining < PAGE_WAIT:
        raise OpenCLIError("eval", "eval no output: platform timeout reached before page wait")
    time.sleep(PAGE_WAIT)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise OpenCLIError("eval", "eval no output: platform timeout reached")
    output = _run_opencli(
        ["browser", "main", "eval", _script_for(platform)], remaining, "eval"
    )
    try:
        rows = _parse_eval_output(output)
    except ValueError as exc:
        raise OpenCLIError("eval", f"parse failed: {exc}") from exc
    return _parse_rows(platform, rows, limit)


def run_collection(
    query: str,
    platforms: tuple[str, ...] | list[str] = PLATFORMS,
    limit: int = 5,
    detail: int = 0,
    images: int = 0,
    vision: int = 0,
    agnes: bool = False,
) -> dict[str, Any]:
    started = time.monotonic()
    total_deadline = started + TOTAL_TIMEOUT + max(0, detail) * 15 + max(0, images) * 10 + max(0, vision) * 20
    items: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    counts: dict[str, int] = {}
    for platform in platforms:
        if platform not in PLATFORMS:
            errors[platform] = "unsupported platform"
            counts[platform] = 0
            continue
        if time.monotonic() >= total_deadline:
            errors[platform] = "open failed: total timeout reached"
            counts[platform] = 0
            continue
        deadline = min(
            time.monotonic() + PLATFORM_TIMEOUT + (max(0, detail) * 14 + max(0, images) * 10 + max(0, vision) * 20 if platform == "xhs" else 0),
            total_deadline,
        )
        try:
            rows = _collect_platform(platform, query, limit, deadline)
            if platform == "xhs" and detail > 0 and rows:
                enriched = 0
                for idx, item in enumerate(rows[:detail]):
                    if time.monotonic() >= deadline - 2:
                        break
                    info = _fetch_note_detail(item["url"], deadline)
                    if not info:
                        continue
                    if info.get("desc"):
                        item["summary"] = str(info["desc"]).strip()
                    if info.get("title"):
                        item["detail_title"] = str(info["title"]).strip()
                    if info.get("author"):
                        item.setdefault("metrics", {})["author"] = info["author"]
                    if images > 0 and info.get("images"):
                        ocr_text = _ocr_note_images(list(info["images"]), images, deadline)
                        if ocr_text:
                            existing = item.get("summary") or ""
                            item["summary"] = (existing + " " + ocr_text).strip()
                            item["ocr_text"] = ocr_text
                    if vision > 0 and idx < vision and info.get("images") and len(item.get("ocr_text") or "") < 120:
                        # Agent-first: mark the image for the assistant's own vision; no external call here.
                        item["needs_vision"] = True
                        item["vision_url"] = list(info["images"])[0]
                        if agnes and time.monotonic() < deadline - 5:
                            vtext = _vision_describe(item["vision_url"])
                            if vtext:
                                item["vision_text"] = vtext
                                item["vision_source"] = "agnes"
                                item["summary"] = ((item.get("summary") or "") + " [看图] " + vtext).strip()
                    enriched += 1
                counts[f"{platform}_detail"] = enriched
            items.extend(rows)
            counts[platform] = len(rows)
        except OpenCLIError as exc:
            errors[platform] = str(exc)
            counts[platform] = 0
        except Exception as exc:
            errors[platform] = f"parse failed: {exc}"
            counts[platform] = 0
    return {
        "query": query,
        "items": items,
        "stats": {"counts": counts, "total_count": len(items), "elapsed_seconds": round(time.monotonic() - started, 3)},
        "errors": errors,
    }


def _human_output(payload: dict[str, Any], platforms: tuple[str, ...]) -> str:
    lines: list[str] = []
    for platform in platforms:
        for item in payload["items"]:
            if item["platform"] != platform:
                continue
            details = [str(item["published"])] if item.get("published") else []
            likes = (item.get("metrics") or {}).get("likes")
            if likes:
                details.append(f"赞 {likes}")
            suffix = f"（{' / '.join(details)}）" if details else ""
            lines.append(f"[{PLATFORM_LABELS.get(platform, platform)}] {item['title']} — {item['url']}{suffix}")
            summary = (item.get("summary") or "").strip()
            if summary:
                lines.append("    正文：" + " ".join(summary.split())[:160])
            if item.get("needs_vision") and not item.get("vision_text"):
                lines.append("    需识图：" + str(item.get("vision_url") or ""))
    counts = payload["stats"]["counts"]
    lines.append("平台统计：" + ", ".join(f"{PLATFORM_LABELS.get(name, name)}={counts.get(name, 0)}条" for name in platforms))
    failures = "; ".join(f"{name}: {reason}" for name, reason in payload["errors"].items()) or "无"
    lines.append(f"失败原因：{failures}")
    lines.append(f"总耗时：{payload['stats']['elapsed_seconds']:.3f}s")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect results from logged-in browser sessions.")
    parser.add_argument("query")
    parser.add_argument("--platforms", default=",".join(PLATFORMS))
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--detail", type=int, default=0, help="enrich first N 小红书 notes with title/desc from note page")
    parser.add_argument("--images", type=int, default=0, help="OCR first N images of each enriched 小红书 note")
    parser.add_argument("--vision", type=int, default=0, help="mark first N notes with thin OCR text as needing agent vision")
    parser.add_argument("--agnes", action="store_true", help="fallback: describe marked images with free Agnes vision")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--out", metavar="FILE")
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("--limit must be at least 1")
    platforms = tuple(dict.fromkeys(part.strip().lower() for part in args.platforms.split(",") if part.strip()))
    unknown = [name for name in platforms if name not in PLATFORMS]
    if unknown:
        parser.error("unknown platform(s): " + ", ".join(unknown))
    payload = run_collection(args.query, platforms, args.limit, args.detail, args.images, args.vision, args.agnes)
    json_text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out:
        try:
            Path(args.out).write_text(json_text + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"collect_browser: could not write output file: {exc}", file=sys.stderr)
            return 2
    print(json_text if args.as_json else _human_output(payload, platforms))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
