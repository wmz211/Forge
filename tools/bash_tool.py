from __future__ import annotations
import asyncio
import os
import re
import shlex
import sys
from typing import Any

from tool import Tool, ToolContext

# Timeout defaults in milliseconds — matches src/utils/timeouts.ts constants
# DEFAULT_TIMEOUT_MS = 120_000 (2 min), MAX_TIMEOUT_MS = 600_000 (10 min)
_DEFAULT_TIMEOUT_MS: int = int(os.environ.get("BASH_DEFAULT_TIMEOUT_MS", "120000"))
_MAX_TIMEOUT_MS: int = max(int(os.environ.get("BASH_MAX_TIMEOUT_MS", "600000")), _DEFAULT_TIMEOUT_MS)

# Matches maxResultSizeChars = 30_000 in BashTool.tsx
MAX_OUTPUT_CHARS = 30_000

# ── Command classification sets (mirrors BashTool.tsx) ─────────────────────

# BASH_SEARCH_COMMANDS in BashTool.tsx
_SEARCH_CMDS = frozenset({"find", "grep", "rg", "ag", "ack", "locate", "which", "whereis"})

# BASH_READ_COMMANDS in BashTool.tsx
_READ_CMDS = frozenset({
    "cat", "head", "tail", "less", "more",
    "wc", "stat", "file", "strings",
    "jq", "awk", "cut", "sort", "uniq", "tr",
})

# BASH_LIST_COMMANDS in BashTool.tsx
_LIST_CMDS = frozenset({"ls", "tree", "du"})

# BASH_SEMANTIC_NEUTRAL_COMMANDS in BashTool.tsx — don't affect read/search nature
_NEUTRAL_CMDS = frozenset({"echo", "printf", "true", "false", ":"})

# BASH_SILENT_COMMANDS in BashTool.tsx — expected no stdout on success
_SILENT_CMDS = frozenset({
    "mv", "cp", "rm", "mkdir", "rmdir", "chmod", "chown", "chgrp",
    "touch", "ln", "cd", "export", "unset", "wait",
})

# Device files that may block indefinitely if read
# Mirrors the device-file blacklist implied by FileReadTool source
_DANGEROUS_DEVICE_RE = re.compile(r"/dev/(stdin|zero|urandom|random|full)\b")

# Standalone sleep N (N >= 2) pattern — mirrors detectBlockedSleepPattern()
_BLOCKING_SLEEP_RE = re.compile(r"^sleep\s+(\d+)\s*$")

# Env-var assignment prefix (VAR=value) — used when extracting base command
_ENV_VAR_RE = re.compile(r"^[A-Za-z_]\w*=")


def _tokens(part: str) -> list[str]:
    try:
        return shlex.split(part, posix=True)
    except ValueError:
        return part.strip().split()


def _strip_leading_env(tokens: list[str]) -> list[str]:
    while tokens and _ENV_VAR_RE.match(tokens[0]):
        tokens = tokens[1:]
    return tokens


def _strip_env_command(tokens: list[str]) -> list[str]:
    if not tokens or tokens[0] != "env":
        return tokens
    tokens = tokens[1:]
    while tokens:
        token = tokens[0]
        if token in ("-i", "-0"):
            tokens = tokens[1:]
            continue
        if token in ("-u", "-C", "-S") and len(tokens) > 1:
            tokens = tokens[2:]
            continue
        if token.startswith("-"):
            tokens = tokens[1:]
            continue
        if _ENV_VAR_RE.match(token):
            tokens = tokens[1:]
            continue
        break
    return tokens


def _strip_safe_wrappers(tokens: list[str]) -> list[str]:
    tokens = _strip_leading_env(tokens)
    changed = True
    while changed and tokens:
        changed = False
        if tokens[0] == "env":
            new_tokens = _strip_env_command(tokens)
            changed = new_tokens != tokens
            tokens = _strip_leading_env(new_tokens)
        elif tokens[0] == "timeout" and len(tokens) >= 3:
            i = 1
            while i < len(tokens) and tokens[i].startswith("-"):
                i += 2 if tokens[i] in ("-k", "--kill-after", "-s", "--signal") and i + 1 < len(tokens) else 1
            if i < len(tokens):
                i += 1
            tokens = _strip_leading_env(tokens[i:])
            changed = True
        elif tokens[0] == "nice" and len(tokens) >= 2:
            i = 1
            if i < len(tokens) and tokens[i] == "-n" and i + 1 < len(tokens):
                i += 2
            elif i < len(tokens) and re.match(r"^-\d+$", tokens[i]):
                i += 1
            tokens = _strip_leading_env(tokens[i:])
            changed = True
        elif tokens[0] in ("nohup", "time") and len(tokens) >= 2:
            tokens = _strip_leading_env(tokens[1:])
            changed = True
    return tokens


