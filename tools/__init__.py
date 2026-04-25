from .bash_tool import BashTool
from .file_read_tool import FileReadTool
from .file_edit_tool import FileEditTool
from .file_write_tool import FileWriteTool
from .glob_tool import GlobTool
from .grep_tool import GrepTool
from .agent_tool import AgentTool
from .web_fetch_tool import WebFetchTool
from .web_search_tool import WebSearchTool
from .powershell_tool import PowerShellTool
from .todo_write_tool import TodoWriteTool
from .tool_search_tool import ToolSearchTool
from .ask_user_question_tool import AskUserQuestionTool
from .sleep_tool import SleepTool
from .notebook_edit_tool import NotebookEditTool
from .task_tools import (
    TaskCreateTool,
    TaskUpdateTool,
    TaskGetTool,
    TaskListTool,
    TaskStopTool,
    TaskOutputTool,
)
from .mcp_tool import McpTool
from services.mcp import discover_mcp_tools


def _build_static_tools() -> list:
    return [
        BashTool(),
        FileReadTool(),
        FileEditTool(),
        FileWriteTool(),
        GlobTool(),
        GrepTool(),
        WebFetchTool(),
        WebSearchTool(),
        PowerShellTool(),
        TodoWriteTool(),
        # Deferred tools - discovered via ToolSearch, not sent to the API by default.
        AskUserQuestionTool(),
        SleepTool(),
        NotebookEditTool(),
        TaskCreateTool(),
        TaskUpdateTool(),
        TaskGetTool(),
        TaskListTool(),
        TaskStopTool(),
        TaskOutputTool(),
    ]


async def build_mcp_tools(cwd: str | None) -> list:
    if not cwd:
        return []
    return [McpTool(item["server"], item["tool"]) for item in await discover_mcp_tools(cwd)]


async def build_builtin_tools_async(
    include_tool_search: bool = True,
    cwd: str | None = None,
) -> list:
    tools = _build_static_tools()
    tools.extend(await build_mcp_tools(cwd))
    if include_tool_search:
        tool_search = ToolSearchTool()
        tools.append(tool_search)
        tool_search.set_tools(tools)
    return tools


def build_builtin_tools(include_tool_search: bool = True, cwd: str | None = None) -> list:
    if cwd is not None:
        try:
            import asyncio
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(build_builtin_tools_async(include_tool_search, cwd))
        raise RuntimeError("Use build_builtin_tools_async() when an event loop is already running")
    tools = _build_static_tools()
    if include_tool_search:
        tool_search = ToolSearchTool()
        tools.append(tool_search)
        tool_search.set_tools(tools)
    return tools


def core_tools_for_api(tools: list) -> list:
    """
    Return the model-facing core tool set when ToolSearch is available.
    Deferred tools stay executable in the runtime tool pool but are meant to be
    discovered through ToolSearch.
    """
    has_tool_search = any(getattr(t, "name", "") == "ToolSearch" for t in tools)
    if not has_tool_search:
        return tools
    return [
        t for t in tools
        if getattr(t, "always_load", False) or not getattr(t, "should_defer", False)
    ]


__all__ = [
    "BashTool",
    "FileReadTool",
    "FileEditTool",
    "FileWriteTool",
    "GlobTool",
    "GrepTool",
    "AgentTool",
    "WebFetchTool",
    "WebSearchTool",
    "PowerShellTool",
    "TodoWriteTool",
    "ToolSearchTool",
    "AskUserQuestionTool",
    "SleepTool",
    "NotebookEditTool",
    "TaskCreateTool",
    "TaskUpdateTool",
    "TaskGetTool",
    "TaskListTool",
    "TaskStopTool",
    "TaskOutputTool",
    "McpTool",
    "build_mcp_tools",
    "build_builtin_tools_async",
    "build_builtin_tools",
    "core_tools_for_api",
]
