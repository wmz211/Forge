# Forge 开发进度

> 参考 Claude Code v2.1.88 架构，使用 Qwen API 复现核心功能
> 模型：qwen3-coder-plus | API：dashscope compatible mode
> 

---

## 已实现功能

### 斜杠命令系统

- [x] **命令注册框架**（`commands/__init__.py`）
  - `register()` / `get()` / `all_commands()` — 镜像 `commands.ts` 的 Command 注册表
  - 别名（aliases）支持、argument_hint 展示

- [x] **`/help`**（`commands/help.py`）— 列出所有注册命令和描述，别名 `/?`
- [x] **`/compact [instructions]`**（`commands/compact.py`）— 手动触发上下文压缩，支持自定义摘要指令
- [x] **`/cost`**（`commands/cost.py`）— 显示本会话 token 用量和估算费用（Qwen3 定价）
- [x] **`/context`**（`commands/context.py`）— 彩色进度条显示 context window 占用，别名 `/ctx`
- [x] **`/status`**（`commands/status.py`）— 显示 model、cwd、session ID、token 用量
- [x] **`/model [name]`**（`commands/model.py`）— 查看或切换模型，列出可用 Qwen 模型
- [x] **`/resume [id|search]`**（`commands/resume.py`）— 列出或恢复历史会话，别名 `/continue`
- [x] **`/add-dir <path>`**（`commands/add_dir.py`）— 添加额外工作目录到会话作用域
- [x] **`/permissions [allow|deny|clear|mode]`**（`commands/permissions_cmd.py`）— 查看/修改权限规则
- [x] **`/memory [edit]`**（`commands/memory.py`）— 查看/编辑 CLAUDE.md 记忆文件，支持 `$EDITOR`
- [x] **`/diff [path]`**（`commands/diff.py`）— 显示 git 未提交变更，彩色 diff 输出
- [x] **`/config [key [value]]`**（`commands/config.py`）— 查看/修改会话配置，别名 `/settings`
- [x] **`/init`**（`commands/init.py`）— 用 LLM 生成 CLAUDE.md 项目说明文件，写入后重载 memory
- [x] **`/clear`**（`commands/clear.py`）— 清除对话历史（正式命令），别名 `/reset`；清除后重注入 CLAUDE.md
- [x] **`/session`**（`commands/session.py`）— 显示当前会话信息 + 列出最近 10 个历史会话，别名 `/sessions`
- [x] **`/commit`**（`commands/commit.py`）— 收集 git 上下文后注入 prompt，让 agent 创建 git commit；镜像 `commands/commit.ts`
- [x] **`/files`**（`commands/files.py`）— 列出 FileStateCache 中所有已读文件（agent 当前 context 中的文件）；镜像 `commands/files/files.ts`
- [x] **`/doctor`**（`commands/doctor.py`）— 诊断 Forge 安装状态（Python 版本、API key、依赖包、git、存储路径）；镜像 `commands/doctor/index.ts`

### 核心架构

- [x] **Qwen API 客户端**（`services/api.py`）
  - 流式输出，工具调用 chunk 累积，OpenAI 兼容格式
  - `stream_options: {include_usage: true}` — 捕获真实 token 用量
  - `done` 事件携带 `usage: {prompt_tokens, completion_tokens}`
  - **Qwen3 Thinking blocks** — `enable_thinking` 参数，`reasoning_content` delta 处理
  - 镜像 `src/services/api/claude.ts`

- [x] **上下文压缩**（`services/compact.py`）
  - 精确复现 `src/services/compact/autoCompact.ts` 的常量与阈值函数
    - `AUTOCOMPACT_BUFFER_TOKENS = 13_000`
    - `WARNING_THRESHOLD_BUFFER_TOKENS = 20_000`
    - `MANUAL_COMPACT_BUFFER_TOKENS = 3_000`
    - `MAX_OUTPUT_TOKENS_FOR_SUMMARY = 20_000`
  - `calculate_token_warning_state()` 与源码完全一致
  - `token_count_with_estimation()` — 优先使用 API 返回的真实 token 数
  - 完整复现 `src/services/compact/prompt.ts`
    - 9 节摘要结构、`<analysis>` scratchpad、`<summary>` 包裹
    - `format_compact_summary()` 剥离 analysis，展开 summary
    - `get_compact_user_summary_message()` 续接指令

- [x] **权限系统**（`permissions.py`）
  - 精确复现 `src/types/permissions.ts` 的类型体系
  - `PermissionBehavior: 'allow' | 'deny' | 'ask'`
  - `PermissionResult: {behavior, message?, decisionReason?}`
  - 四种模式（`EXTERNAL_PERMISSION_MODES`）：
    - `default` — 危险工具提示确认
    - `plan` — 只读工具静默放行，写操作询问
    - `acceptEdits` — 读+编辑静默，bash/agent 询问
    - `bypassPermissions` — 全部静默放行
  - `check_permission()` 镜像 `canUseTool()` 决策逻辑
  - CLI 交互确认对话（镜像终端权限弹窗）

