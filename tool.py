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


def validate_json_schema_value(value: Any, schema: dict, path: str = "input") -> str | None:
    """
    Lightweight validator for the JSON-schema subset used by tool parameters.
    Mirrors the source runtime's schema safeParse gate without adding a dependency.
    """
    if not isinstance(schema, dict):
        return None

    expected = schema.get("type")
    if expected is not None and not _matches_json_type(value, expected):
        expected_text = " or ".join(expected) if isinstance(expected, list) else str(expected)
        return f"{path} must be {expected_text}, got {_json_type_name(value)}"

    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        return f"{path} must be one of {enum!r}"

    if schema.get("type") == "object" or (
        isinstance(value, dict) and "properties" in schema
    ):
        if not isinstance(value, dict):
            return f"{path} must be object, got {_json_type_name(value)}"
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        for key in required:
            if key not in value:
                return f"{path}.{key} is required"
        for key, child_schema in properties.items():
            if key in value:
                error = validate_json_schema_value(value[key], child_schema, f"{path}.{key}")
                if error:
                    return error

    if schema.get("type") == "array" or (
        isinstance(value, list) and "items" in schema
    ):
        if not isinstance(value, list):
            return f"{path} must be array, got {_json_type_name(value)}"
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for idx, item in enumerate(value):
                error = validate_json_schema_value(item, item_schema, f"{path}[{idx}]")
                if error:
                    return error

    return None


def validate_tool_input_schema(tool: Tool, input: dict[str, Any]) -> tuple[bool, str | None]:
    try:
        schema = tool.get_schema()
    except Exception as exc:
        return False, f"Invalid tool schema for {tool.name}: {exc}"
    error = validate_json_schema_value(input, schema, "input")
    return (error is None, error)


def _matches_json_type(value: Any, expected: Any) -> bool:
    if isinstance(expected, list):
        return any(_matches_json_type(value, item) for item in expected)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return (isinstance(value, int) or isinstance(value, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def _json_type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return type(value).__name__
