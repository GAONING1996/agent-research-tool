# Research Script（给 agent 的冷启动说明）

`research.py` 是一套树状调研命令行工具：拆题 → 采集证据 → 逐节点判卷 → 汇总。脚本只做机械活（跑查询、抓正文、存状态），**所有判断（够不够、算不算回答、要不要继续拆）由调用它的模型来做**。

- 位置：本仓库根目录；`pip install .` 后直接用 `research` 命令
- 机器可读契约：`python research.py spec --json`（命令、节点状态、review/claim 字段、E# 引用规则都在里面）
- 本文档是 UTF-8 中英混排；Windows 控制台请先 `chcp 65001` 或按 UTF-8 读取，否则中文显示为乱码。


## 安装与配置（agent 也要知道流程）

```bash
pipx install "agent-research-tool"          # 或 pip install .
pip install "agent-research-tool[pdf,mcp]"  # 可选：PDF 直读 / MCP 入口
```

- 主命令 `research`，可选 MCP 入口 `research-mcp`（stdio）。
- 数据目录（状态、锁、Notebook 临时文件、用户配置）：
  Windows `%LOCALAPPDATA%\research-tool`；Linux/macOS `~/.local/share/research-tool`。
- 配置读取顺序：进程环境变量 > 项目 `.env` > 用户配置文件 > 默认值。
- 查看位置：`research config where --json`。
- 写入密钥：`echo '{"TAVILY_API_KEY":"..."}' | research config set --stdin --json`（不回显、不进仓库）。

### 配置铁律：先提示用户，不许静默降级

1. 开工前先 `research doctor --json`。
2. `needs_user: true` 时，把 `requirements[].asked_as` **原文转述**给用户（那已经是给用户看的一句话），并附 `how_to_fix`。
3. 用户给了值 → `research config set --stdin`。
4. 用户明确说“没有 / 拿不到” → `research config decline --key X --reason "..."`，之后该项标成 `declined`，不再打扰，但报告里要写清损失了什么。
5. `pending_agent_setup[]` 是 agent 自己该装的（pip 包、docker 服务），不要拿去问用户。
6. 禁止“发现缺 key 就换个方式凑合、还不告诉用户”。缺能力只有两种合法结局：配上，或用户明确放弃。

`research setup --json` 可列出全部待配项；`--interactive` 给人用，agent 默认别走交互。

## 一条命令自检

```bash
python research.py doctor --json
```

`ok:true` 且 `needs_user:false` 即可开工。`capabilities[]` 逐项给出 12 项能力的状态（ready / missing / declined）；`requirements[]` 是要问用户的；`pending_agent_setup[]` 是 agent 自己装的。

## 最小完整流程

第一步先做主题解析卡，不要直接写查询：

```bash
python research.py brief --topic "调研主题" --out brief.json
python research.py brief --check --file brief.json        # 缺项会列出来；别名可以为空但要有理由
python research.py brief --matrix --file brief.json --facet 子问题1 --facet 子问题2 --budget 8
```

矩阵只是候选：别名未必都可信、口语/英文未必都有，挑哪些、先跑哪几条由你判断。

```bash
python research.py init "调研问题" --state research.json \
  --purpose "为什么要查" --deliverable "交付物要求" --channels web --json

python research.py set-node --state research.json --node n0 \
  --queries "查询一" "查询二" --counter-queries "反证查询" --json

python research.py run --state research.json --ready --fetch 3 --json   # 默认返回节点摘要（约 2KB）
python research.py run --state research.json --ready --fetch 3 --json --full   # 需要完整状态时
python research.py show --state research.json --node n0 --json
python research.py apply-review --state research.json --file review.json --json
python research.py claim add --state research.json --node n0 \
  --text "结论" --evidence E1,E2 --support dual --json
python research.py digest --state research.json --json
```

### 多条查询怎么写

`--queries` 接受空格分隔的多个值，也接受重复写多次（会合并）：

```bash
python research.py set-node --state research.json --node n1 --queries "查询一" "查询二"
python research.py set-node --state research.json --node n1 --queries "查询三" --queries "查询四"
```

不要把多条查询塞进一个引号里（`--queries "A\nB"` 会被当成**一条**含换行的查询）。

### 层数与 --fetch 的口径

- 层数：根节点 `depth=0` 算第 1 层，`depth=1` 算第 2 层，依此类推。
- `--fetch N`：每个节点每次 `run` 最多抓 N 条正文（按标题相关度排序后取前 N），不是整个调研的总数。
- `--version-date` / `--version-label`：研究基准日与批次名，用来判断信息时效，不是标准发布日或实施日。

### 层数规则（重要）

`init --max-depth 3`（默认）意味着最多 4 层，而工具要求**至少 4 层**才能收口。不要把 `--max-depth` 设成小于 3，否则永远不达标；确实要提前收口时用 `apply-review --allow-early-close` 并在 review 里写 `early_close_reason`。

## review.json 怎么写

字段契约以 `spec --json` 的 `review_schema` 为准。最小可用：

```json
{
  "node_id": "n0",
  "verdict": "answered",
  "result": "一句话结论 [E1][E2]",
  "rationale": "为什么这些证据足够",
  "citations": ["E1", "E2"]
}
```

