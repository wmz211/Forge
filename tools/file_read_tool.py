from __future__ import annotations
import json
import os
import re
import sys
from typing import Any

from tool import Tool, ToolContext
from utils.file_state_cache import FileState

# ── Limits (mirrors src/tools/FileReadTool/limits.ts + src/utils/file.ts) ──

# MAX_OUTPUT_SIZE = 0.25 * 1024 * 1024 in file.ts
MAX_SIZE_BYTES: int = int(os.environ.get("CLAUDE_CODE_FILE_READ_MAX_SIZE_BYTES", str(256 * 1024)))

# DEFAULT_MAX_OUTPUT_TOKENS = 25000; at ~4 chars/token → ~100 000 chars
# We use a char-based cap that's close to this. Overridable via env.
_env_max_chars = os.environ.get("CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS")
MAX_OUTPUT_CHARS: int = (int(_env_max_chars) * 4) if _env_max_chars else 100_000

# MAX_LINES_TO_READ = 2000 in prompt.ts — default read window when no limit given
MAX_LINES_TO_READ: int = 2000

# ── Device file blacklist (mirrors BLOCKED_DEVICE_PATHS in FileReadTool.ts) ─

_BLOCKED_DEVICE_PATHS: frozenset[str] = frozenset({
    # Infinite output — never reach EOF
    "/dev/zero", "/dev/random", "/dev/urandom", "/dev/full",
    # Blocks waiting for input
    "/dev/stdin", "/dev/tty", "/dev/console",
    # Nonsensical to read
    "/dev/stdout", "/dev/stderr",
    # fd aliases for stdin/stdout/stderr
    "/dev/fd/0", "/dev/fd/1", "/dev/fd/2",
})

_PROC_FD_RE = re.compile(r"^/proc/[^/]+/fd/[012]$")


def _is_blocked_device(path: str) -> bool:
    """
    Returns True if path points to a device that would block or loop forever.
    Mirrors isBlockedDevicePath() in FileReadTool.ts.
    """
    if path in _BLOCKED_DEVICE_PATHS:
        return True
    # /proc/self/fd/0-2 and /proc/<pid>/fd/0-2 (Linux stdio aliases)
    if path.startswith("/proc/") and _PROC_FD_RE.match(path):
        return True
    return False


# ── Binary extension set (mirrors BINARY_EXTENSIONS in src/constants/files.ts) ─

# Source excludes PDF and image extensions from this check at call-site;
# our Python version handles image/PDF as "unsupported but with a clear message".
_BINARY_EXTENSIONS: frozenset[str] = frozenset({
    # Images (FileReadTool.ts renders these natively; we don't)
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tiff", ".tif",
    # Videos
    ".mp4", ".mov", ".avi", ".mkv", ".webm", ".wmv", ".flv", ".m4v", ".mpeg", ".mpg",
    # Audio
    ".mp3", ".wav", ".ogg", ".flac", ".aac", ".m4a", ".wma", ".aiff", ".opus",
    # Archives
    ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar", ".xz", ".z", ".tgz", ".iso",
    # Executables / compiled objects
    ".exe", ".dll", ".so", ".dylib", ".bin", ".o", ".a", ".obj", ".lib",
    ".app", ".msi", ".deb", ".rpm",
    # Documents (non-text)
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".odt", ".ods", ".odp",
    # Fonts
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    # Bytecode / VM artifacts
    ".pyc", ".pyo", ".class", ".jar", ".war", ".ear",
    ".node", ".wasm", ".rlib",
    # Database
    ".sqlite", ".sqlite3", ".db", ".mdb", ".idx",
    # Design / 3D
    ".psd", ".ai", ".eps", ".sketch", ".fig", ".xd",
    ".blend", ".3ds", ".max",
    # Flash
    ".swf", ".fla",
    # Lock / profiling
    ".lockb", ".dat", ".data",
})

# Image extensions FileReadTool.ts renders natively (we show a note instead)
_IMAGE_EXTENSIONS: frozenset[str] = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp",
})

# Notebook extensions — source has special handling; we show a note
_NOTEBOOK_EXTENSIONS: frozenset[str] = frozenset({".ipynb"})


