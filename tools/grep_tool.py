from __future__ import annotations
import asyncio
import fnmatch
import os
import re
import shutil
import sys
from typing import Any

from tool import Tool, ToolContext

# Default result cap when head_limit is not specified — mirrors DEFAULT_HEAD_LIMIT = 250
DEFAULT_HEAD_LIMIT = 250

# Mirrors maxResultSizeChars = 20_000 in GrepTool.ts
MAX_OUTPUT_CHARS = 20_000

# Max line length before truncation (mirrors --max-columns 500 in source)
MAX_COLUMNS = 500

# VCS directories excluded from search — mirrors VCS_DIRECTORIES_TO_EXCLUDE in GrepTool.ts
VCS_DIRS = frozenset({".git", ".svn", ".hg", ".bzr", ".jj", ".sl"})

# ripgrep --type aliases for the most common file types
# (subset of ripgrep's built-in type list)
_TYPE_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "py":     ("*.py",),
    "js":     ("*.js", "*.jsx", "*.mjs", "*.cjs"),
    "ts":     ("*.ts", "*.tsx", "*.mts", "*.cts"),
    "rust":   ("*.rs",),
    "go":     ("*.go",),
    "java":   ("*.java",),
    "c":      ("*.c", "*.h"),
    "cpp":    ("*.cpp", "*.cc", "*.cxx", "*.hpp", "*.hxx"),
    "cs":     ("*.cs",),
    "rb":     ("*.rb",),
    "sh":     ("*.sh", "*.bash", "*.zsh"),
    "md":     ("*.md", "*.markdown"),
    "json":   ("*.json",),
    "yaml":   ("*.yaml", "*.yml"),
    "toml":   ("*.toml",),
    "xml":    ("*.xml",),
    "html":   ("*.html", "*.htm"),
    "css":    ("*.css", "*.scss", "*.sass", "*.less"),
    "sql":    ("*.sql",),
    "tf":     ("*.tf", "*.tfvars"),
}


# ── Pagination helpers (mirrors applyHeadLimit / formatLimitInfo) ───────────

def _apply_head_limit(
    items: list[str],
    limit: int | None,
    offset: int = 0,
) -> tuple[list[str], int | None]:
    """
    Apply offset + head_limit slicing.
    Returns (sliced_items, applied_limit_or_None).
    applied_limit is None when no truncation occurred (so the caller can
    omit the pagination note, matching source behaviour).
    Mirrors applyHeadLimit() in GrepTool.ts.
    """
    if limit == 0:
        return items[offset:], None
    effective = limit if limit is not None else DEFAULT_HEAD_LIMIT
    sliced = items[offset: offset + effective]
    truncated = (len(items) - offset) > effective
    return sliced, (effective if truncated else None)


def _format_limit_info(applied_limit: int | None, offset: int) -> str:
    """Mirrors formatLimitInfo() in GrepTool.ts."""
    parts: list[str] = []
    if applied_limit is not None:
        parts.append(f"limit: {applied_limit}")
    if offset:
        parts.append(f"offset: {offset}")
    return ", ".join(parts)


def _to_relative(path: str, cwd: str) -> str:
    """Convert absolute path to relative if under cwd, mirroring toRelativePath()."""
    try:
        return os.path.relpath(path, cwd)
    except ValueError:
        return path


# ── ripgrep runner ──────────────────────────────────────────────────────────

_RG_BIN: str | None = shutil.which("rg")


async def _run_ripgrep(args: list[str], search_path: str) -> list[str]:
    """
    Run ripgrep and return its stdout lines.
    Raises RuntimeError if rg is not available or exits with code > 1
    (exit 1 = no matches, which is fine and returns []).
    """
    if _RG_BIN is None:
        raise RuntimeError("ripgrep (rg) not found")

    cmd = [_RG_BIN] + args + [search_path]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=search_path if os.path.isdir(search_path) else os.path.dirname(search_path),
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    if proc.returncode == 0 or proc.returncode == 1:
        # 0 = matches found, 1 = no matches — both are normal
        return stdout.decode("utf-8", errors="replace").splitlines()
    raise RuntimeError(stderr.decode("utf-8", errors="replace").strip())