# ── Helper utilities ────────────────────────────────────────────────────────

def _split_command_parts(command: str) -> list[str]:
    """
    Split compound command on ||, &&, |, ; to get individual subcommands.
    Quote-aware enough to avoid splitting operators inside single/double quotes.
    This mirrors the intent of splitCommandWithOperators without a full shell AST.
    Returns a flat list of subcommand strings (operators stripped out).
    """
    parts: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    escaped = False
    i = 0
    while i < len(command):
        ch = command[i]
        if escaped:
            buf.append(ch)
            escaped = False
            i += 1
            continue
        if ch == "\\":
            buf.append(ch)
            escaped = True
            i += 1
            continue
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if command.startswith("&&", i) or command.startswith("||", i):
            part = "".join(buf).strip()
            if part:
                parts.append(part)
            buf = []
            i += 2
            continue
        if ch in "|;":
            part = "".join(buf).strip()
            if part:
                parts.append(part)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    part = "".join(buf).strip()
    if part:
        parts.append(part)
    return [p.strip() for p in parts if p.strip()]


def _base_command(part: str) -> str:
    """
    Extract the base command name from a command segment, skipping leading
    VAR=value assignments (mirrors ENV_VAR_ASSIGN_RE logic in bashPermissions.ts).
    """
    for tok in _strip_safe_wrappers(_tokens(part)):
        # Handle paths like /usr/bin/grep → grep
        return os.path.basename(tok)
    return ""


def is_search_or_read_command(command: str) -> dict[str, bool]:
    """
    Classify whether a compound bash command is a search, read, or list operation.
    ALL non-neutral parts must be search/read/list for the whole to qualify.
    Mirrors isSearchOrReadBashCommand() in BashTool.tsx.
    """
    parts = _split_command_parts(command)
    if not parts:
        return {"is_search": False, "is_read": False, "is_list": False}

    has_search = has_read = has_list = has_non_neutral = False
    for part in parts:
        base = _base_command(part)
        if not base:
            continue
        if base in _NEUTRAL_CMDS:
            continue
        has_non_neutral = True
        is_s = base in _SEARCH_CMDS
        is_r = base in _READ_CMDS
        is_l = base in _LIST_CMDS
        if not (is_s or is_r or is_l):
            return {"is_search": False, "is_read": False, "is_list": False}
        if is_s:
            has_search = True
        if is_r:
            has_read = True
        if is_l:
            has_list = True

    if not has_non_neutral:
        return {"is_search": False, "is_read": False, "is_list": False}
    return {"is_search": has_search, "is_read": has_read, "is_list": has_list}


def is_silent_command(command: str) -> bool:
    """
    Returns True when the command is expected to produce no stdout on success
    (so the UI can show "Done" instead of "(no output)").
    Mirrors isSilentBashCommand() in BashTool.tsx.
    """
    parts = _split_command_parts(command)
    if not parts:
        return False
    has_non_fallback = False
    for part in parts:
        base = _base_command(part)
        if not base:
            continue
        has_non_fallback = True
        if base not in _SILENT_CMDS:
            return False
    return has_non_fallback


def command_has_any_cd(command: str) -> bool:
    """
    Returns True if any subcommand in the pipeline is `cd`.
    Mirrors commandHasAnyCd() in bashPermissions.ts.
    cd changes working directory and makes the command non-read-only.
    """
    return any(_base_command(p) == "cd" for p in _split_command_parts(command))


def detect_blocking_sleep(command: str) -> str | None:
    """
    Detect standalone `sleep N` (N >= 2) that should use run_in_background.
    Mirrors detectBlockedSleepPattern() in BashTool.tsx.
    Returns a description string if the pattern is blocked, None otherwise.
    """
    parts = _split_command_parts(command)
    if not parts:
        return None
    m = _BLOCKING_SLEEP_RE.match(parts[0])
    if not m:
        return None
    secs = int(m.group(1))
    if secs < 2:
        return None
    rest = " && ".join(parts[1:]).strip()
    return f"sleep {secs} followed by: {rest}" if rest else f"standalone sleep {secs}"


