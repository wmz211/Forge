from __future__ import annotations
from tool import Tool, ToolContext

ENTER_PLAN_MODE_TOOL_NAME = "EnterPlanMode"

_DESCRIPTION = (
    "Requests permission to enter plan mode for complex tasks requiring "
    "exploration and design before implementation."
)

_PROMPT = """\
Use this tool when you need to switch into plan mode to think through a complex
task before executing it. Plan mode lets you explore the codebase, gather
context, and produce a detailed plan that the user can review and approve before
any changes are made.

Call this tool with no arguments. After calling it you will be in plan mode and
should use available read-only tools to explore, then call ExitPlanMode with
your completed plan.

Notes:
- Cannot be used inside sub-agent contexts.
- In plan mode you should NOT make any file edits or run write commands.
"""


class EnterPlanModeTool(Tool):
    """
    Mirrors EnterPlanModeTool in src/tools/EnterPlanModeTool/EnterPlanModeTool.ts.
    No input parameters. Returns a confirmation message and sets plan mode on
    the session context.
    """

    name = ENTER_PLAN_MODE_TOOL_NAME
    description = _DESCRIPTION
    is_concurrency_safe = True
    should_defer = True

    def get_schema(self) -> dict:
        return {
            "name": self.name,
            "description": _DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        }

    async def call(self, tool_input: dict, ctx: ToolContext) -> str:
        if getattr(ctx, "agent_id", None):
            raise ValueError("EnterPlanMode tool cannot be used in agent contexts")
        ctx.plan_mode = True
        return "Plan mode activated. Use read-only tools to explore the codebase, then call ExitPlanMode with your completed plan."

    def is_read_only(self, input: dict | None = None) -> bool:
        return True
