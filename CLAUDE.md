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
- `tools/` — individual tool implementations (Bash, Read, Edit, Write, Glob, Grep, Agent, WebFetch, WebSearch)
- `commands/` — slash commands registered via `commands/__init__.py`
- `services/` — API client (`api.py`) and compaction (`compact/`)
- `utils/` — tokens, messages, file state cache, memory loading, tool result budget

## Build & Run
```bash
pip install -r requirements.txt
export CODING_AGENT_API_KEY=<your-dashscope-key>
python main.py [--cwd PATH] [--mode default|plan|acceptEdits|bypassPermissions]
```

## Compaction Pipeline (5 defenses, mirrors query.ts)
1. `apply_tool_result_budget` — truncate oversized tool results
2. `snip_compact_if_needed` — remove middle messages
3. microcompact — not yet implemented
4. applyCollapsesIfNeeded — not yet implemented
5. `compact` — full LLM summarisation

## Code Style
- All public functions have docstrings explaining what they mirror in the TypeScript source
- Mirrors comments reference the exact source file and function name
- No unnecessary comments — only "WHY" comments for non-obvious logic
