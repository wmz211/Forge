# Forge

Forge is an experimental Python coding agent inspired by Claude Code. It uses a Qwen/OpenAI-compatible chat API, a ReAct-style tool loop, local permissions, slash commands, session transcripts, and a growing set of coding tools.

This repository is usable as an alpha prototype, but it is not a complete Claude Code reimplementation yet. The fidelity work is ongoing and tracked in `PROGRESS.md`.

## Current Features

- Interactive CLI entry point in `main.py`
- Qwen/OpenAI-compatible streaming API client
- File, shell, grep/glob, web, PowerShell, TodoWrite, ToolSearch, and sub-agent tools
- Workspace-aware permission checks with `default`, `acceptEdits`, `plan`, and `bypassPermissions` modes
- JSONL session transcript storage and resume-oriented command scaffolding
- Focused fidelity tests under `tests/`

## Quick Start

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:FORGE_API_KEY = "your-api-key"
python main.py --cwd .
```

Forge also reads `FORGE_MODEL`; when unset, it defaults to `qwen3-coder-plus`.

For compatibility with earlier local builds, `CODING_AGENT_API_KEY` and `CODING_AGENT_MODEL` are still accepted as fallbacks.

## Tests

```powershell
python -m unittest discover -s tests -v
```

## Status

Forge can be published and iterated publicly, but treat it as an early alpha. The most important remaining fidelity areas are deeper Claude Code parity for tool orchestration, permission UX, IDE integration, shell semantics, hook/plugin behavior, MCP support, and richer recovery paths.