# ── Python fallback ─────────────────────────────────────────────────────────

def _collect_files(
    base: str,
    glob_pattern: str | None,
    type_globs: tuple[str, ...] | None,
) -> list[str]:
    """Walk directory, apply glob and type filters, exclude VCS dirs."""
    if os.path.isfile(base):
        return [base]

    result: list[str] = []
    for dirpath, dirnames, filenames in os.walk(base):
        # Exclude VCS directories in-place to prevent descent
        dirnames[:] = [d for d in dirnames if d not in VCS_DIRS]

        for fname in filenames:
            fpath = os.path.join(dirpath, fname)
            if glob_pattern and not fnmatch.fnmatch(fname, glob_pattern):
                continue
            if type_globs and not any(fnmatch.fnmatch(fname, g) for g in type_globs):
                continue
            result.append(fpath)
    return result


def _python_grep(
    pattern: str,
    base: str,
    glob_pattern: str | None,
    type_globs: tuple[str, ...] | None,
    case_insensitive: bool,
    multiline: bool,
    output_mode: str,
    context_before: int,
    context_after: int,
    show_line_numbers: bool,
) -> list[str]:
    """
    Pure-Python fallback for when ripgrep is unavailable.
    Returns lines in the same format rg would produce.
    """
    flags = re.IGNORECASE if case_insensitive else 0
    if multiline:
        flags |= re.DOTALL
    try:
        regex = re.compile(pattern, flags)
    except re.error as e:
        raise ValueError(f"Invalid regex: {e}") from e

    files = sorted(_collect_files(base, glob_pattern, type_globs))
    out: list[str] = []

    for fpath in files:
        try:
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError:
            continue

        lines = content.splitlines()

        if multiline:
            # Whole-file search: required for patterns that span lines.
            # ripgrep -U scans the full file buffer in one pass.
            file_matches = bool(regex.search(content))
            if output_mode == "files_with_matches":
                if file_matches:
                    out.append(fpath)
                continue
            if output_mode == "count":
                count = len(regex.findall(content))
                if count:
                    out.append(f"{fpath}:{count}")
                continue
            # content mode with multiline: find start line of each match
            if not file_matches:
                continue
            match_line_indices: set[int] = set()
            for m in regex.finditer(content):
                start_line = content.count("\n", 0, m.start())
                match_line_indices.add(start_line)
            # fall through to context expansion below
        else:
            # Per-line search (fast path for the common case)
            match_line_indices_list = [i for i, ln in enumerate(lines) if regex.search(ln)]
            match_line_indices = set(match_line_indices_list)

        if output_mode == "files_with_matches":
            if match_line_indices:
                out.append(fpath)

        elif output_mode == "count":
            if match_line_indices:
                out.append(f"{fpath}:{len(match_line_indices)}")

        elif output_mode == "content":
            if not match_line_indices:
                continue
            # Expand with context lines and deduplicate
            to_include: set[int] = set()
            for idx in match_line_indices:
                for j in range(max(0, idx - context_before), min(len(lines), idx + context_after + 1)):
                    to_include.add(j)

            prev: int | None = None
            for i in sorted(to_include):
                if prev is not None and i > prev + 1:
                    out.append("--")  # context separator
                line_text = lines[i].rstrip()
                if len(line_text) > MAX_COLUMNS:
                    line_text = line_text[:MAX_COLUMNS]
                sep = ":" if i in match_line_indices else "-"
                if show_line_numbers:
                    out.append(f"{fpath}{sep}{i + 1}{sep}{line_text}")
                else:
                    out.append(f"{fpath}{sep}{line_text}")
                prev = i

    return out


# ── Tool ────────────────────────────────────────────────────────────────────

