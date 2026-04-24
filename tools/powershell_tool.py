from __future__ import annotations

import asyncio
import os
import re
from typing import Any

from tool import Tool, ToolContext


_DEFAULT_TIMEOUT_MS = int(os.environ.get("POWERSHELL_DEFAULT_TIMEOUT_MS", "120000"))
_MAX_TIMEOUT_MS = max(int(os.environ.get("POWERSHELL_MAX_TIMEOUT_MS", "600000")), _DEFAULT_TIMEOUT_MS)
_MAX_OUTPUT_CHARS = 30_000

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
            },
            "required": ["command"],
        }

    def is_read_only(self, input: dict[str, Any]) -> bool:
        return _is_read_only_command(str(input.get("command") or ""))

    def is_concurrency_safe_for_input(self, input: dict[str, Any]) -> bool:
        return self.is_read_only(input)

    def render_call_summary(self, args: dict[str, Any]) -> str | None:
        return str(args.get("description") or args.get("command") or "")[:100]

    async def call(self, input: dict[str, Any], ctx: ToolContext) -> str:
        command = str(input["command"])
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