def _has_binary_extension(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    return ext in _BINARY_EXTENSIONS


# ── Tool ────────────────────────────────────────────────────────────────────

class FileReadTool(Tool):
    name = "Read"
    description = (
        "Reads a file from the local filesystem. You can access any file directly by "
        "using this tool.\n"
        "Assume this tool is able to read all files on the machine. If the User provides "
        "a path to a file assume that path is valid. It is okay to read a file that does "
        "not exist; an error will be returned.\n\n"
        "Usage:\n"
        "- The file_path parameter must be an absolute path, not a relative path\n"
        f"- By default, it reads up to {MAX_LINES_TO_READ} lines starting from the "
        "beginning of the file\n"
        "- When you already know which part of the file you need, only read that part. "
        "This can be important for larger files.\n"
        "- Results are returned using cat -n format, with line numbers starting at 1\n"
        "- This tool can only read files, not directories. To read a directory, use an "
        "ls command via the Bash tool.\n"
        "- This tool can read Jupyter notebooks (.ipynb files) and returns all cells "
        "with their outputs.\n"
        "- You will regularly be asked to read screenshots. If the user provides a path "
        "to a screenshot, ALWAYS use this tool to view the file at the path.\n"
        "- If you read a file that exists but has empty contents you will receive a "
        "system reminder warning in place of file contents."
    )
    is_concurrency_safe = True

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "The absolute path to the file to read",
                },
                "offset": {
                    "type": "integer",
                    "description": (
                        "The line number to start reading from. "
                        "Only provide if the file is too large to read at once"
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "The number of lines to read. "
                        "Only provide if the file is too large to read at once."
                    ),
                },
                "pages": {
                    "type": "string",
                    "description": (
                        'Page range for PDF files (e.g., "1-5", "3", "10-20"). '
                        "Only applicable to PDF files. Maximum 20 pages per request."
                    ),
                },
            },
            "required": ["file_path"],
        }

    def render_call_summary(self, args: dict[str, Any]) -> str | None:
        """Show the file path (relative if possible), mirroring getToolUseSummary()."""
        path = args.get("file_path", "")
        return os.path.basename(path) or path

    async def call(self, input: dict[str, Any], ctx: ToolContext) -> str:
        raw_path: str = input["file_path"]

        # Resolve relative paths against cwd
        if not os.path.isabs(raw_path):
            path = os.path.join(ctx.cwd, raw_path)
        else:
            path = raw_path

        # Normalise (resolve . and ..)
        path = os.path.normpath(path)

        # ── Device file check (mirrors isBlockedDevicePath()) ──────────────
        # Use forward-slash form for the check (BLOCKED_DEVICE_PATHS uses POSIX paths)
        posix_path = path.replace("\\", "/")
        if _is_blocked_device(posix_path) or _is_blocked_device(path):
            return (
                f"<error>Cannot read '{raw_path}': this device file would block "
                "or produce infinite output.</error>"
            )

        # ── Existence / type checks ────────────────────────────────────────
        if not os.path.exists(path):
            # Try to suggest a similar file (mirrors suggestPathUnderCwd())
            suggestion = _suggest_similar(path, ctx.cwd)
            msg = f"<error>File not found: {path}."
            if suggestion:
                msg += f" Did you mean {suggestion}?"
            return msg + "</error>"

        if os.path.isdir(path):
            return f"<error>{path} is a directory, not a file.</error>"

        # ── Binary extension check (mirrors hasBinaryExtension()) ──────────
        ext = os.path.splitext(path)[1].lower()
        if ext in _IMAGE_EXTENSIONS:
            result = _read_image_metadata(path)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                mtime = None
            ctx.file_state_cache.set(
                path,
                FileState(
                    content=result,
                    offset=None,
                    limit=None,
                    is_partial_view=True,
                    mtime_at_read=mtime,
                ),
            )
            return result
        if ext in _NOTEBOOK_EXTENSIONS:
            result = _read_notebook(path)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                mtime = None
            ctx.file_state_cache.set(
                path,
                FileState(
                    content=result,
                    offset=None,
                    limit=None,
                    is_partial_view=False,
                    mtime_at_read=mtime,
                ),
            )
            return result
        if ext == ".pdf":
            return _read_pdf(path, input.get("pages"))
        if _has_binary_extension(path):
            return (
                f"<error>This tool cannot read binary files. "
                f"The file appears to be a binary {ext or 'unknown'} file. "
                "Please use appropriate tools for binary file analysis.</error>"
            )

        # ── File size guard (mirrors maxSizeBytes = 256 KB) ───────────────
        try:
            file_size = os.path.getsize(path)
        except OSError as e:
            return f"<error>Cannot stat file: {e}</error>"

        has_window = input.get("offset") is not None or input.get("limit") is not None
        if file_size > MAX_SIZE_BYTES and not has_window:
            # Source throws; we block with an informative message
            return (
                f"<error>File too large to read ({file_size:,} bytes, "
                f"max {MAX_SIZE_BYTES:,} bytes = {MAX_SIZE_BYTES // 1024} KB). "
                "Use offset and limit to read specific portions of the file, "
                "or search for specific content with Grep.</error>"
            )

        # ── Read the file ──────────────────────────────────────────────────
        # offset is 1-based line number (matches source schema description)
        offset: int = int(input.get("offset") or 1)
        limit: int | None = input.get("limit")
        if limit is not None:
            limit = int(limit)

        # Clamp offset to valid range
        offset = max(1, offset)
        start = offset - 1  # 0-based index

        requested_limit = MAX_LINES_TO_READ if limit is None else max(0, limit)
        selected: list[str] = []
        total_lines = 0
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line_no, line in enumerate(f, start=1):
                    total_lines = line_no
                    if line_no < offset:
                        continue
                    if len(selected) < requested_limit:
                        selected.append(line)
        except Exception as e:
            return f"<error>{e}</error>"

        end = start + len(selected)

        if not selected and total_lines > 0:
            return (
                f"<error>Offset {offset} is beyond the end of the file "
                f"({total_lines} lines total).</error>"
            )

        # ── Format: cat -n style (line_number TAB content) ─────────────────
        # Mirrors addLineNumbers() in file.ts
        raw_selected = "".join(selected)
        numbered = "".join(f"{start + i + 1}\t{line}" for i, line in enumerate(selected))

        # ── Char-based output cap (mirrors maxTokens * 4 chars/token) ──────
        if len(numbered) > MAX_OUTPUT_CHARS:
            numbered = numbered[:MAX_OUTPUT_CHARS]
            # Ensure we don't cut mid-line
            last_newline = numbered.rfind("\n")
            if last_newline > 0:
                numbered = numbered[:last_newline]
            numbered += f"\n... [truncated, file has {total_lines} lines total]"

        if not numbered:
            return "<system>File exists but has empty contents.</system>"

        # Append a partial-view note if we didn't read the whole file
        is_partial = (start > 0) or (end < total_lines)
        if is_partial:
            numbered += (
                f"\n[Showing lines {start + 1}-{end} of {total_lines}. "
                "Use offset/limit to read other portions.]"
            )
        if False and is_partial:
            numbered += (
                f"\n[Showing lines {start + 1}–{end} of {total_lines}. "
                "Use offset/limit to read other portions.]"
            )

        # ── Record in file state cache (mirrors readFileState.set() in source) ─
        # FileEditTool consults this cache to enforce must-read-before-edit and
        # to detect concurrent disk modifications between read and edit.
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = None
        ctx.file_state_cache.set(
            path,
            FileState(
                content=raw_selected,
                offset=offset if start > 0 else None,
                limit=limit,
                is_partial_view=is_partial,
                mtime_at_read=mtime,
            ),
        )

        return numbered


