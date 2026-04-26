from __future__ import annotations
import os
import re
import sys
import tempfile
from typing import Any

from tool import Tool, ToolContext
from utils.file_state_cache import FileState

MAX_EDIT_FILE_SIZE: int = int(os.environ.get("CLAUDE_CODE_MAX_EDIT_FILE_SIZE_BYTES", str(1024 * 1024 * 1024)))

# Quote normalization (mirrors normalizeQuotes() in utils.ts)
_QUOTE_NORMALIZATION: list[tuple[str, str]] = [
    (chr(0x2018), chr(39)),  # LEFT SINGLE CURLY
    (chr(0x2019), chr(39)),  # RIGHT SINGLE CURLY
    (chr(0x201c), chr(34)),  # LEFT DOUBLE CURLY
    (chr(0x201d), chr(34)),  # RIGHT DOUBLE CURLY
]


def _normalize_quotes(s: str) -> str:
    """Mirrors normalizeQuotes() in FileEditTool/utils.ts."""
    for curly, straight in _QUOTE_NORMALIZATION:
        s = s.replace(curly, straight)
    return s


def _is_opening_quote_context(chars: list[str], index: int) -> bool:
    if index == 0:
        return True
    prev = chars[index - 1]
    return prev in {" ", "\t", "\n", "\r", "(", "[", "{", chr(0x2014), chr(0x2013)}


def _apply_curly_double_quotes(s: str) -> str:
    chars = list(s)
    out: list[str] = []
    for i, ch in enumerate(chars):
        if ch == '"':
            out.append(chr(0x201c) if _is_opening_quote_context(chars, i) else chr(0x201d))
        else:
            out.append(ch)
    return "".join(out)


def _apply_curly_single_quotes(s: str) -> str:
    chars = list(s)
    out: list[str] = []
    for i, ch in enumerate(chars):
        if ch != "'":
            out.append(ch)
            continue
        prev = chars[i - 1] if i > 0 else ""
        next_ = chars[i + 1] if i + 1 < len(chars) else ""
        if prev.isalpha() and next_.isalpha():
            out.append(chr(0x2019))
        else:
            out.append(chr(0x2018) if _is_opening_quote_context(chars, i) else chr(0x2019))
    return "".join(out)


def _preserve_quote_style(old_string: str, actual_old_string: str, new_string: str) -> str:
    """Mirrors preserveQuoteStyle() in FileEditTool/utils.ts."""
    if old_string == actual_old_string:
        return new_string
    result = new_string
    if chr(0x201c) in actual_old_string or chr(0x201d) in actual_old_string:
        result = _apply_curly_double_quotes(result)
    if chr(0x2018) in actual_old_string or chr(0x2019) in actual_old_string:
        result = _apply_curly_single_quotes(result)
    return result


# De-sanitization (mirrors DESANITIZATIONS in utils.ts)
# String literals use \x3c / \x3e hex escapes for < / > to stay tool-safe.
# Python evaluates these to the correct characters at import time.
_DESANITIZATIONS: list[tuple[str, str]] = [
    ("\x3cfnr\x3e", "\x3cfunction_results\x3e"),
    ("\x3cn\x3e", "\x3cname\x3e"),
    ("\x3c/n\x3e", "\x3c/name\x3e"),
    ("\x3co\x3e", "\x3coutput\x3e"),
    ("\x3c/o\x3e", "\x3c/output\x3e"),
    ("\x3ce\x3e", "\x3cerror\x3e"),
    ("\x3c/e\x3e", "\x3c/error\x3e"),
    ("\x3cs\x3e", "\x3csystem\x3e"),
    ("\x3c/s\x3e", "\x3c/system\x3e"),
    ("\x3cr\x3e", "\x3cresult\x3e"),
    ("\x3c/r\x3e", "\x3c/result\x3e"),
    ("< META_START >", "\x3cMETA_START\x3e"),
    ("< META_END >", "\x3cMETA_END\x3e"),
    ("< EOT >", "\x3cEOT\x3e"),
    ("< META >", "\x3cMETA\x3e"),
    ("< SOS >", "\x3cSOS\x3e"),
    ("\n\nH:", "\n\nHuman:"),
    ("\n\nA:", "\n\nAssistant:"),
]


