"""Free vision via Agnes (agnes-3.0-flash). Local file or URL -> Chinese description.

Usage:
    python vision_agnes.py <image|url> [--prompt "..."] [--json]

Reads AGNES_API_KEY from env or from the Hermes .env file. Direct connection:
Agnes does not use the local proxy (clear HTTP(S)_PROXY before calling).
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ENDPOINT = "https://apihub.agnes-ai.com/v1/chat/completions"
MODEL = "agnes-3.0-flash"
try:
    from . import config
except ImportError:
    import config


def _env_file() -> Path:
    return config.find_env_file() or config.config_file()
SCRIPT_DIR = Path(__file__).resolve().parent
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36"
DEFAULT_PROMPT = (
    "这是一张中文游戏或攻略截图。请提取里面的全部有效信息："
    "阵容/英雄、站位关系、装备、数值、结论。用中文分点回答，不要描述界面外观。"
)


def _api_key() -> str:
    key = os.environ.get("AGNES_API_KEY") or os.environ.get("AGNES_TOKEN")
    if key:
        return key.strip()
    env_file = _env_file()
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("AGNES_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("AGNES_API_KEY missing")


def _curl(args: list[str], timeout: float = 90.0) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k.lower() not in ("http_proxy", "https_proxy", "all_proxy")}
    return subprocess.run(["curl", "-sS", *args], capture_output=True, timeout=timeout, env=env)


def _download(url: str, dest: Path) -> bool:
    proc = _curl(["-L", "-m", "30", "-A", _UA, "-e", "https://www.xiaohongshu.com/", "-o", str(dest), url])
    return proc.returncode == 0 and dest.exists() and dest.stat().st_size > 1024


def _image_part(target: str, workdir: Path) -> str:
    if target.startswith("data:"):
        return target
    if target.startswith(("http://", "https://")):
        local = workdir / "remote.img"
        if not _download(target, local):
            raise RuntimeError(f"download failed: {target[:80]}")
    else:
        local = Path(target)
        if not local.exists():
            raise FileNotFoundError(target)
    mime = "image/webp" if local.suffix.lower() == ".webp" else (mimetypes.guess_type(local.name)[0] or "image/png")
    return f"data:{mime};base64," + base64.b64encode(local.read_bytes()).decode()


def describe(image: str, prompt: str = DEFAULT_PROMPT, timeout: float = 120.0) -> str:
    key = _api_key()
    with tempfile.TemporaryDirectory(prefix="vision-agnes-", dir=str(SCRIPT_DIR)) as tmp:
        workdir = Path(tmp)
        body = {
            "model": MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": _image_part(image, workdir)}},
                    ],
                }
            ],
            "max_tokens": 1200,
        }
        req_path = workdir / "req.json"
        resp_path = workdir / "resp.json"
        req_path.write_text(json.dumps(body), encoding="utf-8")
        proc = _curl(
            [
                "-m", str(int(timeout)),
                "-H", f"Authorization: Bearer {key}",
                "-H", "Content-Type: application/json",
                "--data-binary", f"@{req_path}",
                "-o", str(resp_path),
                "-w", "%{http_code}",
                ENDPOINT,
            ],
            timeout=timeout + 10,
        )
        raw = resp_path.read_text(encoding="utf-8", errors="replace") if resp_path.exists() else ""
    if not raw.strip():
        raise RuntimeError(f"empty response (curl exit {proc.returncode})")
    payload = json.loads(raw)
    choices = payload.get("choices") or []
    if not choices:
        raise RuntimeError(f"no choices: {raw[:200]}")
    return str(choices[0]["message"]["content"]).strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Describe an image with free Agnes vision.")
    parser.add_argument("image", help="local path or http(s) url")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    try:
        text = describe(args.image, args.prompt)
    except Exception as exc:
        print(f"vision_agnes: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"image": args.image, "text": text}, ensure_ascii=False) if args.as_json else text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
