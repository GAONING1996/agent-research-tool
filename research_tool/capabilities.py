"""Capability registry: the single source of truth for setup prompts.

Every configurable capability declares what it buys, how the user obtains it, what the
agent should say to the user, and what happens if it stays unconfigured. Nothing here
degrades silently: a missing capability is reported as `missing` until the user either
configures it or explicitly declines it (`research config decline`).
"""

from __future__ import annotations

import importlib.util
import os
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path

try:
    from . import config
except ImportError:  # pragma: no cover - script mode
    import config


@dataclass
class Capability:
    key: str
    label: str
    tier: str          # required | user_key | agent_install | local_service | optional
    why: str
    asked_as: str
    how_to_get: str
    missing_effect: str
    probe: str         # env | module:<name> | command:<name> | http:<url> | any_env:A,B
    docs: str = ""
    alternatives: list[str] = field(default_factory=list)


REGISTRY: tuple[Capability, ...] = (
    Capability(
        key="TAVILY_API_KEY",
        label="Tavily 搜索",
        tier="required",
        why="网页搜索主通道；没有它就只能用 SearXNG。",
        asked_as="需要你给一个 Tavily 的 API key，去 tavily.com 免费注册就能拿到，两分钟的事。",
        how_to_get="https://tavily.com 注册后在控制台复制 API key，然后：echo '{\"TAVILY_API_KEY\":\"你的key\"}' | research config set --stdin",
        missing_effect="只能依赖 SearXNG；两个都没有则无法搜索，调研无法开始。",
        probe="env",
    ),
    Capability(
        key="SEARXNG_URL",
        label="SearXNG 自建搜索",
        tier="required",
        why="第二路搜索，和 Tavily 交叉验证；也可以用公共实例或本机 docker。",
        asked_as="如果你不想用 Tavily，可以给我一个 SearXNG 地址（自己 docker 起一个也算），二选一即可。",
        how_to_get="docker run -d --name searxng -p 8080:8080 searxng/searxng，然后设置 SEARXNG_URL=http://127.0.0.1:8080",
        missing_effect="少一路搜索源，交叉验证能力下降；仍可用 Tavily 单路工作。",
        probe="env",
        alternatives=["TAVILY_API_KEY"],
    ),
    Capability(
        key="GROQ_API_KEY",
        label="视频/播客转写（Groq Whisper）",
        tier="user_key",
        why="没有字幕的视频、播客、长音频要靠它转文字。",
        asked_as="要不要开转写？需要去 console.groq.com 注册拿一个免费 key，不给的话视频和播客只能拿到标题。",
        how_to_get="https://console.groq.com 注册，创建 API key，然后：echo '{\"GROQ_API_KEY\":\"你的key\"}' | research config set --stdin",
        missing_effect="视频/播客只能拿标题与简介，无字幕内容读不到。",
        probe="env",
    ),
    Capability(
        key="AGNES_API_KEY",
        label="识图兜底（Agnes）",
        tier="user_key",
        why="当前对话模型看不了图时，用免费模型读图表、截图、笔记图。",
        asked_as="要不要开识图兜底？需要一个免费的 Agnes key；不给的话图片只能做本机文字 OCR。",
        how_to_get="按 Agnes 平台文档注册后取 key，然后：echo '{\"AGNES_API_KEY\":\"你的key\"}' | research config set --stdin",
        missing_effect="图片只能走本机 OCR（只认字，不认版式与图表）。",
        probe="env",
    ),
    Capability(
        key="OPEN_NOTEBOOK_URL",
        label="Open Notebook 正文抽取",
        tier="local_service",
        why="强风控站点与长文页面用它抽正文，实测同批页面正文量提升数倍。",
        asked_as="如果要抓强风控站点或长文，需要在有 Docker 的机器上起一个 Open Notebook，我可以用 docker compose 帮你起；不想装也能跑，正文会短一些。",
        how_to_get="仓库 scripts/open_notebook/docker-compose.yml：docker compose up -d，然后设置 OPEN_NOTEBOOK_URL=http://127.0.0.1:5055",
        missing_effect="回落 Tavily/直连/浏览器抓取，部分站点正文偏短或抓不到。",
        probe="env",
    ),
    Capability(
        key="JINA_API_KEY",
        label="Jina Reader（Open Notebook 内部引擎）",
        tier="user_key",
        why="Open Notebook 的第一层抽取引擎，免费额度足够调研用。",
        asked_as="Open Notebook 要接 Jina 才快，需要你注册一个免费 key（约 100 万 token）；不给它就只能退回自带的 bs4 抓取。",
        how_to_get="https://jina.ai/api-dashboard 注册取 key，写进 scripts/open_notebook/.env 的 JINA_API_KEY，再 docker compose up -d --force-recreate",
        missing_effect="Open Notebook 只能走 bs4，风控站点大概率抽不到。",
        probe="open_notebook_env",
    ),
    Capability(
        key="OPENCLI_PATH",
        label="登录态浏览器通道（OpenCLI）",
        tier="local_service",
        why="知乎/小红书/Reddit 这类要登录的站点，用它复用本机 Chrome 的登录状态。",
        asked_as="要抓知乎、小红书、Reddit 的话，需要你在本机 Chrome 里登录一次，并装 OpenCLI 扩展；不装就只能抓公开页面。",
        how_to_get="安装 OpenCLI（https://github.com/jackwener/opencli），保持 Chrome 运行并登录目标站点，然后设置 OPENCLI_PATH 指向可执行文件。",
        missing_effect="登录态平台不可用，自动退回普通网页抓取，部分内容拿不到。",
        probe="opencli",
    ),
    Capability(
        key="CHROME_PATH",
        label="本机 Chrome",
        tier="local_service",
        why="无头浏览器抓取与登录态通道都依赖它。",
        asked_as="需要本机装 Chrome（一般都有），没有的话浏览器级抓取用不了。",
        how_to_get="安装 Google Chrome；装在非默认路径时设置 CHROME_PATH。",
        missing_effect="无头浏览器通道不可用，只剩 HTTP 与搜索接口抓取。",
        probe="chrome",
    ),
    Capability(
        key="YT_DLP_PATH",
        label="yt-dlp（YouTube/B站/音频下载）",
        tier="agent_install",
        why="取视频元数据、字幕与音频；转写流程的前置。",
        asked_as="这一项我自己装，你不用管；装不上再找你。",
        how_to_get="pip install yt-dlp 或下载官方二进制，然后设置 YT_DLP_PATH。",
        missing_effect="YouTube/播客音频拿不到，B站仍可用 bili-cli。",
        probe="ytdlp",
    ),
    Capability(
        key="BILI_PATH",
        label="bili-cli（B站）",
        tier="agent_install",
        why="B站视频标题、作者、简介与字幕。",
        asked_as="这一项我自己装，你不用管。",
        how_to_get="安装 bili-cli 后设置 BILI_PATH，或放进 PATH。",
        missing_effect="B站渠道不可用，其他渠道不受影响。",
        probe="bili",
    ),
    Capability(
        key="PyMuPDF",
        label="PDF 直读（PyMuPDF）",
        tier="agent_install",
        why="标准、论文、报告多是 PDF，直接抽正文比网页抓取可靠。",
        asked_as="这一项我自己装，你不用管。",
        how_to_get="pip install 'agent-research-tool[pdf]'",
        missing_effect="PDF 只能记录链接，读不到正文。",
        probe="module:fitz",
    ),
    Capability(
        key="GITHUB_TOKEN",
        label="GitHub issue/discussion 搜索配额",
        tier="user_key",
        why="查开源项目踩坑时答案多在 issue 和 discussion；带 token 配额从每小时 60 次升到 5000 次。",
        asked_as="如果你有 GitHub 账号，给一个只读 token（Settings → Developer settings → Personal access tokens，勾 public_repo 或只读权限即可），GitHub 搜索会稳很多；不给也能用，只是每小时 60 次。",
        how_to_get="GitHub 设置里生成 classic token（无需任何 scope 也能提高限额），然后：echo '{\"GITHUB_TOKEN\":\"ghp_xxx\"}' | research config set --stdin",
        missing_effect="GitHub 仓库与 issue 搜索仍可用，但每小时 60 次、搜索接口每分钟 10 次，批量调研容易触限。",
        probe="env",
    ),
    Capability(
        key="RESEARCH_PROXY",
        label="境外源代理",
        tier="optional",
        why="YouTube、Groq、Jina 在部分网络下需要代理。",
        asked_as="如果你这边访问 YouTube 或 Groq 不通，告诉我代理地址，我配上；本来就通就不用管。",
        how_to_get="设置 RESEARCH_PROXY=http://127.0.0.1:7897（按你的代理软件端口改）",
        missing_effect="境外源可能超时；国内源不受影响。",
        probe="env",
    ),
)

