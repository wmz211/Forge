from __future__ import annotations
import re
from tool import Tool, ToolContext

ENTER_WORKTREE_TOOL_NAME = "EnterWorktree"

_DESCRIPTION = (
    "Creates an isolated git worktree and switches the session into it. "
    "Use this when you need to make changes on a separate branch without "
    "affecting the current working tree."
)

_PROMPT = """\
Creates a new git worktree at a temporary path and switches the active working
directory to it. The new worktree starts on a fresh branch derived from the
current HEAD.

Parameters:
  name (optional): A slug for the worktree. Each "/"-separated segment may
    contain only letters, digits, dots, underscores, and dashes; max 64 chars
    total. A random name is generated if not provided.

After calling this tool, all subsequent file operations and shell commands will
run inside the new worktree. Use ExitWorktree to return.

Only one worktree session can be active at a time.
"""

_VALID_SLUG_RE = re.compile(r'^[A-Za-z0-9._-]{1,64}(/[A-Za-z0-9._-]{1,64})*$')


class EnterWorktreeTool(Tool):
    """
    Mirrors EnterWorktreeTool in src/tools/EnterWorktreeTool/EnterWorktreeTool.ts.

    Input schema:
      name: optional str — slug for the worktree directory name.

    Output:
      worktreePath: str
      worktreeBranch: str (optional)
      message: str
    """

    name = ENTER_WORKTREE_TOOL_NAME
    description = _DESCRIPTION
    should_defer = True

    def get_schema(self) -> dict:
        return {
            "name": self.name,
            "description": _DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": (
                            "Optional name for the worktree. Each '/'-separated segment may "
                            "contain only letters, digits, dots, underscores, and dashes; "
                            "max 64 chars total. A random name is generated if not provided."
                        ),
                    },
                },
                "required": [],
            },
        }

    async def validate_input(self, tool_input: dict, ctx: ToolContext):
        name = tool_input.get("name")
        if name is not None:
            if not _VALID_SLUG_RE.match(name):
                return False, (
                    f"Invalid worktree name '{name}'. Each segment may only contain "
                    "letters, digits, dots, underscores, and dashes; max 64 chars total."
                )
        return True, None

    async def call(self, tool_input: dict, ctx: ToolContext) -> str:
        from tools.agent_tool.worktree import create_worktree_for_session
        name = tool_input.get("name")
        worktree_path, branch = await create_worktree_for_session(ctx.cwd, name)
        ctx.worktree_path = worktree_path
        ctx.original_cwd = ctx.cwd
        ctx.cwd = worktree_path
        return (
            f"Created worktree at {worktree_path}"
            + (f" on branch {branch}" if branch else "")
            + ". The session is now working inside this worktree."
        )
