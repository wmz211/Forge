from __future__ import annotations
from tool import Tool, ToolContext

EXIT_PLAN_MODE_TOOL_NAME = "ExitPlanMode"

_DESCRIPTION = "Exit plan mode and present the completed plan to the user for review."

_PROMPT = """\
Call this tool when you have finished exploring and are ready to present your
plan. Pass your complete, detailed plan as the `plan` argument.

The user will review the plan. If approved, you may proceed with implementation.
If rejected, you will receive feedback and should revise the plan before
calling ExitPlanMode again.

Guidelines for the plan:
- List every file that will be created or modified.
- Describe each change at the function/class level.
- Include any shell commands you intend to run.
- Keep it concrete enough that the user can judge risk and scope.
"""


class ExitPlanModeTool(Tool):
    """
    Mirrors ExitPlanModeV2Tool in src/tools/ExitPlanModeTool/ExitPlanModeV2Tool.ts.

    Input schema (mirrors _sdkInputSchema in source):
      plan: str — the full plan text to present to the user.
      allowedPrompts: list[{tool, prompt}] — optional semantic permissions for
        Bash actions the plan requires (e.g. {"tool": "Bash", "prompt": "run tests"}).
    """

    name = EXIT_PLAN_MODE_TOOL_NAME
    description = _DESCRIPTION
    is_concurrency_safe = True
    should_defer = True

    def get_schema(self) -> dict:
        return {
            "name": self.name,
            "description": _DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": {
                    "plan": {
                        "type": "string",
                        "description": "The complete plan to present to the user for review.",
                    },
                    "allowedPrompts": {
                        "type": "array",
                        "description": (
                            "Optional: prompt-based Bash permissions needed to execute the plan. "
                            "Each entry describes a category of action rather than a specific command."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "tool": {"type": "string", "enum": ["Bash"]},
                                "prompt": {"type": "string"},
                            },
                            "required": ["tool", "prompt"],
                        },
                    },
                },
                "required": ["plan"],
            },
        }

    async def call(self, tool_input: dict, ctx: ToolContext) -> str:
        plan = tool_input.get("plan", "")
        if not plan:
            raise ValueError("plan is required")

        ctx.plan_mode = False
        ctx.pending_plan = plan

        # In a real interactive session this would pause and prompt the user.
        # In server/SDK mode we surface the plan as a structured response.
        return f"Plan submitted for review:\n\n{plan}\n\nAwaiting user approval before proceeding."

    def is_read_only(self, input: dict | None = None) -> bool:
        return True