def _read_image_metadata(path: str) -> str:
    """
    Return human-readable metadata for an image file.
    Mirrors ImageProcessor.ts — source uses `sharp` to resize and return base64;
    we return metadata since terminal mode can't render images visually.
    """
    size_bytes = os.path.getsize(path)
    ext = os.path.splitext(path)[1].upper().lstrip(".")
    try:
        from PIL import Image
        with Image.open(path) as img:
            w, h = img.size
            fmt = img.format or ext
            mode = img.mode
        return (
            f"[Image: {fmt} {w}×{h}px {mode} {size_bytes:,} bytes]\n"
            f"Note: Image rendering is not supported in terminal mode. "
            f"This is a {fmt} image ({w}×{h} pixels, {mode} colour space)."
        )
    except ImportError:
        return (
            f"[Image: {ext} {size_bytes:,} bytes]\n"
            f"Note: Install Pillow (`pip install Pillow`) to see image dimensions. "
            f"Image rendering is not supported in terminal mode."
        )
    except Exception as e:
        return f"<error>Cannot read image metadata for '{path}': {e}</error>"


def _read_pdf(path: str, pages: Any = None) -> str:
    """
    Best-effort PDF text extraction.  The TypeScript runtime can hand PDFs to
    richer processors; this port extracts text when a local PDF library exists.
    """
    try:
        try:
            from pypdf import PdfReader  # type: ignore
        except ImportError:
            from PyPDF2 import PdfReader  # type: ignore
    except ImportError:
        return (
            "<error>PDF reading requires pypdf or PyPDF2 in this Python runtime. "
            "Install one of those packages or extract text with an external tool.</error>"
        )

    try:
        reader = PdfReader(path)
        selected_pages = _parse_page_range(str(pages or ""), len(reader.pages))
        chunks: list[str] = []
        for page_index in selected_pages:
            text = reader.pages[page_index].extract_text() or ""
            chunks.append(f'<page number="{page_index + 1}">\n{text}\n</page>')
        return "\n".join(chunks) if chunks else "<system>PDF has no extractable text.</system>"
    except Exception as e:
        return f"<error>Cannot read PDF '{path}': {e}</error>"


