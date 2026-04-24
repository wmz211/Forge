"""
/commit command — create a git commit for staged/unstaged changes.
Mirrors Claude Code's /commit command in commands/commit.ts.

Behavior:
  - Gathers git context (status, diff, branch, recent log) via subprocess
  - Submits a structured prompt to the agent that instructs it to create a commit
  - Only git-related Bash calls are needed; all other tools are available
"""
from __future__ import annotations
import subprocess
import os
import commands as registry


_COMMIT_SYSTEM_PROMPT = """\
## Git Safety Protocol

- NEVER update the git config
- NEVER skip hooks (--no-verify, --no-gpg-sign, etc) unless the user explicitly requests it
- CRITICAL: ALWAYS create NEW commits. NEVER use git commit --amend unless explicitly asked
- Do not commit files that likely contain secrets (.env, credentials.json, etc)
- If there are no changes to commit, do not create an empty commit
- Never use git commands with the -i flag (interactive mode is not supported)

## Your task

Based on the git context above, create a single git commit:

1. Analyze staged changes and draft a commit message:
   - Follow the repository's existing commit message style
   - Summarize: new feature, bug fix, refactoring, docs, test, etc.
   - Write a concise (1-2 sentence) message focused on WHY, not what
   - Stage relevant files with `git add` if nothing is staged yet

2. Create the commit. Use HEREDOC syntax to avoid quoting issues:
```
git commit -m "$(cat <<'EOF'
Commit message here.
EOF
)"
```

Do not use any other tools besides Bash for git operations. \
Do not send any other text besides the tool calls.\
"""


def _run_git(args: list[str], cwd: str) -> str:
    """Run a git command and return stdout; return error string on failure."""
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.stdout.strip() or result.stderr.strip() or "(no output)"
    except FileNotFoundError:
        return "(git not found)"
    except subprocess.TimeoutExpired:
        return "(git command timed out)"
    except Exception as e:
        return f"(error: {e})"


async def _call(args: str, engine) -> str | None:
    cwd = engine.cwd

    # Gather git context
    status = _run_git(["status"], cwd)
    diff   = _run_git(["diff", "HEAD"], cwd)
    branch = _run_git(["branch", "--show-current"], cwd)
    log    = _run_git(["log", "--oneline", "-10"], cwd)

    # Truncate diff if huge (>8 KB) to stay within context budget
    if len(diff) > 8_000:
        diff = diff[:8_000] + "\n[... diff truncated ...]"

    prompt = f"""\
## Git context for /commit

- **Current branch**: {branch}
- **Recent commits**:
```
{log}
```
- **Current status**:
```
{status}
```
- **Diff (staged + unstaged)**:
```
{diff if diff else '(no changes)'}
```

{_COMMIT_SYSTEM_PROMPT}
"""

    # Stream through the engine so the agent handles tool calls
    from ui import EventRenderer, render_error
    renderer = EventRenderer(tools=engine.tools)
    renderer.spinner.start()

    try:
        async for event in engine.submit_message(prompt):
            renderer.handle(event)
    except Exception as e:
        renderer.spinner.stop()
        render_error(str(e))

    return None  # Engine already rendered output


registry.register({
    "name": "commit",
    "description": "Create a git commit for current changes",
    "aliases": [],
    "argument_hint": "",
    "call": _call,
})
