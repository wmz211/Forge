from __future__ import annotations
"""
AskUserQuestion tool.
Mirrors src/tools/AskUserQuestionTool/ in Claude Code source.

Lets the model ask the user a multiple-choice question and collect their answer
during execution. In the terminal context we render the question and options as
numbered text and read a typed reply.
"""
import builtins
import sys
from typing import Any

from tool import Tool, ToolContext

ASK_USER_QUESTION_TOOL_NAME = "AskUserQuestion"

_DESCRIPTION = (
    "Asks the user multiple choice questions to gather information, clarify ambiguity, "
    "understand preferences, make decisions or offer them choices."
)

_PROMPT = """\
Use this tool when you need to ask the user questions during execution. This allows you to:
1. Gather user preferences or requirements
2. Clarify ambiguous instructions
3. Get decisions on implementation choices as you work
4. Offer choices to the user about what direction to take.

Usage notes:
- Users will always be able to select "Other" to provide custom text input
- Use multiSelect: true to allow multiple answers to be selected for a question
- If you recommend a specific option, make that the first option in the list and add "(Recommended)" at the end of the label

Plan mode note: In plan mode, use this tool to clarify requirements or choose between approaches BEFORE finalizing your plan. Do NOT use this tool to ask "Is my plan ready?" or "Should I proceed?" - use ExitPlanMode for plan approval.
"""


class AskUserQuestionTool(Tool):
    """
    Presents a numbered option list; the user types a number or free text.
    """
    name = ASK_USER_QUESTION_TOOL_NAME
    description = _DESCRIPTION
    is_concurrency_safe = False
    should_defer = True

    def get_schema(self) -> dict:
        return {
            "name": self.name,
            "description": _PROMPT,
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The question to ask the user",
                    },
                    "options": {
                        "type": "array",
                        "description": "List of options for the user to choose from",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string"},
                                "value": {"type": "string"},
                                "description": {"type": "string"},
                                "preview": {"type": "string"},
                            },
                            "required": ["label", "value"],
                        },
                    },
                    "multiSelect": {
                        "type": "boolean",
                        "description": "Allow the user to select multiple options",
                        "default": False,
                    },
                },
                "required": ["question", "options"],
            },
        }

    def to_openai_tool(self) -> dict:
        schema = self.get_schema()
        return {
            "type": "function",
            "function": {
                "name": schema["name"],
                "description": schema["description"],
                "parameters": schema["parameters"],
            },
        }

    async def validate_input(
        self, input: dict[str, Any], ctx: ToolContext
    ) -> tuple[bool, str | None]:
        if not input.get("question"):
            return False, "question is required"
        options = input.get("options")
        if not isinstance(options, list) or not options:
            return False, "options must be a non-empty list"
        for index, option in enumerate(options):
            if not isinstance(option, dict):
                return False, f"options[{index}] must be an object"
            if not option.get("label") or not option.get("value"):
                return False, f"options[{index}] requires label and value"
        if input.get("multiSelect") and any(option.get("preview") for option in options):
            return False, "preview is only supported for single-select questions"
        return True, None

    async def call(self, input: dict[str, Any], ctx: ToolContext) -> str:
        question = input.get("question", "")
        options = input.get("options", [])
        multi = bool(input.get("multiSelect", False))

        print(f"\n\033[1m{question}\033[0m")
        for i, opt in enumerate(options, 1):
            label = opt.get("label", f"Option {i}")
            desc = opt.get("description", "")
            print(f"  {i}. {label}" + (f" - {desc}" if desc else ""))
            if opt.get("preview"):
                print(f"     {opt['preview']}")

        other_num = len(options) + 1
        print(f"  {other_num}. Other (type your own answer)")

        prompt_text = (
            "Enter numbers separated by commas (or type your answer): "
            if multi
            else "Enter number or type your answer: "
        )

        try:
            raw = (
                sys.stdin.readline().strip()
                if not sys.stdin.isatty()
                else builtins.input(f"  {prompt_text}")
            )
        except (EOFError, KeyboardInterrupt):
            return "User did not provide an answer."

        if not raw:
            return "User did not provide an answer."

        selected: list[str] = []
        parts = [p.strip() for p in raw.split(",")] if multi else [raw.strip()]
        for part in parts:
            if part.isdigit():
                idx = int(part)
                if 1 <= idx <= len(options):
                    selected.append(
                        options[idx - 1].get("value")
                        or options[idx - 1].get("label", "")
                    )
                elif idx == other_num:
                    selected.append(raw)
                else:
                    selected.append(part)
            else:
                selected.append(part)

        return ", ".join(selected) if multi else selected[0]