- [x] **会话持久化**（`query_engine.py`）
  - 精确复现 `src/utils/sessionStorage.ts` 的存储格式
  - **JSONL 格式**（逐行追加，append-only）
  - **路径**：`~/.claude/projects/<sanitized-cwd>/<session-id>.jsonl`
  - `_sanitize_path()` 镜像 `sanitizePath()`（替换路径分隔符为 `-`）
  - `_get_transcript_path()` 镜像 `getTranscriptPath()`
  - `_append_entry()` 镜像 `appendEntryToFile()`
  - 支持 `--session-id UUID` 恢复历史会话

- [x] **主循环 query_loop**（`query.py`）
  - 精确复现 `src/query.ts` 的 `queryLoop()`
  - 接受 `last_usage` — 传入上轮真实 token 数，供压缩阈值决策使用
  - 发射 `assistant_message` / `tool_result_message` 内部事件供引擎持久化
  - `done` 事件携带 `usage` 供 QueryEngine 缓存
  - **Max output tokens 恢复** — `stop_reason="length"` 时注入恢复 prompt，最多重试 3 次（`MAX_OUTPUT_TOKENS_RECOVERY_LIMIT`），镜像 `query.ts` recovery path
  - **压缩防线① `apply_tool_result_budget`** — 每轮前执行，超大 tool result 截断到 10k tokens

- [x] **CLAUDE.md 记忆加载**（`utils/memory.py`）
  - 镜像 `src/utils/attachments.ts` + `src/memdir/` 层级发现逻辑
  - 路径顺序：`~/.claude/CLAUDE.md` → 父目录 `.claude/CLAUDE.md` → `cwd/.claude/CLAUDE.md` → 子目录
  - `inject_memory_into_system_prompt()` — 将所有 CLAUDE.md 内容包在 `<memory>` 块注入 system prompt
  - `/clear` 后自动重注入，保证记忆不随对话清除而丢失

- [x] **Tool 基类 + ToolContext**（`tool.py`）— 统一接口，OpenAI schema 生成

- [x] **工具并发调度**（`query.py` `_partition_tool_calls`）
  - 只读工具并发（asyncio.gather），写入工具串行
  - 镜像 `src/services/tools/toolOrchestration.ts`

- [x] **QueryEngine**（`query_engine.py`）
  - 会话管理，跨轮消息历史
  - 缓存 `last_usage` 供压缩决策
  - `clear()` 截断 JSONL 文件

- [x] **CLI 入口**（`main.py`）
  - 交互式终端，ANSI 彩色输出
  - 斜杠命令：`/clear /cwd /mode /session /exit`
  - `--session-id` 参数恢复历史会话
  - `--mode` 使用正确的源码模式名

### 终端 UI（`ui.py`）

- [x] **`ui.py`** — Claude Code 风格终端渲染（镜像 React/Ink 视觉效果）
  - `BLACK_CIRCLE = '⏺'`（macOS）/ `'●'`（其他）— 镜像 `figures.ts`
  - `TOOL_INDENT = '⎿'` — 工具调用缩进字符
  - `Spinner` — 后台线程驱动的 `⠋⠙⠹…` 等待动画（"Thinking…"）
  - `render_assistant_bullet()` — 每条 assistant 消息前的 `●` 前缀
  - `render_text(text)` — `rich.Markdown` 渲染 assistant 文本（镜像 `<Markdown>` 组件）
  - `render_tool_use(name, args)` — `⎿ ToolName(k='v', …)` 蓝色工具调用头
  - `render_tool_result(name, result, duration_ms)` — 暗灰色结果预览 + 耗时
  - `EventRenderer` — 状态机，统一处理所有 `query_loop` 事件类型
    - 首个 token 到达时停止 spinner、打印 `●`
    - 工具调用完成后重启 spinner（模型继续工作中）
    - **Thinking block 渲染** — `💭 <dim italic 前120字>` 展示推理过程

### 工具实现

- [x] **BashTool** — 执行 shell 命令，超时控制，输出截断，跨平台
- [x] **FileReadTool** — 带行号读取，offset/limit 分段，100K chars 上限
  - 设备文件黑名单（`/dev/zero` 等），二进制扩展名拦截
  - **Jupyter notebook 读取**（`.ipynb`）— JSON 解析，XML 标签单元格格式，输出截断（>10K 用 `jq` 提示），镜像 `src/utils/notebook.ts`
  - **图片元数据读取**（`.png/.jpg/.jpeg/.gif/.webp`）— 用 Pillow 提取尺寸/格式/色彩模式，镜像 `imageProcessor.ts`（无法在终端渲染原始图片）
  - 必须先读后编辑（`FileStateCache` 强制），文件 mtime 校验
