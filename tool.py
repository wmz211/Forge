from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from utils.file_state_cache import FileStateCache, create_empty_cache


@dataclass
class ToolContext:
    cwd: str
    permission_mode: str                    # external PermissionMode, e.g. default/plan/acceptEdits/dontAsk/bypassPermissions
    confirm_fn: Callable[[str, str, dict[str, Any] | None], bool]  # (tool_name, description, input) -> allowed
    additional_working_directories: list[str] = field(default_factory=list)
    todos: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    always_allow: list[Any] = field(default_factory=list)
    always_deny: list[Any] = field(default_factory=list)
    always_ask: list[Any] = field(default_factory=list)
    # Populated by QueryEngine so sub-agents can write sidechain transcripts
    # and metadata alongside the main session's JSONL file.
    # Mirrors toolUseContext.agentId / getSessionId() in runAgent.ts.
    session_id: str = ""
    session_transcript_path: Path = field(
        default_factory=lambda: Path.home() / ".claude" / "projects" / "unknown" / "session.jsonl"
    )
    # Tracks files read in this session — used by FileEditTool to enforce the
    # must-read-before-edit rule and detect concurrent disk modifications.
    # Mirrors readFileState: FileStateCache in QueryEngine.ts / toolUseContext.
    file_state_cache: FileStateCache = field(default_factory=create_empty_cache)


class Tool:
    name: str
    description: str
    is_concurrency_safe: bool = False  # True = read-only, can run in parallel
    max_result_size_chars: int = 30_000
    should_defer: bool = False
    always_load: bool = False
    aliases: tuple[str, ...] = ()
    search_hint: str = ""

    def is_concurrency_safe_for_input(self, input: dict[str, Any]) -> bool:
        """
        Per-input concurrency safety check.
        Mirrors isConcurrencySafe(input) in Tool.ts — the function form that
        partitionToolCalls calls with the parsed input.
        Default: return the class-level boolean (safe for all static tools).
        Tools like BashTool override this to inspect the actual command.
        """
        return self.is_concurrency_safe

    def is_read_only(self, input: dict[str, Any]) -> bool:
        """
        Mirrors Tool.isReadOnly(input).  Defaults to the concurrency flag because
        most static read tools are both read-only and concurrency-safe.
        """
        return self.is_concurrency_safe_for_input(input)

    def is_destructive(self, input: dict[str, Any]) -> bool:
        """Mirrors optional Tool.isDestructive(input)."""
        return False

    def interrupt_behavior(self) -> str:
        """Mirrors optional Tool.interruptBehavior(); source default is block."""
        return "block"

    async def validate_input(self, input: dict[str, Any], ctx: ToolContext) -> tuple[bool, str | None]:
        """
        Mirrors Tool.validateInput().  Tools override this for semantic checks
        that should happen before permission prompting.
        """
        return True, None

    def get_schema(self) -> dict:
        """Return OpenAI function-calling schema."""
        raise NotImplementedError

    async def call(self, input: dict[str, Any], ctx: ToolContext) -> str:
        raise NotImplementedError

    def render_call_summary(self, args: dict[str, Any]) -> str | None:
        """
        Optional: return a compact one-line summary for the UI tool-call display.
        Return None to use the default key=value formatting.
        Mirrors renderToolUseMessage() in Claude Code tool components.
        """
        return None

    def to_openai_tool(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.get_schema(),
            },
        }
