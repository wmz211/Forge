"""
/diff [path] — Show uncommitted git changes in the working directory.
Mirrors Claude Code's /diff command in commands/diff/.

Claude Code shows per-turn diffs; we show the current working-tree diff.
Without argument: git diff HEAD in engine.cwd.
With argument: git diff HEAD -- <path>.
"""
from __future__ import annotations
import asyncio
import commands as _reg


async def call(args: str, engine) -> str:
    path_filter = args.strip()

    cmd = ["git", "diff", "HEAD"]
    if path_filter:
        cmd += ["--", path_filter]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=engine.cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
    except asyncio.TimeoutError:
        return "  \033[31mTimeout running git diff.\033[0m"
    except FileNotFoundError:
        return "  \033[31mgit not found in PATH.\033[0m"

    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace").strip()
        if "not a git repository" in err.lower():
            return f"  \033[33m{engine.cwd} is not a git repository.\033[0m"
        return f"  \033[31mGit error:\033[0m {err}"

    output = stdout.decode("utf-8", errors="replace").strip()
    if not output:
        # Also check staged changes
        cmd2 = ["git", "diff", "--cached"]
        if path_filter:
            cmd2 += ["--", path_filter]
        proc2 = await asyncio.create_subprocess_exec(
            *cmd2, cwd=engine.cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out2, _ = await proc2.communicate()
        staged = out2.decode("utf-8", errors="replace").strip()
        if staged:
            return f"\033[1mStaged changes:\033[0m\n{_colorize(staged)}"
        return "  \033[32mNo uncommitted changes.\033[0m"

    return f"\033[1mUncommitted changes:\033[0m\n{_colorize(output)}"


def _colorize(diff: str) -> str:
    """Add ANSI colors to diff output."""
    lines = []
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            lines.append(f"\033[1m{line}\033[0m")
        elif line.startswith("+"):
            lines.append(f"\033[32m{line}\033[0m")
        elif line.startswith("-"):
            lines.append(f"\033[31m{line}\033[0m")
        elif line.startswith("@@"):
            lines.append(f"\033[36m{line}\033[0m")
        else:
            lines.append(line)
    return "\n".join(lines)


_reg.register({
    "name": "diff",
    "description": "Show uncommitted git changes in the working directory",
    "argument_hint": "[path]",
    "call": call,
})
