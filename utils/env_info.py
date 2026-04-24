"""
Environment info injection for sub-agent system prompts.
Mirrors src/constants/prompts.ts:
  computeEnvInfo()                    → compute_env_info()
  enhanceSystemPromptWithEnvDetails() → enhance_system_prompt()

The Notes section + <env> block are injected into every sub-agent's system
prompt so agents know to use absolute paths, avoid emojis, etc.
"""
from __future__ import annotations

import asyncio
import os
import platform
import subprocess
import sys


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_shell_info() -> str:
    """Mirrors getShellInfoLine() in prompts.ts."""
    shell = os.environ.get("SHELL") or os.environ.get("COMSPEC") or ""
    if shell:
        return f"Shell: {shell}"
    return f"Shell: {'cmd.exe' if sys.platform == 'win32' else '/bin/sh'}"


async def _is_git_repo(cwd: str) -> bool:
    """Check whether cwd is inside a git repository."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "rev-parse", "--git-dir",
            cwd=cwd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=5)
        return proc.returncode == 0
    except Exception:
        return False


def _uname() -> str:
    """Return OS version string. Mirrors getUnameSR() in prompts.ts."""
    try:
        return platform.platform()
    except Exception:
        return platform.system()


# ── Notes block (verbatim from enhanceSystemPromptWithEnvDetails) ─────────────

_NOTES = """\
Notes:
- Agent threads always have their cwd reset between bash calls, as a result \
please only use absolute file paths.
- In your final response, share file paths (always absolute, never relative) \
that are relevant to the task. Include code snippets only when the exact text \
is load-bearing (e.g., a bug you found, a function signature the caller asked \
for) — do not recap code you merely read.
- For clear communication with the user the assistant MUST avoid using emojis.
- Do not use a colon before tool calls. Text like "Let me read the file:" \
followed by a read tool call should just be "Let me read the file." with a period."""


# ── Main API ──────────────────────────────────────────────────────────────────

async def compute_env_info(cwd: str) -> str:
    """
    Build the <env> block injected into every sub-agent's system prompt.
    Mirrors computeEnvInfo() in src/constants/prompts.ts.
    """
    is_git = await _is_git_repo(cwd)
    os_version = _uname()
    shell_info = _get_shell_info()
    platform_str = sys.platform  # 'win32' | 'darwin' | 'linux'

    return (
        f"Here is useful information about the environment you are running in:\n"
        f"<env>\n"
        f"Working directory: {cwd}\n"
        f"Is directory a git repo: {'Yes' if is_git else 'No'}\n"
        f"Platform: {platform_str}\n"
        f"{shell_info}\n"
        f"OS Version: {os_version}\n"
        f"</env>"
    )


async def enhance_system_prompt(base_prompt: str, cwd: str) -> str:
    """
    Append Notes + <env> block to a base system prompt.
    Mirrors enhanceSystemPromptWithEnvDetails() in prompts.ts.

    Called by run_agent.py before every agent execution so each sub-agent
    receives accurate environment context.
    """
    env_info = await compute_env_info(cwd)
    return f"{base_prompt}\n\n{_NOTES}\n\n{env_info}"
