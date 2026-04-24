"""
ui.py — Terminal UI for Forge.
Mirrors Claude Code's React/Ink rendering as closely as possible in a Python CLI.

Visual conventions (from Claude Code source):
  ●  (BLACK_CIRCLE) — prefix before each assistant message block
  ⎿  — indentation character for tool call display
  Markdown — assistant text rendered via rich.Markdown
  Spinner  — "Thinking…" while waiting for first token
  Dimmed   — tool results shown in gray/dim
"""
from __future__ import annotations
import sys
import time
import threading
import shutil
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text
from rich.style import Style

# ── Platform-specific constants (mirrors figures.ts) ─────────────────────────
# Claude Code: BLACK_CIRCLE = env.platform === 'darwin' ? '⏺' : '●'
BLACK_CIRCLE = "⏺" if sys.platform == "darwin" else "●"
TOOL_INDENT = "⎿"

# ── Shared rich console ───────────────────────────────────────────────────────
console = Console(highlight=False, markup=False)

# ── Color palette ─────────────────────────────────────────────────────────────
STYLE_BULLET   = Style(color="bright_cyan", bold=True)
STYLE_TOOL_HDR = Style(color="blue")
STYLE_TOOL_RES = Style(color="bright_black", dim=True)
STYLE_ERR      = Style(color="red")
STYLE_WARN     = Style(color="yellow")
STYLE_SUCCESS  = Style(color="green")
STYLE_DIM      = Style(dim=True)


# ── Spinner ───────────────────────────────────────────────────────────────────

class Spinner:
    """
    Lightweight spinner that prints a "Thinking…" animation while the agent
    is waiting for the first LLM token. Runs in a background daemon thread.
    """
    _FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, label: str = "Thinking"):
        self._label = label
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=0.5)
            self._thread = None
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()

    def _spin(self) -> None:
        idx = 0
        while not self._stop_event.is_set():
            frame = self._FRAMES[idx % len(self._FRAMES)]
            sys.stdout.write(f"\r\033[2m{frame} {self._label}…\033[0m")
            sys.stdout.flush()
            idx += 1
            self._stop_event.wait(timeout=0.08)


# ── Rendering helpers ─────────────────────────────────────────────────────────

def _term_width() -> int:
    return shutil.get_terminal_size((80, 24)).columns


def render_assistant_bullet() -> None:
    """Print the ● bullet that precedes each assistant message block."""
    console.print(Text(f"\n{BLACK_CIRCLE}", style=STYLE_BULLET))


def render_text(text: str) -> None:
    """Render assistant text as Markdown (mirrors <Markdown> in AssistantTextMessage.tsx)."""
    console.print(Markdown(text, code_theme="monokai"))


def _format_args(name: str, arguments: dict | str, tools_by_name: dict) -> str:
    """
    Build a compact one-line arg display for a tool call.
    Delegates to tool.render_call_summary() when available (mirrors
    renderToolUseMessage() in Claude Code tool components).
    """
    tool = tools_by_name.get(name)
    if tool is not None:
        summary = tool.render_call_summary(arguments if isinstance(arguments, dict) else {})
        if summary is not None:
            return summary

    # Default: key=value pairs, large strings replaced by <N chars>
    if isinstance(arguments, dict):
        parts = []
        for k, v in arguments.items():
            if isinstance(v, str) and len(v) > 80:
                parts.append(f"{k}=<{len(v)} chars>")
            else:
                sv = repr(v) if isinstance(v, str) else str(v)
                if len(sv) > 80:
                    sv = sv[:77] + '…"'
                parts.append(f"{k}={sv}")
        return ", ".join(parts)
    return str(arguments)[:120]


def render_tool_use(
    name: str,
    arguments: dict | str,
    tools_by_name: dict | None = None,
) -> None:
    """
    Print a tool call header:  ⎿ ToolName(summary)
    Mirrors Claude Code's tool-call display in the terminal UI.
    """
    summary = _format_args(name, arguments, tools_by_name or {})
    max_w = _term_width() - 4
    header = f"{name}({summary})"
    if len(header) > max_w:
        header = header[:max_w - 1] + "…"

    line = Text()
    line.append(f"{TOOL_INDENT} ", style=STYLE_TOOL_HDR)
    line.append(header, style=STYLE_TOOL_HDR)
    console.print(line)


def render_tool_result(
    name: str, result: str, duration_ms: float | None = None
) -> None:
    """Print a tool result in dimmed style (first line, truncated)."""
    preview = result.strip().replace("\n", " ")
    max_preview = _term_width() - 6
    if len(preview) > max_preview:
        preview = preview[:max_preview - 1] + "…"

    line = Text()
    line.append("   ", style=STYLE_DIM)
    if duration_ms is not None:
        line.append(f"({duration_ms / 1000:.1f}s) ", style=STYLE_DIM)
    line.append(preview, style=STYLE_TOOL_RES)
    console.print(line)


def render_error(msg: str) -> None:
    console.print(Text(f"[Error] {msg}", style=STYLE_ERR))


def render_warn(msg: str) -> None:
    console.print(Text(msg, style=STYLE_WARN))


def render_info(msg: str) -> None:
    console.print(Text(msg, style=STYLE_DIM))


def render_done(reason: str) -> None:
    if reason != "stop":
        console.print(Text(f"\n[Done: {reason}]", style=STYLE_WARN))


# ── High-level event renderer ─────────────────────────────────────────────────

class EventRenderer:
    """
    Stateful renderer that processes query_loop events in Claude Code visual style.

    Usage:
        renderer = EventRenderer(tools=engine.tools)
        renderer.spinner.start()
        async for event in engine.submit_message(prompt):
            renderer.handle(event)
    """

    def __init__(self, tools: list | None = None) -> None:
        self.spinner = Spinner()
        self._tools_by_name: dict = {t.name: t for t in tools} if tools else {}
        self._first_token = True
        self._tool_start_times: dict[str, float] = {}

    def reset(self) -> None:
        self._first_token = True
        self._tool_start_times.clear()

    def handle(self, event: dict) -> None:
        t = event.get("type")

        if t == "text":
            if self._first_token:
                self._first_token = False
                self.spinner.stop()
                render_assistant_bullet()
            sys.stdout.write(event["content"])
            sys.stdout.flush()

        elif t == "thinking":
            # Qwen3 extended thinking blocks — rendered in dim italic so they're
            # visually distinct from the main assistant response.
            # Mirrors the thinking-block display in Claude Code's AssistantMessage.tsx.
            if self._first_token:
                self._first_token = False
                self.spinner.stop()
            content = event.get("content", "")
            if content:
                # Show a collapsible summary: first 120 chars + ellipsis if long
                preview = content.strip().replace("\n", " ")
                if len(preview) > 120:
                    preview = preview[:117] + "…"
                console.print(Text(f"  💭 {preview}", style=Style(dim=True, italic=True)))

        elif t == "tool_use":
            if self._first_token:
                self._first_token = False
                self.spinner.stop()
            sys.stdout.write("\n")
            render_tool_use(
                event["name"],
                event.get("arguments", {}),
                self._tools_by_name,
            )
            self._tool_start_times[event.get("id", event["name"])] = time.monotonic()

        elif t == "tool_result":
            key = event.get("id", event.get("name", ""))
            start = self._tool_start_times.pop(key, None)
            duration = (time.monotonic() - start) * 1000 if start else None
            render_tool_result(event["name"], event.get("result", ""), duration)
            self.spinner.start()
            self._first_token = True

        elif t == "done":
            self.spinner.stop()
            render_done(event.get("reason", "stop"))
            sys.stdout.write("\n")
            sys.stdout.flush()
