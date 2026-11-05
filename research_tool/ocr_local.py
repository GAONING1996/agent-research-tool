"""Local OCR via Windows built-in engine (Windows.Media.Ocr).

No extra installs: PIL converts webp/heic to png, PowerShell calls the WinRT OCR engine.
Usage:
    python ocr_local.py <image|dir> [--json] [--max 5]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PS_SCRIPT = SCRIPT_DIR / "ocr_image.ps1"
_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif", ".heic", ".tif", ".tiff"}
_CJK = r"\u4e00-\u9fff"


def _clean(text: str) -> str:
    text = re.sub(rf"(?<=[{_CJK}]) (?=[{_CJK}])", "", text)
    text = re.sub(rf"(?<=[{_CJK}]) (?=[，。！？；：、])", "", text)
    text = re.sub(rf"(?<=[，。！？；：、]) (?=[{_CJK}])", "", text)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _to_png(src: Path, out_dir: Path) -> Path:
    if src.suffix.lower() == ".png":
        return src
    from PIL import Image  # local import so --help works without PIL

    out = out_dir / (src.stem + ".png")
    with Image.open(src) as image:
        image.convert("RGB").save(out)
    return out


def ocr_images(paths: list[Path], timeout_seconds: float | None = None) -> list[dict[str, str]]:
    if not paths:
        return []
    if not PS_SCRIPT.exists():
        raise FileNotFoundError(f"missing OCR script: {PS_SCRIPT}")
    with tempfile.TemporaryDirectory(prefix="ocr-local-", dir=str(SCRIPT_DIR)) as tmp:
        tmp_dir = Path(tmp)
        prepared: list[Path] = []
        for path in paths:
            try:
                prepared.append(_to_png(path, tmp_dir))
            except Exception:  # unreadable image: skip, caller sees empty text
                continue
        if not prepared:
            return []
        cmd = [
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(PS_SCRIPT), "-Paths", *[str(p) for p in prepared],
        ]
        timeout = max(60, 30 * len(prepared)) if timeout_seconds is None else max(0.05, timeout_seconds)
        subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        results = []
        for path in prepared:
            txt_path = Path(str(path) + ".txt")
            text = txt_path.read_text(encoding="utf-8", errors="replace") if txt_path.exists() else ""
            results.append({"path": str(path), "text": _clean(text)})
        return results


def _iter_inputs(target: Path, limit: int) -> list[Path]:
    if target.is_file():
        return [target]
    files = sorted(
        (p for p in target.rglob("*") if p.is_file() and p.suffix.lower() in _IMAGE_EXT),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return files[:limit]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OCR images with the Windows built-in engine.")
    parser.add_argument("target", help="image file or directory")
    parser.add_argument("--max", type=int, default=5, help="max images when target is a directory")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    target = Path(args.target).expanduser()
    if not target.exists():
        print(f"not found: {target}", file=sys.stderr)
        return 2
    results = ocr_images(_iter_inputs(target, max(1, args.max)))
    if not results:
        print("no readable images", file=sys.stderr)
        return 1
    if args.as_json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for row in results:
            print(f"== {row['path']} ==")
            print(row["text"] or "(no text)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
