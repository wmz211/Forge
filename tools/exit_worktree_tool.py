from __future__ import annotations
from tool import Tool, ToolContext

EXIT_WORKTREE_TOOL_NAME = "ExitWorktree"

_DESCRIPTION = (
    "Exit the current git worktree and return to the original working directory. "
    "Either keep the worktree on disk or remove it along with its branch."
)

_PROMPT = """\
Exits the worktree session created by EnterWorktree and restores the original
working directory.

Parameters:
  action: "keep" | "remove"
    - "keep": leave the worktree and its branch on disk for later use.
    - "remove": delete the worktree directory and its branch. If there are
      uncommitted changes or unmerged commits you must also pass
      discard_changes: true or the tool will refuse and list them.
  discard_changes (optional bool): Required true when action is "remove" and
    the worktree has uncommitted files or commits not present on the base branch.
"""


class ExitWorktreeTool(Tool):
    """
    Mirrors ExitWorktreeTool in src/tools/ExitWorktreeTool/ExitWorktreeTool.ts.

    Input schema:
      action: "keep" | "remove"
      discard_changes: bool (optional) — required true when removing a dirty worktree.

    Output:
      action, originalCwd, worktreePath, message
    """

    name = EXIT_WORKTREE_TOOL_NAME
    description = _DESCRIPTION
    should_defer = True

    def get_schema(self) -> dict:
        return {
            "name": self.name,
            "description": _DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["keep", "remove"],
                        "description": (
                            '"keep" leaves the worktree and branch on disk; '
                            '"remove" deletes both.'
                        ),
                    },
                    "discard_changes": {
                        "type": "boolean",
                        "description": (
                            "Required true when action is \"remove\" and the worktree has "
                            "uncommitted files or unmerged commits. The tool will refuse "
                            "and list them otherwise."
                        ),
                    },
                },
                "required": ["action"],
            },
        }

    async def call(self, tool_input: dict, ctx: ToolContext) -> str:
        from tools.agent_tool.worktree import cleanup_worktree, keep_worktree
        action = tool_input.get("action")
        discard = tool_input.get("discard_changes", False)

        worktree_path = getattr(ctx, "worktree_path", None)
        if not worktree_path:
            raise ValueError("Not currently in a worktree session. Call EnterWorktree first.")

        original_cwd = getattr(ctx, "original_cwd", ctx.cwd)

        if action == "keep":
            await keep_worktree(worktree_path)
            ctx.cwd = original_cwd
            ctx.worktree_path = None
            return (
                f"Worktree kept at {worktree_path}. "
                f"Returned to original directory {original_cwd}."
            )
        elif action == "remove":
            removed = await cleanup_worktree(worktree_path, force=discard)
            ctx.cwd = original_cwd
            ctx.worktree_path = None
            return (
                f"Worktree at {worktree_path} removed. "
                f"Returned to original directory {original_cwd}."
            )
        else:
            raise ValueError(f"Unknown action '{action}'. Must be 'keep' or 'remove'.")