def _desanitize(s: str) -> tuple[str, list[tuple[str, str]]]:
    """
    Expand sanitized XML aliases back to their real forms.
    Mirrors desanitizeMatchString() in utils.ts.
    Returns (expanded_string, list_of_applied_replacements).
    """
    applied: list[tuple[str, str]] = []
    for from_, to in _DESANITIZATIONS:
        if from_ in s:
            s = s.replace(from_, to)
            applied.append((from_, to))
    return s, applied


# String matching with fallbacks (mirrors normalizeFileEditInput())
def _find_actual_string(content: str, search: str) -> str | None:
    """
    Find the actual occurrence in content, trying:
    1. Exact match
    2. Quote normalization (curly -> straight)
    3. De-sanitization (XML alias expansion)
    Returns the actual string as it appears in the file, or None.
    Mirrors findActualString() in utils.ts.
    """
    if search in content:
        return search

    norm_search = _normalize_quotes(search)
    norm_content = _normalize_quotes(content)
    idx = norm_content.find(norm_search)
    if idx != -1:
        return content[idx: idx + len(search)]

    desan, _ = _desanitize(search)
    if desan != search and desan in content:
        return desan

    return None


# Line ending detection (mirrors detectLineEndings())
def _detect_line_ending(content: str) -> str:
    """
    Detect the dominant line ending in content.
    Returns CRLF or LF. Mirrors detectLineEndings() in file.ts.
    """
    crlf = content.count("\r\n")
    lf = content.count("\n") - crlf
    return "\r\n" if crlf > lf else "\n"


def _normalize_line_endings(content: str, target_ending: str) -> str:
    """Normalise all line endings in content to target_ending."""
    unified = content.replace("\r\n", "\n").replace("\r", "\n")
    if target_ending == "\r\n":
        return unified.replace("\n", "\r\n")
    return unified


# Trailing whitespace stripping (mirrors stripTrailingWhitespace())
_TRAILING_WS_RE = re.compile(r"[ \t]+(?=\r?\n|$)", re.MULTILINE)


def _strip_trailing_whitespace(s: str) -> str:
    """Mirrors stripTrailingWhitespace() in utils.ts (preserves line endings)."""
    return _TRAILING_WS_RE.sub("", s)