BY_KEY = {capability.key: capability for capability in REGISTRY}
SETUP_DECLINED = "SETUP_DECLINED"


def declined() -> set[str]:
    raw = config.get(SETUP_DECLINED, "")
    return {item.strip() for item in raw.split(",") if item.strip()}


def decline(key: str, reason: str = "") -> None:
    current = declined()
    current.add(key)
    config.write_value(SETUP_DECLINED, ",".join(sorted(current)))
    if reason:
        config.write_value(f"SETUP_DECLINED_{key}", reason)


def _probe(capability: Capability) -> tuple[bool, str]:
    probe = capability.probe
    if probe == "chrome":
        found = config.chrome_path()
        return (bool(found), str(found) if found else "not found")
    if probe == "ytdlp":
        found = config.resolve_program("YT_DLP_PATH", "yt-dlp",
                                       candidates=(str(Path.home() / ".agent-reach-venv" / "Scripts" / "yt-dlp.exe"),))
        return (bool(found), str(found) if found else "not found")
    if probe == "opencli":
        found = config.resolve_program("OPENCLI_PATH", "opencli",
                                       candidates=(str(Path.home() / "AppData" / "Local" / "hermes" / "node" / "opencli"),))
        return (bool(found), str(found) if found else "not found")
    if probe == "bili":
        found = config.resolve_program("BILI_PATH", "bili",
                                       candidates=(str(Path.home() / ".agent-reach-venv" / "Scripts" / "bili.exe"),))
        return (bool(found), str(found) if found else "not found")
    if probe == "open_notebook_env":
        value = config.get(capability.key)
        if not value:
            env_path = config.PACKAGE_DIR.parent / "open_notebook" / ".env"
            try:
                for line in env_path.read_text(encoding="utf-8-sig").splitlines():
                    key, separator, item = line.partition("=")
                    if separator and key.strip() == capability.key:
                        value = item.strip().strip('"').strip("'")
            except OSError:
                pass
        return (bool(value), "set" if value else "not set")
    if probe == "env":
        value = config.get(capability.key)
        if capability.key == "RESEARCH_PROXY":
            value = config.proxy_url()
        return (bool(value), "set" if value else "not set")
    if probe.startswith("module:"):
        name = probe.split(":", 1)[1]
        found = importlib.util.find_spec(name) is not None
        return (found, "installed" if found else "not installed")
    if probe.startswith("command:"):
        name = probe.split(":", 1)[1]
        configured = config.get(capability.key)
        if configured and os.path.exists(configured):
            return (True, configured)
        resolved = shutil.which(name)
        return (bool(resolved), resolved or "not found")
    return (False, "unknown probe")


def statuses() -> list[dict]:
    """Probe every capability and attach setup state. Never raises."""
    skipped = declined()
    rows = []
    for capability in REGISTRY:
        ready, detail = _probe(capability)
        if ready:
            state = "ready"
        elif capability.key in skipped:
            state = "declined"
        else:
            state = "missing"
        row = asdict(capability)
        row.update({"state": state, "detail": detail})
        rows.append(row)
    return rows


def pending() -> list[dict]:
    """Capabilities the user has neither configured nor declined."""
    return [row for row in statuses() if row["state"] == "missing"]


def search_ready() -> bool:
    return bool(config.get("TAVILY_API_KEY") or config.get("SEARXNG_URL"))


def require(key: str) -> None:
    """Raise a user-actionable error when a capability is missing and not declined."""
    capability = BY_KEY.get(key)
    if capability is None:
        return
    ready, _detail = _probe(capability)
    if ready:
        return
    if key in declined():
        return
    raise CapabilityMissing(capability)


class CapabilityMissing(RuntimeError):
    def __init__(self, capability: Capability):
        super().__init__(capability.asked_as)
        self.capability = capability
