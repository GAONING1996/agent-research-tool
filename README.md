# agent-research-tool

给 AI agent 用的树状调研工具：发现 → 抓正文 → 判卷 → 继续拆 → 汇总成报告。
人在这个流程里只做两件事：**给 key**、**在浏览器里登录**。其余全部由 agent 完成。

命令行是唯一真源；MCP 是可选薄壳（`research-mcp`），两者调用同一份实现。

---

## 1. 安装

需要 Python 3.10+。

> 尚未发布到 PyPI，请从 GitHub 安装。下面两条等价，选一条即可。

```bash
# 方式一：pipx（隔离环境，推荐）
pipx install "git+https://github.com/GAONING1996/agent-research-tool.git"

# 方式二：pip
pip install "git+https://github.com/GAONING1996/agent-research-tool.git"

# 需要 PDF 直读 / MCP 时，在第二种方式上加 extras：
pip install "agent-research-tool[pdf,mcp] @ git+https://github.com/GAONING1996/agent-research-tool.git"
```

装完有两个命令：

| 命令 | 用途 |
|---|---|
| `research` | 主入口，所有调研动作 |
| `research-mcp` | MCP stdio 服务，给 Claude Desktop / Codex / 其他支持 MCP 的客户端 |

从源码装（要改代码时用）：

```bash
git clone https://github.com/GAONING1996/agent-research-tool.git
cd agent-research-tool
pip install -e "."
```

---

## 2. 配置：先问人，不静默降级

工具的能力分三类：**必须有的**（搜索）、**要用户给 key 的**（转写、识图）、**agent 自己能装的**（yt-dlp、PyMuPDF、本地服务）。

正确的顺序，agent 必须照做：

```bash
research doctor --json      # 1. 探测环境，拿到结构化清单
```

返回里有三个关键字段：

- `needs_user`：true 表示有必须由人提供的东西。
- `requirements[]`：每条带 `asked_as`（**可以直接转述给用户的一句话**）、`how_to_fix`、`missing_effect`、`decline_command`。
- `pending_agent_setup[]`：agent 自己该装的东西（pip 包、docker 服务），不要拿去烦用户。

agent 拿到 `requirements` 后，逐条问用户；用户给了就写进去：

```bash
# bash / zsh / git-bash
echo '{"TAVILY_API_KEY":"..."}' | research config set --stdin --json

# Windows PowerShell
'{"TAVILY_API_KEY":"..."}' | research config set --stdin --json
```

**用户明确表示拿不到时**，才允许降级，并且必须显式记录，不能默默跳过：

```bash
research config decline --key GROQ_API_KEY --reason "用户没有 Groq 账号"
```

之后 `doctor` 会把这项标成 `declined`，不再重复打扰，同时在结果里保留“这项没配、会损失什么”。

用户想自己过一遍全部配置项：`research setup --interactive`。
只想看还差什么：`research setup --json`。

### 配置项从哪读

进程环境变量 > 项目 `.env` > 用户配置文件 > 默认值。

| 平台 | 用户配置文件 |
|---|---|
| Windows | `%LOCALAPPDATA%\research-tool\config.env` |
| Linux / macOS | `~/.local/share/research-tool/config.env` |

查看实际生效位置：`research config where --json`。
写入一律走 `config set`（不回显、POSIX 上权限 600），**不要把 key 写进仓库**。

### 能力清单

| 能力 | 配置项 | 谁来解决 | 不配的后果 |
|---|---|---|---|
| 网页搜索（主） | `TAVILY_API_KEY` | 用户给 key | 只能靠 SearXNG；都没有则调研无法开始 |
| 网页搜索（辅） | `SEARXNG_URL` | 用户/agent 起 docker | 少一路交叉验证 |
| 视频/播客转写 | `GROQ_API_KEY` | 用户给 key | 无字幕内容只拿得到标题 |
| 识图兜底 | `AGNES_API_KEY` | 用户给 key | 图片只能本机 OCR（只认字） |
| 长文抽取 | `OPEN_NOTEBOOK_URL` + `JINA_API_KEY` | agent 起 docker（上游 Open Notebook）+ 用户给 Jina key | 风控站点正文偏短或抓不到 |
| 登录态平台 | Chrome + `OPENCLI_PATH` | 用户登录一次 + 装扩展 | 知乎/小红书/Reddit 只能抓公开页 |
| YouTube/音频 | `YT_DLP_PATH` | agent 装 | YouTube 与播客音频不可用 |
| B 站 | `BILI_PATH` | agent 装 | B 站渠道不可用 |
| PDF 直读 | `PyMuPDF` | agent 装 | PDF 只有链接，没有正文 |
| 境外源代理 | `RESEARCH_PROXY` | 按网络情况 | 境外源可能超时（国内源不受影响） |

