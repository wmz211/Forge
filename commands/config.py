"""
/config [key [value]] — View or set session configuration.
Mirrors Claude Code's /config command (aliases: /settings).

Without args: show all current settings.
With key only: show that setting's value.
With key + value: update the setting for this session.

Writable settings: model, mode, max_turns, cwd
"""
from __future__ import annotations
import commands as _reg
from permissions import PERMISSION_MODES


_SETTINGS_META = {
    "model":     "Active AI model",
    "mode":      "Permission mode: " + " | ".join(PERMISSION_MODES),
    "max_turns": "Max agent turns before stopping",
    "cwd":       "Working directory (read-only; use OS commands to change)",
}


def _get(engine, key: str):
    return {
        "model":     engine._api.model,
        "mode":      engine.permission_mode,
        "max_turns": engine.max_turns,
        "cwd":       engine.cwd,
    }.get(key)


def _set(engine, key: str, value: str) -> str | None:
    if key == "model":
        engine._api.model = value
        return None
    if key == "mode":
        if value not in PERMISSION_MODES:
            return f"Invalid mode '{value}'. Choose: {', '.join(PERMISSION_MODES)}"
        engine.permission_mode = value
        return None
    if key == "max_turns":
        try:
            engine.max_turns = int(value)
            return None
        except ValueError:
            return f"max_turns must be an integer, got: {value!r}"
    if key == "cwd":
        return "cwd is read-only."
    return f"Unknown setting: {key!r}"


async def call(args: str, engine) -> str:
    parts = args.strip().split(None, 1)

    if not parts:
        # Show all settings
        lines = ["\033[1mCurrent configuration:\033[0m\n"]
        for key, desc in _SETTINGS_META.items():
            val = _get(engine, key)
            lines.append(f"  \033[36m{key:<12}\033[0m = \033[1m{val}\033[0m")
            lines.append(f"               \033[90m{desc}\033[0m")
        lines.append("\n  Usage: /config <key> [value]")
        return "\n".join(lines)

    key = parts[0]
    if key not in _SETTINGS_META:
        return (
            f"  \033[31mUnknown setting:\033[0m {key!r}\n"
            f"  Available: {', '.join(_SETTINGS_META)}"
        )

    if len(parts) == 1:
        # Read single setting
        return f"  {key} = \033[1m{_get(engine, key)}\033[0m\n  \033[90m{_SETTINGS_META[key]}\033[0m"

    err = _set(engine, key, parts[1])
    if err:
        return f"  \033[31mError:\033[0m {err}"
    return f"  \033[32mSet\033[0m {key} = \033[1m{_get(engine, key)}\033[0m"


_reg.register({
    "name": "config",
    "description": "View or set session configuration",
    "aliases": ["settings"],
    "argument_hint": "[key [value]]",
    "call": call,
})
