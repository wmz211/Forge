from __future__ import annotations
"""
Hook system — execute user-defined shell commands at lifecycle points.
Mirrors src/utils/hooks.ts.

Hooks are loaded from .claude/settings.json (project) and ~/.claude/settings.json (user).
Format:
  {
    "hooks": {
      "PreCompact":   [{"matcher": "auto", "hooks": [{"type": "command", "command": "..."}]}],
      "PostCompact":  [{"matcher": "auto", "hooks": [{"type": "command", "command": "..."}]}],
      "SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": "..."}]}],
      "PreToolUse":   [{"matcher": "Bash",  "hooks": [{"type": "command", "command": "..."}]}],
      "PostToolUse":  [{"matcher": "Bash",  "hooks": [{"type": "command", "command": "..."}]}]
    }
  }

Hook events implemented (command-type only):
  - PreCompact     : fires before compaction; stdout → newCustomInstructions
  - PostCompact    : fires after compaction
  - SessionStart   : fires on startup/resume/clear/compact
  - PreToolUse     : fires before each tool call
  - PostToolUse    : fires after each tool call
"""

import asyncio
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Literal

# ── Hook events implemented ────────────────────────────────────────────────────
HOOK_EVENTS = frozenset({
    "PreCompact",
    "PostCompact",
    "SessionStart",
    "PreToolUse",
    "PostToolUse",
    "Stop",
    "Notification",
    "SessionEnd",
})

# Settings file paths (mirrors _settingsPathForSource in settings.ts)
_SETTINGS_SOURCES = [
    ("user",    lambda cwd: Path.home() / ".claude" / "settings.json"),
    ("project", lambda cwd: Path(cwd) / ".claude" / "settings.json"),
    ("local",   lambda cwd: Path(cwd) / ".claude" / "settings.local.json"),
]

# ── Settings loading ───────────────────────────────────────────────────────────