- [x] **FileEditTool** — 精确字符串替换（唯一性校验）
- [x] **FileWriteTool** — 创建/覆写文件，自动创建父目录
- [x] **GlobTool** — 递归文件模式匹配，按修改时间排序
- [x] **GrepTool** — 正则内容搜索
  - `-A/-B/-C/context` 上下文行，`type` 文件类型过滤，`multiline` 模式
  - ripgrep 优先，Python `re` 降级兜底，`head_limit`/`offset` 分页
- [x] **WebFetchTool** — Jina Reader 转 Markdown，**超大页面（>50K chars）自动调用 Qwen 摘要**，镜像 `WebFetchTool.ts` Haiku 摘要行为
- [x] **AgentTool** — 子 Agent（递归 query_loop），工具权限隔离

---

## 架构对照表

| 功能 | Claude Code 文件 | Forge 文件 |
|------|----------------|----------------|
| 主循环 | `src/query.ts` `queryLoop()` | `query.py` `query_loop()` |
| 会话管理 | `src/QueryEngine.ts` | `query_engine.py` `QueryEngine` |
| 工具调度 | `src/services/tools/toolOrchestration.ts` | `query.py` `_partition_tool_calls()` |
| 上下文压缩常量 | `src/services/compact/autoCompact.ts` | `services/compact.py` |
| 压缩 prompt | `src/services/compact/prompt.ts` | `services/compact.py` |
| 权限类型 | `src/types/permissions.ts` | `permissions.py` |
| 权限决策 | `src/utils/permissions/permissions.ts` | `permissions.py` `check_permission()` |
| 会话存储格式 | `src/utils/sessionStorage.ts` | `query_engine.py` JSONL helpers |
| Token 计数 | `src/utils/tokens.ts` | `services/compact.py` `token_count_with_estimation()` |
| API 客户端 | `src/services/api/claude.ts` | `services/api.py` |
| Bash 执行 | `src/tools/BashTool/` | `tools/bash_tool.py` |
| 文件读取 | `src/tools/FileReadTool/` | `tools/file_read_tool.py` |
| Notebook 解析 | `src/utils/notebook.ts` | `tools/file_read_tool.py` `_read_notebook()` |
| 图片元数据 | `src/tools/FileReadTool/imageProcessor.ts` | `tools/file_read_tool.py` `_read_image_metadata()` |
| 大页摘要 | `src/tools/WebFetchTool/` | `tools/web_fetch_tool.py` `_summarize_content()` |
| 文件编辑 | `src/tools/FileEditTool/` | `tools/file_edit_tool.py` |
| 文件写入 | `src/tools/FileWriteTool/` | `tools/file_write_tool.py` |
| 文件搜索 | `src/tools/GlobTool/` | `tools/glob_tool.py` |
| 内容搜索 | `src/tools/GrepTool/` | `tools/grep_tool.py` |
| 子 Agent | `src/tools/AgentTool/` | `tools/agent_tool.py` |
| 终端 UI | `components/` `AssistantTextMessage.tsx` `figures.ts` | `ui.py` `EventRenderer` |
| CLAUDE.md 加载 | `src/utils/attachments.ts` `src/memdir/` | `utils/memory.py` |
| Tool result 截断 | `src/utils/toolResultStorage.ts` `applyToolResultBudget()` | `utils/tool_result_budget.py` |
| Max output tokens 恢复 | `src/query.ts` recovery path | `query.py` recovery block |
| /commit 命令 | `src/commands/commit.ts` | `commands/commit.py` |
| /files 命令 | `src/commands/files/files.ts` | `commands/files.py` |
| /doctor 命令 | `src/commands/doctor/index.ts` | `commands/doctor.py` |

---

## 待实现功能

- [ ] **WebSearchTool** — 网络搜索（需要搜索 API）
- [ ] **Swarm 多 Agent** — 多 Claude 实例并发 + mailbox 通信
- [ ] **MCP 协议支持** — Model Context Protocol 工具服务器
- [ ] **增量文件快照** — `src/utils/fileHistory.ts` `fileStateCache`
- [ ] **Token 精确计数** — 可接入 tiktoken 替代 4 chars/token 估算
- [ ] **Prompt caching** — Qwen 是否支持待验证
- [x] **Extended thinking** — Qwen3 thinking blocks，`enable_thinking` + `reasoning_content` 渲染
- [ ] **工具调用摘要** — `generateToolUseSummary` 每批工具后用 Haiku 生成简短摘要（gated by `config.gates.emitToolUseSummaries`，低优先级）
- [ ] **Microcompact（防线③）** — 源码已改为 cached MC（Anthropic cache editing API）或时间触发（默认禁用），与 Qwen 无关，暂不实现
- [ ] **Context collapses（防线④）** — gated by `feature('CONTEXT_COLLAPSE')`，暂不实现
- [ ] **Token 精确计数** — 当前使用 4 chars/token 估算；接入 tiktoken 可提高精度
- [ ] **MCP 协议支持** — 完全缺失，高工作量
- [ ] **Prompt caching** — Qwen 是否支持待验证
- [ ] **IDE 集成** — VS Code 扩展

