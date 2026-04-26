"""
RemoteTriggerTool — manage scheduled remote agent triggers via the Claude.ai API.

Mirrors src/tools/RemoteTriggerTool/RemoteTriggerTool.ts.
Feature-gated: enabled only when env var FORGE_AGENT_TRIGGERS_REMOTE=1.

Actions: list | get | create | update | run
"""
from __future__ import annotations
import os
import json
from tool import Tool, ToolContext

REMOTE_TRIGGER_TOOL_NAME = "RemoteTrigger"

_DESCRIPTION = "Manage scheduled remote agent triggers (list, get, create, update, run)."

_PROMPT = """\
Manage persistent remote triggers that can schedule Claude agents to run in the
cloud on a cron schedule or on-demand.

Actions:
  list   — list all triggers for the current organization.
  get    — get details of a specific trigger (requires trigger_id).
  create — create a new trigger (requires body with cron, prompt, etc.).
  update — update an existing trigger (requires trigger_id and body).
  run    — immediately run a trigger (requires trigger_id).

Returns the raw API response as JSON.
"""


class RemoteTriggerTool(Tool):
    """
    Mirrors RemoteTriggerTool in src/tools/RemoteTriggerTool/RemoteTriggerTool.ts.

    Input schema:
      action: "list" | "get" | "create" | "update" | "run"
      trigger_id: str (required for get, update, run)
      body: dict (JSON body for create and update)
    """

    name = REMOTE_TRIGGER_TOOL_NAME
    description = _DESCRIPTION
    should_defer = True

    def is_enabled(self) -> bool:
        return os.environ.get("FORGE_AGENT_TRIGGERS_REMOTE", "0") == "1"

    def get_schema(self) -> dict:
        return {
            "name": self.name,
            "description": _DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "get", "create", "update", "run"],
                        "description": "The operation to perform on remote triggers.",
                    },
                    "trigger_id": {
                        "type": "string",
                        "description": "Required for get, update, and run.",
                    },
                    "body": {
                        "type": "object",
                        "description": "JSON body for create and update.",
                    },
                },
                "required": ["action"],
            },
        }

    async def call(self, tool_input: dict, ctx: ToolContext) -> str:
        # In a real implementation this would call the Claude.ai triggers API
        # using the user's OAuth token. Here we return a stub response since
        # we don't have credentials in the Forge context.
        action = tool_input.get("action")
        trigger_id = tool_input.get("trigger_id")
        body = tool_input.get("body", {})

        stub = {
            "status": 200,
            "json": json.dumps({
                "message": (
                    f"RemoteTrigger.{action} is not yet backed by the Forge API. "
                    "Set FORGE_AGENT_TRIGGERS_REMOTE=1 and provide FORGE_OAUTH_TOKEN "
                    "to enable real remote trigger management."
                ),
                "action": action,
                "trigger_id": trigger_id,
            }),
        }
        return json.dumps(stub)