全部配齐后，能力与作者本机一致；差异只在配置，不在代码。

---

## 3. 用起来

```bash
research init "问题" --state tree.json --purpose "给谁、用来干什么" --deliverable "每条结论要含什么"
research set-node --state tree.json --node n0 --queries "查询1" "查询2" --counter-queries "反驳查询"
research run --state tree.json --ready --fetch 4
research show --state tree.json --node n0 --chars 1500
research apply-review --state tree.json --file review.json
research claim add --state tree.json --node n0 --text "结论" --evidence E1,E2 --support dual
research status --state tree.json
research digest --state tree.json
```

规则：

- **至少四层**。`depth 0` 是第 1 层；`partial` / `not_found` 必须继续拆，除非显式 `--allow-early-close` 并写理由。
- **判卷权在 agent**。脚本只报事实（字数、质量线索、抓取失败数），不判定“够不够”。
- **每个节点自己指定查询与渠道**，不要依赖脚本按问题字面切词。
- **证据编号按节点编号**；写 `claim` 前先 `show` 确认 `[E#]` 顺序。
- 跑完一波先 `status` 看层数与待拆节点，再决定继续拆还是收口。

### 常用入口

| 脚本 | 用途 |
|---|---|
| `research` | 树状调研全流程 |
| `fetch_smart` | 四级抓正文（Tavily → HTTP → 无头 Chrome → 已登录会话；可选 PDF、滚动翻页、Open Notebook） |
| `collect` | 七渠道采集：web / bili / youtube / hn / github / rss / podcast / stats |
| `search_dual` | Tavily + SearXNG 并行搜索并去重 |
| `rss_reader` | 订阅源轮转读取 |
| `collect_browser` | 登录态平台（小红书 / 知乎 / Reddit） |

全部支持 `--json`。统一外壳：`{ok, schema_version, command, data}`，失败为 `{ok:false, error:{code,message,hint,next_command}}`。
退出码：`0` 成功、`2` 参数/校验错、`3` 状态不允许、`4` 采集或网络失败、`6` 需要用户操作（看 `error.asked_as`）。

---

## 4. MCP（可选）

```bash
pip install "agent-research-tool[mcp] @ git+https://github.com/GAONING1996/agent-research-tool.git"
research-mcp                      # stdio
```

Windows 上如果客户端找不到 `research-mcp`，用完整路径（`pipx` 装在 `%LOCALAPPDATA%\pipx\venvs\agent-research-tool\Scripts\research-mcp.exe`，`pip` 装在 Python 的 `Scripts` 目录）。

Claude Desktop / Codex 的配置写法（以 Codex 为例）：

```bash
codex mcp add research -- research-mcp
```

提供的工具：`doctor`、`init`、`set_node`、`run`、`show`、`review`、`claim`、`digest`、`status`。
工具内部调用的就是同一个 CLI，不会出现两套行为。

---

## 5. 给 agent 的冷启动验收

新机器、空目录、没有任何历史上下文，只按本文件与 `research spec --json` 操作，应当能跑完：

1. `research doctor --json` → 有 `requirements` 就按 `asked_as` 问用户；
2. `research init ...` 建树；
3. `research set-node ...` 写查询；
4. `research run --ready --fetch 3` 拿到 ≥1 条正文（`output` 里 `bodies > 0`）；
5. `research show` 读到正文原文；
6. `research apply-review` → `claim add` → `digest` 出汇总。

哪一步需要“我记得应该这样”，就是文档还缺东西，请开 issue。

---

## 6. 数据与隐私

- 状态文件、锁文件、临时数据默认放用户数据目录，不写安装目录。
- 不导出、不读取浏览器的 cookie 文件；登录态只复用用户已打开的浏览器会话。
- 密钥只进环境变量或用户配置文件，不写日志、不进仓库。
- 抓取失败会记录原因，不静默丢弃。

## 7. 开发

```bash
pip install -e ".[dev]"
python -m unittest discover          # 169 项离线测试
```

## License

MIT，见 `LICENSE`。