- `verdict` 只有三种：`answered` / `partial` / `not_found`。
- `partial` 必须写 `unresolved`（还缺什么）。
- 未到最小层数时，`partial` / `not_found` 必须给 `children`（1–3 个），每个子问题要能独立执行：

```json
{
  "node_id": "n1",
  "verdict": "partial",
  "result": "已知部分",
  "unresolved": ["还缺的具体事实"],
  "children": [
    {
      "question": "自包含的子问题，不依赖上下文也能搜",
      "scope_delta": "这个子问题补的是哪块空白",
      "acceptance": ["什么样算答到了"]
    }
  ]
}
```

`spec --json` 的 `accepted_payload_forms` 还支持一次提交多条（数组 / `{"reviews": [...]}` / 按节点 id 做键）。

## 采集失败怎么处理

`run` 的退出码：

- `0`：成功。部分条目抓取失败但有可用正文时，结果里会出现 `warnings`（不阻断），照常进入判卷。
- `4`：节点没有任何可用证据，或查询全部失败。看 `errors`，修渠道/配置/网络后 `reset` 再重跑。
- `5`：超时。缩小 `--fetch` 或延长 `--timeout`。

单条抓取失败不要重试超过两次；换渠道（`--channels web` 或 `browser`）或换来源更划算。

## 正文抽取通道（Open Notebook）

`fetch_smart.py` 在 HTTP / Tavily / 无头浏览器之外，可调用本机 Open Notebook 做正文抽取（同一篇搜狐文章 3,220 字 → 121,058 字）。

- 启用条件：`.env` 里 `OPEN_NOTEBOOK_URL=http://127.0.0.1:5055`；未设置时完全跳过。
- 强制使用：`python fetch_smart.py "<url>" --route notebook --json`。
- 调研内切换：`FETCH_SMART_ROUTE=notebook python research.py run ...`。
- 服务依赖：Docker 里的 `open_notebook` + `surrealdb`，compose 在 `scripts/open_notebook/docker-compose.yml`。服务停了工具照常工作，回落原链路；`doctor` 会给出 `OPEN_NOTEBOOK_URL` 检查项。

## 登录态渠道

`browser` 渠道依赖用户 Chrome 正在运行且已登录目标站点，工具不会替人登录。先跑 `doctor` 看 `chrome_running` 与 `opencli_session`。登录态抓取失败不要连续重试三次，直接换 `--channels web` 或换平台。

### 滚动（默认关）

首屏之外的 DOM 不存在，`innerText` 只拿得到已渲染部分。需要长列表时：

```bash
python fetch_smart.py "<url>" --route logged-in --scroll 4 --json
python research.py run --state tree.json --node n1 --fetch 3 --scroll 4
python research.py set-node --state tree.json --node n1 --scroll 3
```

- 范围 0–10，默认 0；结果带 `scroll: {screens, stopped, chars_before, chars_after}`。
- `research.py status` 还会给出 `pdf_candidates`（未审节点里还没抓到正文的 `.pdf` 链接数）。PDF 常只有编号式标题、排序靠后，容易被漏；看到这个数就先 `show` 找出来，用 `fetch_smart.py "<pdf-url>"` 单抓。
- 抓取结果还带 `expand_hints`（阅读全文/加载更多/展开更多/查看更多 各出现几次）；`research.py status` 会把「有展开提示但没滚过」的节点列进 `scroll_candidates` 并给建议命令。
- 脚本不自动判定该不该滚，由调用的模型决定：有提示且条目不全 → 滚 3–5 屏；滚完字数涨幅很小 → 到底了，改抓详情页。
- 站点把未登录页重定向走时（知乎问题页会回首页），拿到的是首页推荐流，别当成正文。

## 判卷口径（模型判断，不是脚本规则）

- `answered`：关键数字/事实至少有 2 个独立来源；单一来源只能算 `partial`，并在 `unresolved` 写明缺什么。
- `partial`：部分答到，必须写 `unresolved`；未到最小层数时给 children。
- `not_found`：没查到，给 1–3 个可执行子问题，不要空手收口。
- 来源互相矛盾：写进结论并标注分歧，不替来源选边。
- 时间敏感的事实必须带日期锚点。

## 常用辅助命令

| 命令 | 用途 |
|---|---|
| `status --json` | 层数、待拆节点、待审节点、`scroll_candidates`、下一步命令 |
| `prompt --node n1` | 生成可直接派给子 agent 的任务书 |
| `show --node n1 --chars 3000` | 看某个节点的证据与正文 |
| `report` / `digest` | 树状报告 / 七段式汇总 |
| `claim add/list/remove` | 结论账本，`--support single\|dual` |
| `reset --node n1` | 重开节点重采 |
| `clean --state x.json` | 清理上一轮遗留的 `*.lock`（默认只删 10 分钟前的） |

`run` 的 `--json` 默认只给节点摘要（id/status/depth/证据数/正文数/抓取失败数 + channels_used + layers + pending_*），避免几万 token 灌进上下文；要完整状态加 `--full`。`show` 用 `--chars N` 控制单条正文长度。后台跑完每次都会留一批 `*.lock`，收尾时跑一次 `clean --state`。

任何一步的输出都是 JSON 信封 `{ok, command, data|error}`；`ok:false` 时看 `error.message`、`error.hint`、`error.next_command`。
