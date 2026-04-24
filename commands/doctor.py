"""
/doctor command — diagnose and verify the Forge installation.
Mirrors Claude Code's /doctor command in commands/doctor/index.ts.

Checks:
  - API key present and reachable (test call with minimal tokens)
  - Python version
  - Required packages installed
  - cwd exists and is readable
  - git available (for /commit, /diff)
  - Session storage directory writable
"""
from __future__ import annotations
import sys
import os
import shutil
from pathlib import Path
import commands as registry


async def _check_api(engine) -> tuple[bool, str]:
    """Attempt a minimal API round-trip to verify the key is valid."""
    try:
        result_text = ""
        async for event in engine._api.stream(
            messages=[{"role": "user", "content": "Reply with just: ok"}],
            tools=None,
            system_prompt="You are a test. Reply with exactly: ok",
        ):
            if event["type"] == "text":
                result_text += event["content"]
            if event["type"] == "done":
                break
        if result_text.strip():
            return True, f"OK (response: {result_text.strip()[:40]!r})"
        return False, "API returned empty response"
    except Exception as e:
        return False, f"FAILED — {e}"


async def _call(args: str, engine) -> str | None:
    lines = ["Forge /doctor report\n"]

    # Python version
    pv = sys.version.split()[0]
    ok = tuple(int(x) for x in pv.split(".")[:2]) >= (3, 10)
    lines.append(f"  Python version  : {pv}  {'✓' if ok else '✗ (3.10+ required)'}")

    # API key present
    api_key = os.environ.get("FORGE_API_KEY", "")
    lines.append(f"  API key         : {'present ✓' if api_key else 'MISSING ✗'}")

    # Model
    lines.append(f"  Model           : {engine._api.model}")

    # Required packages
    for pkg in ("openai", "prompt_toolkit", "rich"):
        try:
            __import__(pkg)
            lines.append(f"  Package {pkg:<13}: installed ✓")
        except ImportError:
            lines.append(f"  Package {pkg:<13}: MISSING ✗  (pip install {pkg})")

    # cwd
    cwd = engine.cwd
    cwd_ok = os.path.isdir(cwd)
    lines.append(f"  Working dir     : {cwd}  {'✓' if cwd_ok else '✗ (not a directory)'}")

    # git available
    git_path = shutil.which("git")
    lines.append(f"  git             : {git_path or 'not found ✗'}")

    # Session storage writable
    from query_engine import _get_project_dir
    proj_dir = _get_project_dir(cwd)
    try:
        proj_dir.mkdir(parents=True, exist_ok=True)
        test_file = proj_dir / ".doctor_write_test"
        test_file.write_text("x")
        test_file.unlink()
        lines.append(f"  Session storage : {proj_dir}  ✓")
    except Exception as e:
        lines.append(f"  Session storage : {proj_dir}  ✗ ({e})")

    # Thinking mode
    thinking = getattr(engine._api, "_thinking", False)
    lines.append(f"  Thinking mode   : {'enabled' if thinking else 'disabled'} (FORGE_ENABLE_THINKING)")

    # API test (skip if key missing to avoid misleading error)
    lines.append("")
    if api_key:
        lines.append("  Testing API connection…")
        ok, msg = await _check_api(engine)
        lines.append(f"  API test        : {msg}")
    else:
        lines.append("  API test        : skipped (no API key)")

    return "\n".join(lines)


registry.register({
    "name": "doctor",
    "description": "Diagnose and verify Forge installation",
    "aliases": [],
    "argument_hint": "",
    "call": _call,
})
