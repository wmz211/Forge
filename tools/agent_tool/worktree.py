"""
Git worktree helpers for isolated agent execution.
Mirrors src/utils/worktree.ts:
  createAgentWorktree()   → _create_worktree()
  hasWorktreeChanges()    → _has_worktree_changes()
  removeAgentWorktree()   → _remove_worktree()
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import uuid


async def _run_git(args: list[str], cwd: str, timeout: float = 30.0) -> tuple[int, str, str]:
    """Run a git subcommand, return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return (
        proc.returncode,
        stdout_b.decode("utf-8", errors="replace"),
        stderr_b.decode("utf-8", errors="replace"),
    )


async def create_worktree(cwd: str) -> tuple[str, str] | str:
    """
    Create a temporary git worktree for agent isolation.
    Mirrors createAgentWorktree() in worktree.ts.

    Returns (worktree_dir, branch_name) on success,
    or an '<error>…</error>' string on failure.
    """
    # Verify git repo
    try:
        rc, _, _ = await _run_git(["rev-parse", "--git-dir"], cwd, timeout=10)
        if rc != 0:
            return "<error>Not a git repository — worktree isolation requires git.</error>"
    except asyncio.TimeoutError:
        return "<error>git command timed out while checking repository.</error>"
    except FileNotFoundError:
        return "<error>git not found — worktree isolation requires git in PATH.</error>"

    branch_name = f"agent-worktree-{uuid.uuid4().hex[:8]}"
    tmp_dir = tempfile.mkdtemp(prefix="cagent-wt-")

    try:
        rc, _, stderr = await _run_git(
            ["worktree", "add", "-b", branch_name, tmp_dir, "HEAD"],
            cwd=cwd,
            timeout=30,
        )
        if rc != 0:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return f"<error>git worktree add failed: {stderr.strip()}</error>"
    except asyncio.TimeoutError:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return "<error>git worktree add timed out.</error>"

    return tmp_dir, branch_name


async def has_worktree_changes(worktree_dir: str) -> str:
    """
    Return a porcelain status string if the worktree has uncommitted changes,
    otherwise return ''.
    Mirrors hasWorktreeChanges() in worktree.ts.
    """
    try:
        rc, stdout, _ = await _run_git(["status", "--porcelain"], worktree_dir)
        return stdout.strip() if rc == 0 else ""
    except Exception:
        return ""


async def remove_worktree(
    worktree_dir: str,
    branch_name: str | None,
    repo_cwd: str,
) -> str:
    """
    Remove the worktree and delete its branch.
    Returns a human-readable summary if there were uncommitted changes.
    Mirrors removeAgentWorktree() in worktree.ts.
    """
    changes = await has_worktree_changes(worktree_dir)
    summary = f"Worktree had uncommitted changes:\n{changes}" if changes else ""

    # git worktree remove --force <path>
    try:
        await _run_git(["worktree", "remove", "--force", worktree_dir], repo_cwd, timeout=15)
    except Exception:
        # best-effort: if git fails, clean up the directory directly
        shutil.rmtree(worktree_dir, ignore_errors=True)

    # Delete the auto-created branch
    if branch_name:
        try:
            await _run_git(["branch", "-D", branch_name], repo_cwd, timeout=10)
        except Exception:
            pass

    return summary