def _validate_command(command: str) -> str | None:
    sleep_pattern = detect_blocking_sleep(command)
    if sleep_pattern is not None:
        return (
            f"Blocked: {sleep_pattern}. "
            "Run blocking commands in the background with run_in_background: true - "
            "you'll get a completion notification when done. "
            "If you genuinely need a short delay (rate limiting, pacing), "
            "keep it under 2 seconds."
        )

    if _DANGEROUS_DEVICE_RE.search(command):
        return (
            "Command accesses a potentially blocking device file "
            "(/dev/stdin, /dev/zero, /dev/urandom, /dev/random, /dev/full). "
            "These may block forever. Use explicit alternatives instead."
        )

    return None


# ── Tool implementation ─────────────────────────────────────────────────────

class BashTool(Tool):
    name = "Bash"
    description = (
        "Executes a given bash command and returns its output.\n\n"
        "The working directory persists between commands, but shell state does not. "
        "The shell environment is initialized from the user's profile (bash or zsh).\n\n"
        "IMPORTANT: Avoid using this tool to run `find`, `grep`, `cat`, `head`, `tail`, "
        "`sed`, `awk`, or `echo` commands, unless explicitly instructed or after you have "
        "verified that a dedicated tool cannot accomplish your task. Instead, use the "
        "appropriate dedicated tool (Glob, Grep, Read, Edit, Write).\n\n"
        "Always quote file paths that contain spaces with double quotes in your command."
    )
    # Overridden per-call via is_concurrency_safe_for_input(); default False.
    is_concurrency_safe = False

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The command to execute",
                },
                "timeout": {
                    "type": "integer",
                    "description": (
                        f"Optional timeout in milliseconds (max {_MAX_TIMEOUT_MS}). "
                        f"By default, your command will timeout after {_DEFAULT_TIMEOUT_MS}ms "
                        f"({_DEFAULT_TIMEOUT_MS // 60000} minutes)."
                    ),
                },
                "description": {
                    "type": "string",
                    "description": (
                        "Clear, concise description of what this command does in active voice. "
                        "Never use words like 'complex' or 'risk' in the description - "
                        "just describe what it does.\n\n"
                        "For simple commands (git, npm, standard CLI tools), keep it brief "
                        "(5-10 words):\n"
                        '- ls → "List files in current directory"\n'
                        '- git status → "Show working tree status"\n\n'
                        "For commands that are harder to parse at a glance (piped commands, "
                        "obscure flags, etc.), add enough context to clarify what it does:\n"
                        '- find . -name "*.tmp" -exec rm {} \\; → "Find and delete all .tmp files recursively"'
                    ),
                },
                "run_in_background": {
                    "type": "boolean",
                    "description": (
                        "Set to true to run this command in the background. "
                        "Use this if you don't need the result immediately and are OK being "
                        "notified when the command completes later. "
                        "You do not need to use '&' at the end of the command when using "
                        "this parameter."
                    ),
                },
            },
            "required": ["command"],
        }

    def is_read_only(self, input: dict[str, Any]) -> bool:
        """
        Returns True if the command only reads data (no side effects).
        Mirrors isReadOnly() in BashTool.tsx:
          commandHasAnyCd → False; otherwise delegate to is_search_or_read_command.
        """
        command = input.get("command", "")
        if command_has_any_cd(command):
            return False
        result = is_search_or_read_command(command)
        return result["is_search"] or result["is_read"] or result["is_list"]

    def is_concurrency_safe_for_input(self, input: dict[str, Any]) -> bool:
        """
        Mirrors isConcurrencySafe(input) in BashTool.tsx:
          return this.isReadOnly?.(input) ?? false
        """
        return self.is_read_only(input)

    async def validate_input(self, input: dict[str, Any], ctx: ToolContext) -> tuple[bool, str | None]:
        error = _validate_command(str(input.get("command", "")))
        if error is not None:
            return False, error
        return True, None

    def render_call_summary(self, args: dict[str, Any]) -> str | None:
        """
        Show the description field if present, otherwise the command (truncated).
        Mirrors getToolUseSummary() in BashTool.tsx.
        """
        desc = args.get("description")
        if desc:
            return desc
        cmd = args.get("command", "")
        return cmd[:80] + "…" if len(cmd) > 80 else cmd

    async def call(self, input: dict[str, Any], ctx: ToolContext) -> str:
        command: str = input["command"]
        validation_error = _validate_command(command)
        if validation_error is not None:
            return f"<error>{validation_error}</error>"

        # ── Input validation (mirrors validateInput() in BashTool.tsx) ────────
        sleep_pattern = detect_blocking_sleep(command)
        if sleep_pattern is not None:
            return (
                f"<error>Blocked: {sleep_pattern}. "
                "Run blocking commands in the background with run_in_background: true — "
                "you'll get a completion notification when done. "
                "If you genuinely need a short delay (rate limiting, pacing), "
                "keep it under 2 seconds.</error>"
            )

        # ── Device file protection ─────────────────────────────────────────────
        # /dev/zero, /dev/urandom etc. can block indefinitely.
        # Mirrors the device-file blacklist implied by FileReadTool source.
        if _DANGEROUS_DEVICE_RE.search(command):
            return (
                "<error>Command accesses a potentially blocking device file "
                "(/dev/stdin, /dev/zero, /dev/urandom, /dev/random, /dev/full). "
                "These may block forever. Use explicit alternatives instead.</error>"
            )

        # ── Timeout: source uses milliseconds; clamp to valid range ───────────
        raw_timeout = input.get("timeout")
        if raw_timeout is None:
            timeout_ms = _DEFAULT_TIMEOUT_MS
        else:
            timeout_ms = max(1, min(int(raw_timeout), _MAX_TIMEOUT_MS))
        timeout_s = timeout_ms / 1000.0

        run_in_background: bool = bool(input.get("run_in_background", False))

        executable = None if sys.platform == "win32" else "/bin/bash"

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=ctx.cwd,
                executable=executable,
            )

            # ── Background mode ───────────────────────────────────────────
            # Source spawns a LocalShellTask with a task ID and captures output.
            # We register the process in ctx.todos["_bg_tasks"] so TaskStop /
            # TaskOutput can interact with it. Mirrors the task_id + proc pattern
            # from LocalShellTask in the source.
            if run_in_background:
                import uuid as _uuid
                task_id = str(_uuid.uuid4())[:8]
                output_buf: list[str] = []

                async def _capture(proc=proc, buf=output_buf):
                    """Background coroutine that drains stdout+stderr into buf."""
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
                    "type": "local_bash",
                    "output": output_buf,
                    "capture_task": capture_task,
                }
                pid = proc.pid
                return (
                    f"Command running in background (task_id={task_id}, PID={pid}). "
                    f"Use TaskOutput(task_id='{task_id}') to get output or "
                    f"TaskStop(task_id='{task_id}') to stop it."
                )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_s
                )
            except asyncio.TimeoutError:
                proc.kill()
                return f"<error>Command timed out after {timeout_ms}ms</error>"

            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")
            exit_code = proc.returncode

            # Source trims trailing whitespace on stdout (.trimEnd())
            stdout = stdout.rstrip()
            stderr = stderr.rstrip()

            # ── Output truncation ─────────────────────────────────────────
            # Mirrors maxResultSizeChars = 30_000 in BashTool.tsx.
            # Source uses EndTruncatingAccumulator (keeps start, drops end).
            if len(stdout) > MAX_OUTPUT_CHARS:
                total = len(stdout)
                stdout = stdout[:MAX_OUTPUT_CHARS] + f"\n... [truncated, {total} total chars]"

            # ── Result assembly ────────────────────────────────────────────
            # Mirrors mapToolResultToToolResultBlockParam() in BashTool.tsx:
            #   [processedStdout, errorMessage].filter(Boolean).join('\n')
            # where errorMessage = stderr.trim() + "\nExit code N" on failure.
            parts: list[str] = []
            if stdout:
                parts.append(stdout)

            error_parts: list[str] = []
            if stderr:
                error_parts.append(stderr)
            if exit_code != 0:
                error_parts.append(f"Exit code {exit_code}")
            if error_parts:
                parts.append("\n".join(error_parts))

            if not parts:
                # Mirrors noOutputExpected / isSilentBashCommand logic
                return "Done" if is_silent_command(command) else "(no output)"

            return "\n".join(parts)

        except Exception as e:
            return f"<error>{e}</error>"
