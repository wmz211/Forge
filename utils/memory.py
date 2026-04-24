from __future__ import annotations
"""
Memory file loading — discovers and loads CLAUDE.md files.
Mirrors src/utils/attachments.ts + src/memdir/ in Claude Code source.

Load order (mirrors source, later entries take higher precedence):
  1. Global user memory  : ~/.claude/CLAUDE.md
  2. Project root memory : <cwd>/CLAUDE.md (and any parent up to ~/.claude/)
  3. Local memory dir    : <cwd>/.claude/CLAUDE.md
  4. Sub-directory files : <cwd>/**/.claude/CLAUDE.md (depth ≤ 3)

The combined content is injected into the system prompt so the model knows
about project-specific rules without the user having to repeat them.
"""

import os
from pathlib import Path

# Max bytes to read from a single CLAUDE.md file (mirrors MAX_MEMORY_FILE_SIZE)
_MAX_BYTES = 200_000

# Max combined content across all memory files (rough guard)
_MAX_COMBINED_BYTES = 500_000

# Header injected before memory content in the system prompt
_MEMORY_HEADER = (
    "\n\n<memory>\n"
    "The following content from CLAUDE.md files provides project-specific "
    "instructions and context. Follow these instructions when assisting.\n\n"
)
_MEMORY_FOOTER = "\n</memory>"


def _read_file_safe(path: Path) -> str:
    """Read a file up to _MAX_BYTES, returning empty string on error."""
    try:
        raw = path.read_bytes()
        if len(raw) > _MAX_BYTES:
            raw = raw[:_MAX_BYTES]
        return raw.decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


def _candidate_paths(cwd: str) -> list[Path]:
    """
    Enumerate CLAUDE.md candidate paths in load order.
    Mirrors the discovery logic in attachments.ts / memdir/.
    """
    candidates: list[Path] = []
    cwd_path = Path(cwd).resolve()

    # 1. Global user-level memory
    user_memory = Path.home() / ".claude" / "CLAUDE.md"
    candidates.append(user_memory)

    # 2. Project-root memory: walk from cwd up to home (or filesystem root)
    #    Collect all CLAUDE.md files along the path, root-first.
    home = Path.home()
    ancestry: list[Path] = []
    current = cwd_path
    while True:
        ancestry.append(current / "CLAUDE.md")
        if current == home or current == current.parent:
            break
        current = current.parent
    # Reverse so parent dirs come first (least specific first)
    candidates.extend(reversed(ancestry))

    # 3. .claude/CLAUDE.md in the project directory
    candidates.append(cwd_path / ".claude" / "CLAUDE.md")

    # 4. Subdirectory .claude/CLAUDE.md files (depth ≤ 3)
    try:
        for root, dirs, files in os.walk(cwd_path):
            # Compute depth relative to cwd
            rel = Path(root).relative_to(cwd_path)
            depth = len(rel.parts)
            if depth > 3:
                dirs.clear()
                continue
            # Skip hidden dirs and common noise
            dirs[:] = [
                d for d in dirs
                if not d.startswith(".")
                and d not in {"node_modules", "__pycache__", ".venv", "venv",
                              ".git", "dist", "build", ".next"}
            ]
            for fname in files:
                if fname == "CLAUDE.md" and root != str(cwd_path):
                    candidates.append(Path(root) / fname)
    except OSError:
        pass

    return candidates


def load_memory_files(cwd: str) -> str:
    """
    Load all CLAUDE.md files reachable from cwd and return combined text.
    Returns an empty string when no memory files exist.

    Mirrors getAttachmentMessages() + CLAUDE.md loading in attachments.ts.
    Deduplicates by resolved path to avoid including the same file twice
    (e.g. if cwd == home the global and project-root files are the same).
    """
    seen: set[Path] = set()
    sections: list[str] = []
    total_bytes = 0

    for candidate in _candidate_paths(cwd):
        try:
            resolved = candidate.resolve()
        except OSError:
            continue

        if resolved in seen or not resolved.exists():
            continue
        seen.add(resolved)

        content = _read_file_safe(resolved)
        if not content:
            continue

        total_bytes += len(content)
        if total_bytes > _MAX_COMBINED_BYTES:
            break

        # Label each section so the model knows which file it came from
        rel_label = _relative_label(resolved, cwd)
        sections.append(f"### {rel_label}\n\n{content}")

    if not sections:
        return ""

    return _MEMORY_HEADER + "\n\n---\n\n".join(sections) + _MEMORY_FOOTER


def _relative_label(path: Path, cwd: str) -> str:
    """Return a human-readable label: relative path if under cwd, else ~/ form."""
    cwd_path = Path(cwd).resolve()
    home = Path.home()
    try:
        return str(path.relative_to(cwd_path))
    except ValueError:
        pass
    try:
        return "~/" + str(path.relative_to(home))
    except ValueError:
        pass
    return str(path)


def inject_memory_into_system_prompt(system_prompt: str, cwd: str) -> str:
    """
    Append loaded CLAUDE.md content to the system prompt.
    Returns the original system prompt unchanged if no memory files exist.
    Mirrors the memory-attachment step in QueryEngine.ts / attachments.ts.
    """
    memory = load_memory_files(cwd)
    if not memory:
        return system_prompt
    return system_prompt + memory
