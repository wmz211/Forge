"""
Command registry — mirrors Claude Code's commands.ts.

Each command is a dict with:
  name        str          primary command name
  description str          shown in /help
  aliases     list[str]    alternative names (optional)
  argument_hint str        shown after name in /help (optional)
  call        callable     async fn(args: str, engine) -> str | None
"""
from __future__ import annotations
from typing import Callable, Awaitable

# Registry: name → command dict
_REGISTRY: dict[str, dict] = {}
_ALIAS_MAP: dict[str, str] = {}   # alias → canonical name


def register(cmd: dict) -> None:
    name = cmd["name"]
    _REGISTRY[name] = cmd
    for alias in cmd.get("aliases", []):
        _ALIAS_MAP[alias] = name


def get(name: str) -> dict | None:
    if name in _REGISTRY:
        return _REGISTRY[name]
    canonical = _ALIAS_MAP.get(name)
    if canonical:
        return _REGISTRY.get(canonical)
    return None


def all_commands() -> list[dict]:
    return list(_REGISTRY.values())


# ── Auto-import all command modules to trigger register() calls ───────────────
from commands import (  # noqa: E402, F401
    help,
    compact,
    cost,
    context,
    status,
    model,
    resume,
    add_dir,
    permissions_cmd,
    memory,
    diff,
    config,
    init,
    clear,
    session,
    commit,
    files,
    doctor,
)