class GrepTool(Tool):
    name = "Grep"
    description = (
        "A powerful search tool built on ripgrep\n\n"
        "Usage:\n"
        "- ALWAYS use Grep for search tasks. NEVER invoke `grep` or `rg` as a Bash command. "
        "The Grep tool has been optimized for correct permissions and access.\n"
        "- Supports full regex syntax (e.g., \"log.*Error\", \"function\\s+\\w+\")\n"
        "- Filter files with glob parameter (e.g., \"*.js\", \"**/*.tsx\") or type parameter "
        "(e.g., \"js\", \"py\", \"rust\")\n"
        "- Output modes: \"content\" shows matching lines, \"files_with_matches\" shows only "
        "file paths (default), \"count\" shows match counts\n"
        "- Use Agent tool for open-ended searches requiring multiple rounds\n"
        "- Pattern syntax: Uses ripgrep (not grep) - literal braces need escaping "
        "(use `interface\\{\\}` to find `interface{}` in Go code)\n"
        "- Multiline matching: By default patterns match within single lines only. "
        "For cross-line patterns like `struct \\{[\\s\\S]*?field`, use `multiline: true`\n"
    )
    is_concurrency_safe = True

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "The regular expression pattern to search for in file contents",
                },
                "path": {
                    "type": "string",
                    "description": (
                        "File or directory to search in (rg PATH). "
                        "Defaults to current working directory."
                    ),
                },
                "glob": {
                    "type": "string",
                    "description": (
                        'Glob pattern to filter files (e.g. "*.js", "*.{ts,tsx}") - '
                        "maps to rg --glob"
                    ),
                },
                "output_mode": {
                    "type": "string",
                    "enum": ["content", "files_with_matches", "count"],
                    "description": (
                        'Output mode: "content" shows matching lines (supports -A/-B/-C '
                        "context, -n line numbers, head_limit), "
                        '"files_with_matches" shows file paths (supports head_limit), '
                        '"count" shows match counts (supports head_limit). '
                        'Defaults to "files_with_matches".'
                    ),
                },
                "-B": {
                    "type": "integer",
                    "description": (
                        "Number of lines to show before each match (rg -B). "
                        'Requires output_mode: "content", ignored otherwise.'
                    ),
                },
                "-A": {
                    "type": "integer",
                    "description": (
                        "Number of lines to show after each match (rg -A). "
                        'Requires output_mode: "content", ignored otherwise.'
                    ),
                },
                "-C": {
                    "type": "integer",
                    "description": "Alias for context.",
                },
                "context": {
                    "type": "integer",
                    "description": (
                        "Number of lines to show before and after each match (rg -C). "
                        'Requires output_mode: "content", ignored otherwise.'
                    ),
                },
                "-n": {
                    "type": "boolean",
                    "description": (
                        "Show line numbers in output (rg -n). "
                        'Requires output_mode: "content", ignored otherwise. Defaults to true.'
                    ),
                },
                "-i": {
                    "type": "boolean",
                    "description": "Case insensitive search (rg -i)",
                },
                "type": {
                    "type": "string",
                    "description": (
                        "File type to search (rg --type). Common types: js, py, rust, go, java, "
                        "etc. More efficient than include for standard file types."
                    ),
                },
                "head_limit": {
                    "type": "integer",
                    "description": (
                        'Limit output to first N lines/entries, equivalent to "| head -N". '
                        "Works across all output modes: content (limits output lines), "
                        "files_with_matches (limits file paths), count (limits count entries). "
                        "Defaults to 250 when unspecified. Pass 0 for unlimited "
                        "(use sparingly — large result sets waste context)."
                    ),
                },
                "offset": {
                    "type": "integer",
                    "description": (
                        'Skip first N lines/entries before applying head_limit, equivalent to '
                        '"| tail -n +N | head -N". Works across all output modes. Defaults to 0.'
                    ),
                },
                "multiline": {
                    "type": "boolean",
                    "description": (
                        "Enable multiline mode where . matches newlines and patterns can span "
                        "lines (rg -U --multiline-dotall). Default: false."
                    ),
                },
            },
            "required": ["pattern"],
        }

    def render_call_summary(self, args: dict[str, Any]) -> str | None:
        """Mirrors getToolUseSummary() in GrepTool.ts — show pattern [in path]."""
        pat = args.get("pattern", "")
        path = args.get("path")
        if path:
            return f"{pat} in {path}"
        return pat

    async def call(self, input: dict[str, Any], ctx: ToolContext) -> str:
        pattern: str = input["pattern"]
        raw_path: str | None = input.get("path")
        base = os.path.join(ctx.cwd, raw_path) if raw_path and not os.path.isabs(raw_path) else (raw_path or ctx.cwd)
        glob_pattern: str | None = input.get("glob")
        output_mode: str = input.get("output_mode", "files_with_matches")
        context_before: int = input.get("-B", 0) or 0
        context_after: int = input.get("-A", 0) or 0
        context_c: int | None = input.get("-C") or input.get("context")
        if context_c is not None:
            context_before = context_after = int(context_c)
        else:
            context_before = int(context_before)
            context_after = int(context_after)
        show_line_numbers: bool = input.get("-n", True)
        case_insensitive: bool = input.get("-i", False)
        file_type: str | None = input.get("type")
        head_limit: int | None = input.get("head_limit")
        offset: int = int(input.get("offset") or 0)
        multiline: bool = bool(input.get("multiline", False))

        # Resolve type → glob extensions for fallback
        type_globs: tuple[str, ...] | None = None
        if file_type and file_type in _TYPE_EXTENSIONS:
            type_globs = _TYPE_EXTENSIONS[file_type]

        # ── Build ripgrep args (mirrors call() in GrepTool.ts) ───────────────
        rg_args: list[str] = ["--hidden"]

        # Exclude VCS dirs
        for vcs in VCS_DIRS:
            rg_args += ["--glob", f"!{vcs}"]

        # Max columns to prevent base64/minified noise
        rg_args += ["--max-columns", str(MAX_COLUMNS)]

        if multiline:
            rg_args += ["-U", "--multiline-dotall"]

        if case_insensitive:
            rg_args.append("-i")

        # Output mode flags
        if output_mode == "files_with_matches":
            rg_args.append("-l")
        elif output_mode == "count":
            rg_args.append("-c")

        # Line numbers (content mode only)
        if show_line_numbers and output_mode == "content":
            rg_args.append("-n")

        # Context (content mode only)
        if output_mode == "content":
            if context_c is not None:
                rg_args += ["-C", str(context_c)]
            else:
                if context_before:
                    rg_args += ["-B", str(context_before)]
                if context_after:
                    rg_args += ["-A", str(context_after)]

        # Pattern (use -e if starts with dash to avoid flag collision)
        if pattern.startswith("-"):
            rg_args += ["-e", pattern]
        else:
            rg_args.append(pattern)

        # File type
        if file_type:
            rg_args += ["--type", file_type]

        # Glob filter(s)
        if glob_pattern:
            for g in re.split(r"[\s,]+", glob_pattern):
                if g:
                    rg_args += ["--glob", g]

        # ── Execute: ripgrep first, Python fallback ───────────────────────────
        try:
            raw_lines = await _run_ripgrep(rg_args, base)
        except Exception:
            # Fallback to pure-Python grep
            try:
                raw_lines = _python_grep(
                    pattern=pattern,
                    base=base,
                    glob_pattern=glob_pattern,
                    type_globs=type_globs,
                    case_insensitive=case_insensitive,
                    multiline=multiline,
                    output_mode=output_mode,
                    context_before=context_before,
                    context_after=context_after,
                    show_line_numbers=show_line_numbers,
                )
            except ValueError as e:
                return f"<error>{e}</error>"

        # ── Format output (mirrors mapToolResultToToolResultBlockParam) ────────
        if output_mode == "content":
            # Convert absolute paths to relative, then paginate.
            # rg content lines: /abs/path:lineno:text  (matched)
            #                or  /abs/path-lineno-text  (context separator)
            # On Windows, drive letters produce "D:" at the start; skip pos 1.
            # rg content-mode line formats:
            #   match:   /abs/path:lineno:text       (separator = ":")
            #   context: /abs/path-lineno-text       (separator = "-")
            #   group:   --
            # On Windows, drive letter adds "D:" at pos 0-1, so we skip pos 1
            # when searching for the path/lineno separator.
            _win = sys.platform == "win32"

            def _relativize_content_line(line: str) -> str:
                if line == "--":
                    return line  # context group separator
                # On Windows, start search after drive colon (pos 1)
                search_start = 2 if (_win and len(line) > 1 and line[1] == ":") else 0
                # Try match-line format first (colon separator)
                colon = line.find(":", search_start)
                if colon > search_start:
                    fpath = line[:colon]
                    if os.path.sep in fpath or "/" in fpath:
                        return _to_relative(fpath, ctx.cwd) + line[colon:]
                    return line
                # Try context-line format (hyphen separator after path)
                # Pattern: path-DIGITS-rest  where path contains a dir separator
                m = re.search(r"(-\d+-)", line[search_start:])
                if m:
                    split_pos = search_start + m.start()
                    fpath = line[:split_pos]
                    if os.path.sep in fpath or "/" in fpath:
                        return _to_relative(fpath, ctx.cwd) + line[split_pos:]
                return line

            rel_lines = [_relativize_content_line(ln) for ln in raw_lines]
            items, applied_limit = _apply_head_limit(rel_lines, head_limit, offset)
            content = "\n".join(items) if items else "No matches found"
            limit_info = _format_limit_info(applied_limit, offset)
            if limit_info:
                content += f"\n\n[Showing results with pagination = {limit_info}]"
            return content

        if output_mode == "count":
            # rg count lines: /abs/path:N
            # rfind gives us the LAST colon — the one before the count number —
            # which is correct even on Windows where "D:" appears at position 1.
            def _relativize_count_line(line: str) -> str:
                colon = line.rfind(":")
                # Guard: must be past drive letter (pos 1 on Windows)
                if colon > 1:
                    return _to_relative(line[:colon], ctx.cwd) + line[colon:]
                return line

            rel_lines = [_relativize_count_line(ln) for ln in raw_lines]
            items, applied_limit = _apply_head_limit(rel_lines, head_limit, offset)
            total_matches = 0
            file_count = 0
            for ln in items:
                colon = ln.rfind(":")
                if colon > 0:
                    try:
                        total_matches += int(ln[colon + 1:])
                        file_count += 1
                    except ValueError:
                        pass
            content = "\n".join(items) if items else "No matches found"
            limit_info = _format_limit_info(applied_limit, offset)
            summary = (
                f"\n\nFound {total_matches} total "
                f"{'occurrence' if total_matches == 1 else 'occurrences'} "
                f"across {file_count} {'file' if file_count == 1 else 'files'}."
                + (f" with pagination = {limit_info}" if limit_info else "")
            )
            return content + summary

        # files_with_matches (default) — sort by mtime, newest first
        if not raw_lines:
            return "No files found"

        def _mtime(p: str) -> float:
            try:
                return os.path.getmtime(p)
            except OSError:
                return 0.0

        sorted_files = sorted(raw_lines, key=_mtime, reverse=True)
        items, applied_limit = _apply_head_limit(sorted_files, head_limit, offset)

        rel_files = [_to_relative(p, ctx.cwd) for p in items]
        n = len(rel_files)
        limit_info = _format_limit_info(applied_limit, offset)
        header = f"Found {n} {'file' if n == 1 else 'files'}"
        if limit_info:
            header += f" {limit_info}"
        return header + "\n" + "\n".join(rel_files)