def _format_file_size(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    unit = units[0]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            break
        value /= 1024
    if unit == "B":
        return f"{size}B"
    return f"{value:.1f}{unit}".replace(".0", "")


def _edit_size_error(path: str) -> str | None:
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    if size > MAX_EDIT_FILE_SIZE:
        return (
            f"File is too large to edit ({_format_file_size(size)}). "
            f"Maximum editable file size is {_format_file_size(MAX_EDIT_FILE_SIZE)}."
        )
    return None


# Atomic write (mirrors writeTextContent() + tmpfile in FileEditTool.ts)
def _write_atomic(path: str, content: str, encoding: str = "utf-8") -> None:
    """
    Write content atomically using a temporary file + rename.
    Mirrors the atomic read-modify-write section in FileEditTool.ts call().
    """
    dir_ = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(dir=dir_)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
        os.replace(tmp_path, path)  # atomic on POSIX; best-effort on Windows
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


class FileEditTool(Tool):
    name = "Edit"
    description = (
        "Performs exact string replacements in files.\n\n"
        "Usage:\n"
        "- You must use your Read tool at least once in the conversation before editing. "
        "This tool will error if you attempt an edit without reading the file.\n"
        "- When editing text from Read tool output, ensure you preserve the exact "
        "indentation (tabs/spaces) as it appears AFTER the line number prefix. "
        "The line number prefix format is: line number + tab. Everything after that "
        "is the actual file content to match. Never include any part of the line number "
        "prefix in the old_string or new_string.\n"
        "- ALWAYS prefer editing existing files in the codebase. NEVER write new files "
        "unless explicitly required.\n"
        "- Only use emojis if the user explicitly requests it. Avoid adding emojis to "
        "files unless asked.\n"
        "- The edit will FAIL if `old_string` is not unique in the file. Either provide "
        "a larger string with more surrounding context to make it unique or use "
        "`replace_all` to change every instance of `old_string`.\n"
        "- Use `replace_all` for replacing and renaming strings across the file. This "
        "parameter is useful if you want to rename a variable for instance."
    )
    is_concurrency_safe = False

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "The absolute path to the file to modify",
                },
                "old_string": {
                    "type": "string",
                    "description": "The text to replace",
                },
                "new_string": {
                    "type": "string",
                    "description": "The text to replace it with (must be different from old_string)",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": (
                        "Replace all occurrences of old_string (default false). "
                        "Useful for renaming variables or identifiers across the file."
                    ),
                },
            },
            "required": ["file_path", "old_string", "new_string"],
        }

    def render_call_summary(self, args: dict) -> str | None:
        """Show file path + first non-empty line of old_string as the anchor."""
        path = args.get("file_path", "?")
        old = args.get("old_string", "")
        anchor = next((ln.strip() for ln in old.splitlines() if ln.strip()), "")
        if len(anchor) > 60:
            anchor = anchor[:57] + chr(0x2026)
        return f"{os.path.basename(path)}  ← {anchor!r}" if anchor else os.path.basename(path)

    async def validate_input(self, input: dict[str, Any], ctx: ToolContext) -> tuple[bool, str | None]:
        raw_path = str(input.get("file_path", ""))
        old_str = str(input.get("old_string", ""))
        new_str = str(input.get("new_string", ""))
        path = os.path.normpath(os.path.join(ctx.cwd, raw_path) if not os.path.isabs(raw_path) else raw_path)

        size_error = _edit_size_error(path)
        if size_error:
            return False, size_error
        if old_str == new_str:
            return False, "No changes to make: old_string and new_string are exactly the same."
        if path.endswith(".ipynb"):
            return False, "File is a Jupyter Notebook. Use the NotebookEdit tool to edit this file."
        if old_str != "" and os.path.exists(path):
            cached = ctx.file_state_cache.get(path)
            if cached is None or cached.is_partial_view:
                return False, "File has not been read yet. Read it first before writing to it."
        return True, None

    async def call(self, input: dict, ctx: ToolContext) -> str:
        raw_path: str = input["file_path"]
        old_str: str = input["old_string"]
        new_str: str = input["new_string"]
        replace_all: bool = bool(input.get("replace_all", False))

        if not os.path.isabs(raw_path):
            path = os.path.join(ctx.cwd, raw_path)
        else:
            path = raw_path
        path = os.path.normpath(path)

        size_error = _edit_size_error(path)
        if size_error:
            return size_error

        is_markdown = re.search(r"\.(md|mdx)$", path, re.IGNORECASE) is not None
        effective_new = new_str if is_markdown else _strip_trailing_whitespace(new_str)

        if not os.path.exists(path):
            if old_str != "":
                return f"File not found: {path}"
            try:
                os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
                _write_atomic(path, effective_new, encoding="utf-8")
                mtime = os.path.getmtime(path)
            except Exception as e:
                return f"Failed to write file: {e}"
            ctx.file_state_cache.set(
                path,
                FileState(
                    content=effective_new,
                    offset=None,
                    limit=None,
                    is_partial_view=False,
                    mtime_at_read=mtime,
                ),
            )
            return f"Created {os.path.basename(path)} with {effective_new.count(chr(10)) + 1} lines"
        if os.path.isdir(path):
            return f"{path} is a directory, not a file."

        if old_str == "":
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    existing_content = f.read()
            except Exception as e:
                return f"Failed to read file: {e}"
            if existing_content.strip() != "":
                return "Cannot create new file - file already exists."
            try:
                _write_atomic(path, effective_new, encoding="utf-8")
                mtime = os.path.getmtime(path)
            except Exception as e:
                return f"Failed to write file: {e}"
            ctx.file_state_cache.set(
                path,
                FileState(
                    content=effective_new,
                    offset=None,
                    limit=None,
                    is_partial_view=False,
                    mtime_at_read=mtime,
                ),
            )
            return f"Edit applied to {os.path.basename(path)}: replaced 1 occurrence (+{effective_new.count(chr(10))} lines)"

        # ── Must-read-before-edit check (mirrors readFileState guard in source) ─
        # Source (FileEditTool.tsx): if (!readFileState.has(filePath)) → error.
        # Enforces that the model has seen the current file content before patching
        # it, preventing blind edits on files it has never read.
        cached = ctx.file_state_cache.get(path)
        if cached is None:
            return (
                "You must read the file with the Read tool before editing it.\n"
                "This ensures you have the current file content before making changes."
            )
        if cached.is_partial_view:
            return (
                "File has not been fully read yet. Read the full file before editing it.\n"
                "This ensures you have the current complete file content before making changes."
            )

        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                original_content = f.read()
        except Exception as e:
            return f"Failed to read file: {e}"

        # ── Concurrent modification check ──────────────────────────────────────
        # Source: detect when the file was modified on disk between the Read and
        # the Edit calls (another process or tool changed it).
        if cached.mtime_at_read is not None:
            try:
                current_mtime = os.path.getmtime(path)
                if current_mtime > cached.mtime_at_read + 0.001:  # 1 ms tolerance
                    is_full_read = cached.offset is None and cached.limit is None
                    if is_full_read and original_content == cached.content:
                        pass
                    else:
                        ctx.file_state_cache.delete(path)
                        return (
                            f"The file '{os.path.basename(path)}' has been modified on disk "
                            "since you last read it. Re-read the file with the Read tool "
                            "to get the current content before editing."
                        )
            except OSError:
                pass  # can't stat — proceed, let the read below surface any error

        original_ending = _detect_line_ending(original_content)

        actual_old = _find_actual_string(original_content, old_str)
        if actual_old is None:
            return (
                "old_string not found in file. Use Read to verify the "
                "exact content before editing.\n"
                "Tip: Copy the exact text from the Read output, including all "
                "indentation and whitespace."
            )
        actual_new = _preserve_quote_style(old_str, actual_old, effective_new)

        count = original_content.count(actual_old)
        if count > 1 and not replace_all:
            return (
                f"Found {count} matches of the string to replace, but "
                "replace_all is false. To replace all occurrences, set "
                "replace_all to true. To replace only one occurrence, please "
                "provide more context to uniquely identify the instance.\n"
                f"String: {actual_old[:100]!r}"
            )

        if actual_old == actual_new and not replace_all:
            return "old_string and new_string are identical — no change would be made."

        if replace_all:
            new_content = original_content.replace(actual_old, actual_new)
        else:
            target = actual_old
            if (
                actual_new == ""
                and not actual_old.endswith("\n")
                and original_content.find(actual_old + "\n") != -1
            ):
                target = actual_old + "\n"
            new_content = original_content.replace(target, actual_new, 1)

        if new_content == original_content:
            return "Edit did not change the file content. Check that old_string is correct."

        if original_ending == "\r\n" and "\r\n" not in new_content:
            new_content = _normalize_line_endings(new_content, "\r\n")

        try:
            _write_atomic(path, new_content, encoding="utf-8")
        except Exception as e:
            return f"Failed to write file: {e}"

        # ── Invalidate cache entry after successful write ───────────────────────
        # Refresh read state after a successful write, mirroring the source
        # readFileState.set(filePath, { content: updatedFile, ... }) path.
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = None
        ctx.file_state_cache.set(
            path,
            FileState(
                content=new_content,
                offset=None,
                limit=None,
                is_partial_view=False,
                mtime_at_read=mtime,
            ),
        )

        lines_changed = actual_new.count("\n") - old_str.count("\n")
        occurrences = count if replace_all else 1
        occ_str = f"{occurrences} occurrence{'s' if occurrences != 1 else ''}"
        sign = "+" if lines_changed >= 0 else ""
        return (
            f"Edit applied to {os.path.basename(path)}: "
            f"replaced {occ_str} ({sign}{lines_changed} lines)"
        )
