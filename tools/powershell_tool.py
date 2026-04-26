from __future__ import annotations

import asyncio
import os
import re
from typing import Any

from tool import Tool, ToolContext


_DEFAULT_TIMEOUT_MS = int(os.environ.get("POWERSHELL_DEFAULT_TIMEOUT_MS", "120000"))
_MAX_TIMEOUT_MS = max(int(os.environ.get("POWERSHELL_MAX_TIMEOUT_MS", "600000")), _DEFAULT_TIMEOUT_MS)
_MAX_OUTPUT_CHARS = 30_000
_BLOCKING_SLEEP_RE = re.compile(r"^(?:start-sleep|sleep)(?:\s+-s(?:econds)?)?\s+(\d+)\s*$", re.IGNORECASE)

_READ_VERBS = frozenset({
    "get-childitem",
    "gci",
    "dir",
    "ls",
    "get-content",
    "gc",
    "type",
    "select-string",
    "measure-object",
    "get-item",
    "gi",
    "test-path",
    "resolve-path",
    "where-object",
    "select-object",
    "sort-object",
})
_NEUTRAL = frozenset({"write-output", "echo"})


def _split_pipeline(command: str) -> list[str]:
    return [p.strip() for p in re.split(r"\||;|&&|\|\|", command or "") if p.strip()]


def _base_command(part: str) -> str:
    tokens = part.strip().split()
    if not tokens:
        return ""
    return tokens[0].lower()


def _is_read_only_command(command: str) -> bool:
    parts = _split_pipeline(command)
    if not parts:
        return False
    saw_real_command = False
    for part in parts:
        base = _base_command(part)
        if not base:
            continue
        if base in _NEUTRAL:
            continue
        saw_real_command = True
        if base not in _READ_VERBS:
            return False
    return saw_real_command


def detect_blocking_sleep(command: str) -> str | None:
    first = re.split(r"[;|&\r\n]", (command or "").strip(), maxsplit=1)[0].strip()
    if not first:
        return None
    match = _BLOCKING_SLEEP_RE.match(first)
    if not match:
        return None
    secs = int(match.group(1))
    if secs < 2:
        return None
    rest = (command or "").strip()[len(first):].lstrip(" \t\r\n;|&")
    return f"Start-Sleep {secs} followed by: {rest}" if rest else f"standalone Start-Sleep {secs}"


def _validate_command(command: str) -> str | None:
    sleep_pattern = detect_blocking_sleep(command)
    if sleep_pattern is not None:
        return (
            f"Blocked: {sleep_pattern}. "
            "Run blocking commands in the background with run_in_background: true - "
            "you'll get a completion notification when done. "
            "If you genuinely need a delay (rate limiting, deliberate pacing), "
            "keep it under 2 seconds."
        )
    return None


class PowerShellTool(Tool):
    name = "PowerShell"
    search_hint = "run Windows PowerShell commands"
    description = (
        "Executes a PowerShell command and returns its output. Use this on Windows "
        "when PowerShell semantics are required. Prefer Read, Grep, Glob, Edit, "
        "and Write for filesystem operations when possible."
    )
    is_concurrency_safe = False

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The PowerShell command to execute",
                },
                "timeout": {
                    "type": "integer",
                    "description": f"Optional timeout in milliseconds (max {_MAX_TIMEOUT_MS}).",
                },
                "description": {
                    "type": "string",
                    "description": "Clear, concise active-voice description of what this command does.",
                },
                "run_in_background": {
                    "type": "boolean",
                    "description": "Set to true to run this command in the background.",
                },
            },
            "required": ["command"],
        }

    def is_read_only(self, input: dict[str, Any]) -> bool:
        return _is_read_only_command(str(input.get("command") or ""))

    def is_concurrency_safe_for_input(self, input: dict[str, Any]) -> bool:
        return self.is_read_only(input)

    def render_call_summary(self, args: dict[str, Any]) -> str | None:
        return str(args.get("description") or args.get("command") or "")[:100]

    async def validate_input(self, input: dict[str, Any], ctx: ToolContext) -> tuple[bool, str | None]:
        if input.get("run_in_background"):
            return True, None
        error = _validate_command(str(input.get("command") or ""))
        if error is not None:
            return False, error
        return True, None

    async def call(self, input: dict[str, Any], ctx: ToolContext) -> str:
        command = str(input["command"])
        if not input.get("run_in_background"):
            validation_error = _validate_command(command)
            if validation_error is not None:
                return f"<error>{validation_error}</error>"
        timeout_ms = int(input.get("timeout") or _DEFAULT_TIMEOUT_MS)
        timeout_ms = max(1, min(timeout_ms, _MAX_TIMEOUT_MS))

        proc = await asyncio.create_subprocess_exec(
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=ctx.cwd,
        )

        if input.get("run_in_background"):
            import uuid as _uuid
            task_id = str(_uuid.uuid4())[:8]
            output_buf: list[str] = []

            async def _capture(proc=proc, buf=output_buf):
                try:
                    stdout_b, stderr_b = await proc.communicate()
                    if stdout_b:
                        buf.append(stdout_b.decode("utf-8", errors="replace"))
                    if stderr_b:
                        buf.append(stderr_b.decode("utf-8", errors="replace"))
                except Exception:
                    pass

            capture_task = asyncio.ensure_future(_capture())
            ctx.todos.setdefault("_bg_tasks", {})[task_id] = {
                "proc": proc,
                "command": command,
                "type": "local_powershell",
                "output": output_buf,
                "capture_task": capture_task,
            }
            return (
                f"PowerShell command running in background with ID: {task_id} "
                f"(PID {proc.pid}). Use TaskOutput with task_id={task_id} to read output."
            )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_ms / 1000
            )
        except asyncio.TimeoutError:
            proc.kill()
            return f"<error>PowerShell command timed out after {timeout_ms}ms</error>"

        stdout = stdout_bytes.decode("utf-8", errors="replace").rstrip()
        stderr = stderr_bytes.decode("utf-8", errors="replace").rstrip()

        parts: list[str] = []
        if stdout:
            parts.append(stdout)
        if stderr:
            parts.append(stderr)
        if proc.returncode:
            parts.append(f"Exit code {proc.returncode}")

        result = "\n".join(parts) if parts else "(no output)"
        if len(result) > _MAX_OUTPUT_CHARS:
            result = result[:_MAX_OUTPUT_CHARS] + f"\n... [truncated, {len(result)} total chars]"
        return result