def _read_json_safe(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _load_hooks_config(cwd: str) -> dict[str, list[dict]]:
    """
    Merge hooks from all settings sources (user → project → local).
    Later sources extend (not override) earlier sources for each event.
    Mirrors getHooksConfigFromSnapshot() behaviour.
    """
    merged: dict[str, list[dict]] = {}
    for _source_name, path_fn in _SETTINGS_SOURCES:
        path = path_fn(cwd)
        data = _read_json_safe(path)
        hooks_section = data.get("hooks")
        if not isinstance(hooks_section, dict):
            continue
        for event, matchers in hooks_section.items():
            if not isinstance(matchers, list):
                continue
            merged.setdefault(event, []).extend(matchers)
    return merged


# ── Pattern matching ───────────────────────────────────────────────────────────

def _matches_pattern(match_query: str, pattern: str) -> bool:
    """
    Mirrors matchesPattern() in hooks.ts.
    - No pattern / "*" → always matches
    - Alphanumeric with optional "|" separators → exact match (pipe-separated OR)
    - Otherwise → regex match
    """
    if not pattern or pattern == "*":
        return True
    # Simple alphanumeric (possibly pipe-separated) → exact match
    if re.fullmatch(r"[a-zA-Z0-9_|]+", pattern):
        parts = [p.strip() for p in pattern.split("|")]
        return match_query in parts
    # Regex match
    try:
        return bool(re.search(pattern, match_query))
    except re.error:
        return False


# ── Hook execution (command-type only) ────────────────────────────────────────

def _execute_hook_command(
    command: str,
    hook_input: dict,
    timeout: float = 60.0,
) -> tuple[int, str, str]:
    """
    Execute a single shell command hook synchronously.
    Passes hook_input JSON via stdin.
    Mirrors execCommandHook() in hooks.ts.

    Returns (exit_code, stdout, stderr).
    Callers determine success (exit_code == 0), blocking (exit_code == 2),
    and output (stdout on success, stderr on failure) — mirroring the source.
    """
    try:
        input_json = json.dumps(hook_input)
        proc = subprocess.run(
            command,
            shell=True,
            input=input_json,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        return 1, "", f"Hook timed out after {timeout}s"
    except Exception as exc:
        return 1, "", str(exc)


async def _execute_hook_command_async(
    command: str,
    hook_input: dict,
    timeout: float = 60.0,
) -> tuple[int, str, str]:
    """
    Async wrapper around _execute_hook_command.
    Runs in a thread pool so it doesn't block the event loop.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, _execute_hook_command, command, hook_input, timeout
    )


def _get_matching_commands(
    hooks_config: dict[str, list[dict]],
    event: str,
    match_query: str | None,
) -> list[dict]:
    """
    Return all command-type hook commands that match event + match_query.
    Mirrors getMatchingHooks() + the matcher filtering in hooks.ts.
    """
    matchers = hooks_config.get(event, [])
    commands: list[dict] = []
    for matcher_entry in matchers:
        if not isinstance(matcher_entry, dict):
            continue
        pattern = matcher_entry.get("matcher")  # None → matches everything
        if match_query is not None and pattern and not _matches_pattern(match_query, pattern):
            continue
        hook_list = matcher_entry.get("hooks", [])
        for hook in hook_list:
            if isinstance(hook, dict) and hook.get("type") == "command":
                commands.append(hook)
    return commands


# ── Base hook input ────────────────────────────────────────────────────────────

def _base_hook_input(
    cwd: str,
    session_id: str,
    transcript_path: str,
    permission_mode: str | None = None,
) -> dict:
    """
    Mirrors createBaseHookInput() in hooks.ts.
    """
    base: dict = {
        "session_id": session_id,
        "transcript_path": transcript_path,
        "cwd": cwd,
    }
    if permission_mode:
        base["permission_mode"] = permission_mode
    return base


# ── Public API ─────────────────────────────────────────────────────────────────

async def execute_pre_compact_hooks(
    trigger: Literal["manual", "auto"],
    custom_instructions: str | None,
    cwd: str,
    session_id: str = "",
    transcript_path: str = "",
) -> dict:
    """
    Execute PreCompact hooks.
    Mirrors executePreCompactHooks() in hooks.ts.

    Returns {"new_custom_instructions": str | None, "user_display_message": str | None}
    Successful hook stdout is joined and returned as new_custom_instructions to be
    appended to the compact prompt.
    """
    hooks_config = _load_hooks_config(cwd)
    commands = _get_matching_commands(hooks_config, "PreCompact", trigger)
    if not commands:
        return {}

    hook_input = {
        **_base_hook_input(cwd, session_id, transcript_path),
        "hook_event_name": "PreCompact",
        "trigger": trigger,
        "custom_instructions": custom_instructions,
    }

    results = await asyncio.gather(*[
        _execute_hook_command_async(
            cmd["command"],
            hook_input,
            timeout=cmd.get("timeout", 60.0),
        )
        for cmd in commands
    ])

    successful_outputs: list[str] = []
    display_messages: list[str] = []
    for (code, stdout, stderr), cmd in zip(results, commands):
        command_str = cmd["command"]
        # Mirrors hooks.ts: exit 0 → stdout is the output; non-zero → stderr
        succeeded = code == 0
        out = (stdout if succeeded else stderr).strip()
        if succeeded:
            if out:
                successful_outputs.append(out)
                display_messages.append(
                    f"PreCompact [{command_str}] completed successfully: {out}"
                )
            else:
                display_messages.append(
                    f"PreCompact [{command_str}] completed successfully"
                )
        else:
            if out:
                display_messages.append(f"PreCompact [{command_str}] failed: {out}")
            else:
                display_messages.append(f"PreCompact [{command_str}] failed")

    return {
        "new_custom_instructions": "\n\n".join(successful_outputs) if successful_outputs else None,
        "user_display_message": "\n".join(display_messages) if display_messages else None,
    }


async def execute_post_compact_hooks(
    trigger: Literal["manual", "auto"],
    compact_summary: str,
    cwd: str,
    session_id: str = "",
    transcript_path: str = "",
) -> dict:
    """
    Execute PostCompact hooks.
    Mirrors executePostCompactHooks() in hooks.ts.

    Returns {"user_display_message": str | None}
    """
    hooks_config = _load_hooks_config(cwd)
    commands = _get_matching_commands(hooks_config, "PostCompact", trigger)
    if not commands:
        return {}

    hook_input = {
        **_base_hook_input(cwd, session_id, transcript_path),
        "hook_event_name": "PostCompact",
        "trigger": trigger,
        "compact_summary": compact_summary,
    }

    results = await asyncio.gather(*[
        _execute_hook_command_async(
            cmd["command"],
            hook_input,
            timeout=cmd.get("timeout", 60.0),
        )
        for cmd in commands
    ])

    display_messages: list[str] = []
    for (code, stdout, stderr), cmd in zip(results, commands):
        command_str = cmd["command"]
        succeeded = code == 0
        out = (stdout if succeeded else stderr).strip()
        if succeeded:
            if out:
                display_messages.append(
                    f"PostCompact [{command_str}] completed successfully: {out}"
                )
            else:
                display_messages.append(
                    f"PostCompact [{command_str}] completed successfully"
                )
        else:
            if out:
                display_messages.append(f"PostCompact [{command_str}] failed: {out}")
            else:
                display_messages.append(f"PostCompact [{command_str}] failed")

    return {
        "user_display_message": "\n".join(display_messages) if display_messages else None,
    }


async def execute_session_start_hooks(
    source: Literal["startup", "resume", "clear", "compact"],
    cwd: str,
    session_id: str = "",
    transcript_path: str = "",
    model: str | None = None,
) -> list[dict]:
    """
    Execute SessionStart hooks.
    Mirrors executeSessionStartHooks() in hooks.ts.

    Returns list of HookResultMessage-like dicts with output text.
    """
    hooks_config = _load_hooks_config(cwd)
    commands = _get_matching_commands(hooks_config, "SessionStart", source)
    if not commands:
        return []

    hook_input: dict = {
        **_base_hook_input(cwd, session_id, transcript_path),
        "hook_event_name": "SessionStart",
        "source": source,
    }
    if model:
        hook_input["model"] = model

    results = await asyncio.gather(*[
        _execute_hook_command_async(
            cmd["command"],
            hook_input,
            timeout=cmd.get("timeout", 60.0),
        )
        for cmd in commands
    ])

    messages: list[dict] = []
    for (code, stdout, stderr), cmd in zip(results, commands):
        succeeded = code == 0
        out = (stdout if succeeded else stderr).strip()
        if out:
            messages.append({
                "type": "hook_result",
                "event": "SessionStart",
                "command": cmd["command"],
                "succeeded": succeeded,
                "output": out,
            })
    return messages


async def execute_pre_tool_use_hooks(
    tool_name: str,
    tool_input: dict,
    cwd: str,
    session_id: str = "",
    transcript_path: str = "",
    permission_mode: str | None = None,
) -> dict:
    """
    Execute PreToolUse hooks for the given tool.
    Mirrors executePreToolUseHooks() in hooks.ts.

    Returns {
      "block": bool,           — True if any hook returned exit code 2 (block)
      "block_reason": str,     — stderr/stdout of the blocking hook
      "user_display_message": str | None,
    }
    """
    hooks_config = _load_hooks_config(cwd)
    commands = _get_matching_commands(hooks_config, "PreToolUse", tool_name)
    if not commands:
        return {"block": False}

    hook_input = {
        **_base_hook_input(cwd, session_id, transcript_path, permission_mode),
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input,
    }

    results = await asyncio.gather(*[
        _execute_hook_command_async(
            cmd["command"],
            hook_input,
            timeout=cmd.get("timeout", 60.0),
        )
        for cmd in commands
    ])

    display_messages: list[str] = []
    for (code, stdout, stderr), cmd in zip(results, commands):
        command_str = cmd["command"]
        succeeded = code == 0
        # Exit code 2 → blocking error (mirrors hooks.ts: result.status === 2 → blocked)
        blocked = code == 2
        out = (stdout if succeeded else stderr).strip()
        if blocked:
            display_messages.append(f"PreToolUse [{command_str}] blocked: {out}")
            return {
                "block": True,
                "block_reason": out or f"Blocked by hook: {command_str}",
                "user_display_message": "\n".join(display_messages),
            }
        data = _parse_hook_json(stdout) if succeeded else None
        if data:
            if data.get("continue") is False:
                return {
                    "block": True,
                    "block_reason": data.get("stopReason") or data.get("reason") or "Blocked by PreToolUse hook",
                    "user_display_message": "\n".join(display_messages) if display_messages else None,
                }
            if data.get("decision") == "block":
                return {
                    "block": True,
                    "block_reason": data.get("reason") or "Blocked by PreToolUse hook",
                    "user_display_message": "\n".join(display_messages) if display_messages else None,
                }
            specific = data.get("hookSpecificOutput")
            if isinstance(specific, dict) and specific.get("hookEventName") == "PreToolUse":
                permission_decision = specific.get("permissionDecision")
                if permission_decision in ("deny", "ask"):
                    return {
                        "block": True,
                        "block_reason": specific.get("permissionDecisionReason") or "Blocked by PreToolUse hook",
                        "permission_decision": permission_decision,
                        "user_display_message": "\n".join(display_messages) if display_messages else None,
                    }
                if permission_decision == "allow" or specific.get("updatedInput"):
                    return {
                        "block": False,
                        "permission_decision": permission_decision,
                        "updated_input": specific.get("updatedInput"),
                        "additional_context": specific.get("additionalContext"),
                        "user_display_message": "\n".join(display_messages) if display_messages else None,
                    }
        if out:
            display_messages.append(
                f"PreToolUse [{command_str}] completed: {out}"
            )

    return {
        "block": False,
        "user_display_message": "\n".join(display_messages) if display_messages else None,
    }


def _parse_hook_json(stdout: str) -> dict | None:
    """
    Parse a command hook JSON response from stdout.
    Claude Code treats plain stdout as display/additional text for most hooks,
    but PermissionRequest decisions are carried in hookSpecificOutput JSON.
    """
    text = (stdout or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


async def execute_permission_request_hooks(
    tool_name: str,
    tool_input: dict,
    permission_result: dict,
    cwd: str,
    session_id: str = "",
    transcript_path: str = "",
    permission_mode: str | None = None,
) -> dict | None:
    """
    Execute PermissionRequest hooks and return a hook decision.

    Mirrors the source's PermissionRequest hook path in permissions.ts:
    hooks may approve or deny an otherwise interactive permission request.
    Supported JSON stdout shape:

      {
        "hookSpecificOutput": {
          "hookEventName": "PermissionRequest",
          "decision": {"behavior": "allow", "updatedInput": {...}}
        }
      }

    or {"decision": {"behavior": "deny", "message": "..."}}
    """
    hooks_config = _load_hooks_config(cwd)
    commands = _get_matching_commands(hooks_config, "PermissionRequest", tool_name)
    if not commands:
        return None

    hook_input = {
        **_base_hook_input(cwd, session_id, transcript_path, permission_mode),
        "hook_event_name": "PermissionRequest",
        "tool_name": tool_name,
        "tool_input": tool_input,
        "permission_result": permission_result,
    }

    results = await asyncio.gather(*[
        _execute_hook_command_async(
            cmd["command"],
            hook_input,
            timeout=cmd.get("timeout", 60.0),
        )
        for cmd in commands
    ])

    for code, stdout, stderr in results:
        if code != 0:
            continue
        data = _parse_hook_json(stdout)
        if not data:
            continue
        specific = data.get("hookSpecificOutput")
        if isinstance(specific, dict) and specific.get("hookEventName") == "PermissionRequest":
            decision = specific.get("decision")
        else:
            decision = data.get("decision")
        if not isinstance(decision, dict):
            continue

        behavior = decision.get("behavior")
        if behavior == "allow":
            return {
                "behavior": "allow",
                "updatedInput": decision.get("updatedInput"),
                "decisionReason": {
                    "type": "hook",
                    "hookName": "PermissionRequest",
                    "reason": decision.get("reason") or "Allowed by PermissionRequest hook",
                },
            }
        if behavior == "deny":
            return {
                "behavior": "deny",
                "message": decision.get("message") or stderr.strip() or "Permission denied by PermissionRequest hook",
                "decisionReason": {
                    "type": "hook",
                    "hookName": "PermissionRequest",
                    "reason": decision.get("reason") or decision.get("message") or "",
                },
            }

    return None


async def execute_stop_hooks(
    last_assistant_message: str | None,
    cwd: str,
    session_id: str = "",
    transcript_path: str = "",
    permission_mode: str | None = None,
    stop_hook_active: bool = False,
) -> dict:
    """
    Execute Stop hooks when the agent finishes a response (no tool calls).
    Mirrors executeStopHooks() in hooks.ts.

    Returns {
      "block": bool,        — True if exit code 2 (model should continue)
      "block_reason": str,  — message to feed back to the model as a system note
    }
    A blocking Stop hook with message is converted to:
      "Stop hook feedback:\\n{message}"
    which is injected back into the query loop as a user message so the model
    can act on it.
    """
    hooks_config = _load_hooks_config(cwd)
    commands = _get_matching_commands(hooks_config, "Stop", None)
    if not commands:
        return {"block": False}

    hook_input: dict = {
        **_base_hook_input(cwd, session_id, transcript_path, permission_mode),
        "hook_event_name": "Stop",
        "stop_hook_active": stop_hook_active,
    }
    if last_assistant_message:
        hook_input["last_assistant_message"] = last_assistant_message

    results = await asyncio.gather(*[
        _execute_hook_command_async(
            cmd["command"],
            hook_input,
            timeout=cmd.get("timeout", 60.0),
        )
        for cmd in commands
    ])

    for (code, stdout, stderr), cmd in zip(results, commands):
        if code == 2:
            out = stderr.strip() or stdout.strip()
            return {
                "block": True,
                "block_reason": f"Stop hook feedback:\n{out}" if out else "Stop hook blocked",
            }

    return {"block": False}


async def execute_user_prompt_submit_hooks(
    prompt: str,
    cwd: str,
    session_id: str = "",
    transcript_path: str = "",
    permission_mode: str | None = None,
) -> dict:
    """
    Execute UserPromptSubmit hooks when the user submits a message.
    Mirrors executeUserPromptSubmitHooks() in hooks.ts.

    Returns {
      "block": bool,          — True if any hook returned exit code 2
      "block_reason": str,    — stderr of the blocking hook
      "additional_context": str | None,  — extra context to inject into the conversation
    }
    """
    hooks_config = _load_hooks_config(cwd)
    commands = _get_matching_commands(hooks_config, "UserPromptSubmit", None)
    if not commands:
        return {"block": False}

    hook_input = {
        **_base_hook_input(cwd, session_id, transcript_path, permission_mode),
        "hook_event_name": "UserPromptSubmit",
        "prompt": prompt,
    }

    results = await asyncio.gather(*[
        _execute_hook_command_async(
            cmd["command"],
            hook_input,
            timeout=cmd.get("timeout", 60.0),
        )
        for cmd in commands
    ])

    additional_contexts: list[str] = []
    for (code, stdout, stderr), cmd in zip(results, commands):
        command_str = cmd["command"]
        succeeded = code == 0
        blocked = code == 2
        out = (stdout if succeeded else stderr).strip()
        if blocked:
            return {
                "block": True,
                "block_reason": out or f"Blocked by UserPromptSubmit hook: {command_str}",
            }
        if succeeded and stdout.strip():
            # Successful hook stdout → additional context to inject
            additional_contexts.append(stdout.strip())

    return {
        "block": False,
        "additional_context": "\n\n".join(additional_contexts) if additional_contexts else None,
    }


async def execute_post_tool_use_hooks(
    tool_name: str,
    tool_input: dict,
    tool_response: str,
    cwd: str,
    session_id: str = "",
    transcript_path: str = "",
    permission_mode: str | None = None,
) -> dict:
    """
    Execute PostToolUse hooks for the given tool.
    Mirrors executePostToolUseHooks() in hooks.ts.

    Returns {"user_display_message": str | None}
    """
    hooks_config = _load_hooks_config(cwd)
    commands = _get_matching_commands(hooks_config, "PostToolUse", tool_name)
    if not commands:
        return {}

    hook_input = {
        **_base_hook_input(cwd, session_id, transcript_path, permission_mode),
        "hook_event_name": "PostToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_response": tool_response,
    }

    results = await asyncio.gather(*[
        _execute_hook_command_async(
            cmd["command"],
            hook_input,
            timeout=cmd.get("timeout", 60.0),
        )
        for cmd in commands
    ])

    display_messages: list[str] = []
    additional_contexts: list[str] = []
    updated_tool_output = None
    for (code, stdout, stderr), cmd in zip(results, commands):
        command_str = cmd["command"]
        succeeded = code == 0
        out = (stdout if succeeded else stderr).strip()
        data = _parse_hook_json(stdout) if succeeded else None
        if data:
            if data.get("continue") is False:
                display_messages.append(
                    data.get("stopReason") or data.get("reason") or "Execution stopped by PostToolUse hook"
                )
            specific = data.get("hookSpecificOutput")
            if isinstance(specific, dict) and specific.get("hookEventName") == "PostToolUse":
                additional = specific.get("additionalContext")
                if isinstance(additional, str) and additional:
                    additional_contexts.append(additional)
                if "updatedMCPToolOutput" in specific:
                    updated_tool_output = specific.get("updatedMCPToolOutput")
        elif out:
            if succeeded:
                display_messages.append(
                    f"PostToolUse [{command_str}] completed: {out}"
                )
            else:
                display_messages.append(
                    f"PostToolUse [{command_str}] failed: {out}"
                )

    result = {
        "user_display_message": "\n".join(display_messages) if display_messages else None,
    }
    if additional_contexts:
        result["additional_context"] = "\n\n".join(additional_contexts)
    if updated_tool_output is not None:
        result["updated_tool_output"] = updated_tool_output
    return result
