"""Central configuration for the research tool.

Resolution order (first hit wins):
    process environment  >  project .env  >  user config file  >  built-in default

The same code runs on the author's machine and on a fresh clone: only the values differ.
Nothing here prompts, blocks, or requires a specific OS layout.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent

# .env files are looked up next to the package and one level up (legacy Hermes layout).
DOTENV_CANDIDATES = (
    PACKAGE_DIR / ".env",
    PACKAGE_DIR.parent / ".env",
    PACKAGE_DIR.parent.parent / ".env",
)

_cache: dict[str, dict[str, str]] | None = None


def data_dir() -> Path:
    """Writable directory for state, locks and user config (never the install dir)."""
    override = (os.environ.get("RESEARCH_DATA_DIR") or "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = (os.environ.get("LOCALAPPDATA") or "").strip() or str(Path.home() / "AppData" / "Local")
        return Path(base) / "research-tool"
    base = (os.environ.get("XDG_DATA_HOME") or "").strip()
    return (Path(base) if base else Path.home() / ".local" / "share") / "research-tool"


def config_file() -> Path:
    override = (os.environ.get("RESEARCH_CONFIG") or "").strip()
    return Path(override).expanduser() if override else data_dir() / "config.env"


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return values
    for line in lines:
        candidate = line.strip()
        if not candidate or candidate.startswith("#"):
            continue
        if candidate.startswith("export "):
            candidate = candidate[7:].lstrip()
        key, separator, value = candidate.partition("=")
        if not separator:
            continue
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def _sources() -> dict[str, dict[str, str]]:
    global _cache
    if _cache is None:
        project: dict[str, str] = {}
        for path in DOTENV_CANDIDATES:
            project.update(_parse_env_file(path))
        _cache = {"project": project, "user": _parse_env_file(config_file())}
    return _cache


def refresh() -> None:
    """Forget cached .env values (used after setup writes a new value)."""
    global _cache
    _cache = None


def find_env_file() -> Path | None:
    """First .env that actually exists, in lookup order."""
    for path in DOTENV_CANDIDATES:
        if path.is_file():
            return path
    return None


def get(name: str, default: str = "") -> str:
    value = (os.environ.get(name) or "").strip()
    if value:
        return value
    sources = _sources()
    for scope in ("project", "user"):
        value = (sources[scope].get(name) or "").strip()
        if value:
            return value
    return default


def get_int(name: str, default: int) -> int:
    try:
        return int(get(name, str(default)))
    except ValueError:
        return default


def get_float(name: str, default: float) -> float:
    try:
        return float(get(name, str(default)))
    except ValueError:
        return default


def resolve_program(env_var: str, command: str, candidates: tuple[str, ...] = ()) -> Path | None:
    """Config value, then PATH lookup, then platform-specific candidate paths."""
    configured = get(env_var)
    if configured:
        path = Path(configured).expanduser()
        return path if path.exists() else None
    found = shutil.which(command)
    if found:
        return Path(found)
    for candidate in candidates:
        path = Path(candidate).expanduser()
        if path.exists():
            return path
    return None


def proxy_url() -> str:
    """Explicit RESEARCH_PROXY, else standard proxy env vars, else empty (no hard-coded proxy)."""
    for name in ("RESEARCH_PROXY", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return get("RESEARCH_PROXY", "").strip()


def chrome_path() -> Path | None:
    candidates = (
        r"C:/Program Files/Google/Chrome/Application/chrome.exe",
        r"C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
    )
    return resolve_program("CHROME_PATH", "chrome", candidates)


def write_value(name: str, value: str) -> Path:
    """Persist a value into the user config file with 0600 permissions."""
    path = config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _parse_env_file(path)
    existing[name] = value
    body = "\n".join(f"{key}={item}" for key, item in existing.items()) + "\n"
    path.write_text(body, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    refresh()
    return path


def describe() -> dict[str, str]:
    """Where each config layer lives, for doctor/debug output."""
    return {
        "package_dir": str(PACKAGE_DIR),
        "data_dir": str(data_dir()),
        "config_file": str(config_file()),
        "dotenv_files": ", ".join(str(path) for path in DOTENV_CANDIDATES),
        "proxy": proxy_url() or "(none)",
    }