---

## 快速开始

```bash
cd Forge
pip install -r requirements.txt

# 基本使用（default 模式，危险工具需确认）
python main.py

# 指定工作目录
python main.py --cwd /path/to/project

# plan 模式（只读静默，写操作询问）
python main.py --mode plan

# 无限制模式（全部工具静默）
python main.py --mode bypassPermissions

# 恢复历史会话（启动时显示的 session UUID）
python main.py --session-id xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

会话 JSONL 自动保存到：
```
~/.claude/projects/<sanitized-cwd>/<session-id>.jsonl
```
# Forge fidelity progress

This project is a Python/Qwen reimplementation of selected Claude Code
behaviors. It is not yet a faithful full clone of Claude Code. Treat each
"done" item below as "implemented in this runtime" only after it has matching
source semantics, tests or behavior checks, and documented gaps.

## 2026-04-24 fidelity pass

### Completed in this pass
- Extended `Tool` / `ToolContext` with Claude Code-like semantic slots:
  `validate_input`, `is_read_only`, `is_destructive`, `interrupt_behavior`,
  `max_result_size_chars`, `should_defer`, `always_load`, aliases, and
  additional working directories.
- Tool execution now runs tool-level validation before permission prompting.
- File permission checks now receive `cwd` and additional working directories.
- Read/Edit/Write permission behavior now distinguishes workspace paths,
  outside-workspace paths, suspicious Windows paths, and sensitive write paths
  such as `.git`, `.claude`, `.ssh`, `.aws`, and `.config`.
- `/add-dir` scope is now connected to tool permission evaluation.
- `Read` can read bounded windows from files larger than the default size cap
  when `offset` or `limit` is provided.
- `Read` has best-effort PDF text extraction when `pypdf` or `PyPDF2` is
  available, instead of claiming PDF support while returning a hardcoded TODO.
- Added a first-pass `PowerShell` tool and registered it in the default tool
  pool, so Windows shell semantics are no longer forced through `Bash`.
- Added `TodoWrite`, including schema validation, at-most-one `in_progress`
  validation, per-session todo state, all-completed clearing behavior, and the
  source-compatible success message.
- Moved builtin tool construction behind `tools.build_builtin_tools()` so later
  deferred-tool and ToolSearch work has a cleaner registration point.
- Added a first-pass `ToolSearch` tool with `select:<tool>` and keyword search
  over deferred tools. `WebFetch`, `WebSearch`, and `TodoWrite` are now marked
  deferred; the model-facing tool schema list contains core tools plus
  `ToolSearch`, while the runtime pool can still execute all tools.
- Added a lightweight `unittest` fidelity suite covering permission decisions,
  sensitive write paths, additional working directories, large-file windowed
  reads, TodoWrite validation/state clearing, and ToolSearch deferred visibility.
- Python syntax check passed for the changed core files.

### Still not faithful
- The original Claude Code `ToolUseContext` is much richer: hooks, MCP,
  app state, notifications, file history, streaming tool execution, content
  replacement state, and query/source metadata are still missing or partial.
- Permission behavior is still an approximation. Missing pieces include full
  filesystem policy logic, hook decisions, auto/classifier mode, sandbox
  overrides, denial tracking, enterprise policy/MDM, and path rule matching.
- `PowerShell` lacks Claude Code's full CLM/security validation, sandbox
  integration, git safety, background task support, and detailed permission
  suggestion flow.
- `Read` image handling still returns metadata only. Claude Code can pass image
  content through its richer processing path.
- `query_loop` still lacks faithful microcompact, context collapse, stop hooks,
  post-sampling hooks, streaming tool executor, task budget, and tool-use
  summary behavior.
- `ToolSearch` returns text/list output instead of Anthropic `tool_reference`
  blocks because the current Qwen/OpenAI-compatible API path does not expand
  `tool_reference` blocks.

### Next fidelity targets
1. Port filesystem permission matching more directly from
   `src/utils/permissions/filesystem.ts`.
2. Replace the current shell command splitting approximations with a parser
   closer to the source Bash/PowerShell command semantics.
3. Extend the fidelity test suite to cover Read-before-Edit, Windows PowerShell
   command handling, and Agent sidechain behavior.
4. Wire todo state into the visible UI/status layer instead of only returning
   tool-result text.
5. Add NotebookEdit or MCP primitives next, depending on whether local coding
   fidelity or ecosystem fidelity is the priority.

---