def _parse_page_range(raw: str, total_pages: int) -> list[int]:
    if total_pages <= 0:
        return []
    if not raw.strip():
        return list(range(min(total_pages, 20)))

    pages: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            left, right = part.split("-", 1)
            start = max(1, int(left.strip() or "1"))
            end = min(total_pages, int(right.strip() or str(total_pages)))
            pages.extend(range(start - 1, end))
        else:
            page = int(part)
            if 1 <= page <= total_pages:
                pages.append(page - 1)
        if len(pages) >= 20:
            break

    seen: set[int] = set()
    out: list[int] = []
    for page in pages:
        if page not in seen:
            seen.add(page)
            out.append(page)
    return out[:20]


_NOTEBOOK_OUTPUT_MAX_CHARS = 10_000  # mirrors notebook.ts large-output threshold


def _read_notebook(path: str) -> str:
    """
    Parse a Jupyter notebook (.ipynb) and return its cells as XML-tagged text.
    Mirrors src/utils/notebook.ts readNotebook() / formatCell().

    Cell format:
      <cell id="<id>"><cell_type>markdown</cell_type>cell source</cell id="<id>">
      Code cells omit <cell_type>; non-python kernels add <language>.
      Outputs are appended as text; large outputs (>10 000 chars) replaced with jq hint.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            nb = json.load(f)
    except Exception as e:
        return f"<error>Cannot parse notebook '{path}': {e}</error>"

    lang = (
        nb.get("metadata", {})
          .get("language_info", {})
          .get("name", "python")
          .lower()
    )
    cells = nb.get("cells", [])
    parts = []

    for idx, cell in enumerate(cells):
        cell_type = cell.get("cell_type", "code")
        source_raw = cell.get("source", [])
        source = "".join(source_raw) if isinstance(source_raw, list) else str(source_raw)
        cell_id = cell.get("id", f"cell-{idx}")

        # Metadata prefix — mirrors notebook.ts conditional metadata injection
        meta = ""
        if cell_type != "code":
            meta = f"<cell_type>{cell_type}</cell_type>\n"
        elif lang and lang != "python":
            meta = f"<language>{lang}</language>\n"

        cell_content = meta + source

        # Outputs for code cells
        if cell_type == "code":
            output_texts: list[str] = []
            for out in cell.get("outputs", []):
                out_type = out.get("output_type", "")
                if out_type == "error":
                    ename = out.get("ename", "Error")
                    evalue = out.get("evalue", "")
                    output_texts.append(f"{ename}: {evalue}")
                elif out_type in ("stream", "execute_result", "display_data"):
                    data = out.get("data", {})
                    text_raw = out.get("text") or data.get("text/plain", [])
                    text = "".join(text_raw) if isinstance(text_raw, list) else str(text_raw)
                    if text:
                        output_texts.append(text)
                    # Note image outputs without embedding raw base64
                    if "image/png" in data or "image/jpeg" in data:
                        output_texts.append("[image output]")

            if output_texts:
                combined = "\n".join(output_texts)
                if len(combined) > _NOTEBOOK_OUTPUT_MAX_CHARS:
                    combined = (
                        f"Outputs too large. Use BashTool with: "
                        f"jq '.cells[{idx}].outputs' \"{path}\""
                    )
                cell_content += "\n" + combined

        parts.append(f'<cell id="{cell_id}">{cell_content}</cell id="{cell_id}">')

    if not parts:
        return f"<result>Notebook '{path}' has no cells.</result>"

    return "\n".join(parts)


def _suggest_similar(missing_path: str, cwd: str) -> str | None:
    """
    Try to find a file with a similar name under cwd.
    Simplified version of findSimilarFile() / suggestPathUnderCwd() in source.
    """
    target_name = os.path.basename(missing_path).lower()
    if not target_name:
        return None
    best: str | None = None
    best_score = 0
    try:
        for dirpath, dirnames, filenames in os.walk(cwd):
            # Skip VCS dirs
            dirnames[:] = [d for d in dirnames if d not in {".git", ".svn", ".hg"}]
            for fname in filenames:
                score = _name_similarity(target_name, fname.lower())
                if score > best_score:
                    best_score = score
                    best = os.path.join(dirpath, fname)
            if best_score > 0.8:
                break  # Good enough, stop early
    except OSError:
        return None
    return best if best_score > 0.5 else None


def _name_similarity(a: str, b: str) -> float:
    """Simple character-overlap similarity (0–1)."""
    if not a or not b:
        return 0.0
    # Longest common subsequence length / max length
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    common = sum(1 for c in shorter if c in longer)
    return common / len(longer)
