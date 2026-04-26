# Forge

Python reimplementation of Claude Code (Claude Code v2.1.88 architecture) using the Qwen API.

## Tech Stack
- **Language**: Python 3.10+
- **API**: Qwen (dashscope OpenAI-compatible endpoint), model `qwen3-coder-plus`
- **Key libs**: `openai` (async client), `prompt_toolkit` (REPL), `rich` (terminal rendering)

## Architecture

| File | Role |
|------|------|
| `main.py` | CLI entry point, REPL loop |
| `query.py` | Core ReAct agent loop (`query_loop`) |
| `query_engine.py` | Session management, persistence (JSONL) |
| `tool.py` | Tool base class + ToolContext |
| `permissions.py` | Permission decision engine |
| `permission_rules.py` | Rule loading from settings files |
| `ui.py` | Terminal rendering (spinner, markdown, tool display) |
| `server.py` | FastAPI HTTP server mode |

## Key Directories
- `tools/` — individual tool implementations (Bash, Read, Edit, Write, Glob, Grep, Agent, WebFetch, WebSearch, AskUserQuestion, Sleep, NotebookEdit, TaskCreate/Update/Get/List/Stop/Output)
- `commands/` — slash commands registered via `commands/__init__.py`
- `services/` — API client (`api.py`) and compaction (`compact/`)
- `utils/` — tokens, messages, file state cache, memory loading, tool result budget, hooks

## Memory (mirrors attachments.ts / memdir/)
`utils/memory.py` discovers CLAUDE.md files in full load order:
1. `~/.claude/CLAUDE.md` (global)
2. Ancestor dirs up to home
3. `<cwd>/CLAUDE.md` (project root)
4. `<cwd>/.claude/CLAUDE.md` (local)
5. Subdirs `<cwd>/**/.claude/CLAUDE.md` depth ≤ 3
Deduplication by resolved path. `get_memory_file_entries(cwd)` returns entries for `/memory show`.

## Build & Run
```bash
pip install -r requirements.txt
export FORGE_API_KEY=<your-dashscope-key>
python main.py [--cwd PATH] [--mode default|plan|acceptEdits|bypassPermissions]
```

## Compaction Pipeline (5 defenses, mirrors query.ts)
1. `apply_tool_result_budget` — truncate oversized tool results
2. `snip_compact_if_needed` — remove middle messages
3. `microcompact_messages` — time-based tool-result clearing
4. applyCollapsesIfNeeded — not yet implemented (CONTEXT_COLLAPSE feature-gated)
5. `compact` — full LLM summarisation (with PreCompact/PostCompact hooks)

## Session Storage (mirrors sessionStorage.ts)
JSONL format; path: `~/.claude/projects/<sanitized-cwd>/<session-id>.jsonl`
- `{type: "summary"}` — header written on new session creation (for fast listing)
- `{type: "message"}` — one entry per user/assistant/tool message
- `{type: "heartbeat"}` — written at the start of each API turn (signals liveness)
- `archive_session()` — moves JSONL to `archived/` subdirectory
- `get_sessions(filter)` — returns session metadata list with optional cwd/model/since/limit filtering
- `/session list [--cwd] [--model] [--since 7d]` — filtered listing
- `/session archive <id-prefix>` — archive a session

## Hook System (mirrors hooks.ts)
Hooks loaded from `.claude/settings.json` (project) and `~/.claude/settings.json` (user).
- `PreCompact` / `PostCompact` — wired in `services/compact/compact.py`
- `PreToolUse` / `PostToolUse` — wired in `query.py` around each tool call
- `Stop` — wired in `query.py` at "no tool calls" point; exit code 2 re-injects feedback and continues loop
- `UserPromptSubmit` — wired in `query_engine.py` before processing; exit code 2 blocks prompt
- `SessionStart` — fired in `main.py` on startup/resume

## Code Style
- All public functions have docstrings explaining what they mirror in the TypeScript source
- Mirrors comments reference the exact source file and function name
- No unnecessary comments — only "WHY" comments for non-obvious logic
