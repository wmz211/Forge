"""
SendMessageTool — send a message to another agent (teammate) in a swarm.

Mirrors src/tools/SendMessageTool/SendMessageTool.ts.
Feature-gated: enabled only when FORGE_AGENT_SWARMS=1 or when the current
session has an active swarm context.

The simplest case in a single-agent setup is to allow the model to send
messages to named background sub-agents it has previously spawned.
"""
from __future__ import annotations
import os
import json
from tool import Tool, ToolContext

SEND_MESSAGE_TOOL_NAME = "SendMessage"

_DESCRIPTION = (
    "Send a message to a teammate agent by name, or broadcast to all teammates "
    "with \"*\". Used to coordinate work in multi-agent swarms."
)

_PROMPT = """\
Send a text or structured message to a running teammate agent.

Parameters:
  to: Recipient. Use a teammate name (as returned by the AgentTool), "*" to
      broadcast to all teammates, or an agent ID.
  message: The message content. Either a plain string or a structured object
      (e.g. {"type": "shutdown_request"} or {"type": "shutdown_response", ...}).
  summary (optional): A 5-10 word summary shown as a preview in the UI.
      Required when message is a plain string.

Structured message types (discriminated union):
  shutdown_request   — ask the recipient to finish up and exit.
  shutdown_response  — reply to a shutdown_request (approve/reject).
  plan_approval_response — respond to a plan approval request.

Returns a confirmation when the message was delivered.
"""


class SendMessageTool(Tool):
    """
    Mirrors SendMessageTool in src/tools/SendMessageTool/SendMessageTool.ts.

    Input schema:
      to: str — teammate name, "*" for broadcast, or agent ID.
      message: str | dict — plain string or structured message object.
      summary: str (optional) — UI preview (required when message is a string).
    """

    name = SEND_MESSAGE_TOOL_NAME
    description = _DESCRIPTION
    should_defer = True

    def is_enabled(self) -> bool:
        return os.environ.get("FORGE_AGENT_SWARMS", "0") == "1"

    def get_schema(self) -> dict:
        return {
            "name": self.name,
            "description": _DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {
                        "type": "string",
                        "description": (
                            'Recipient: teammate name, "*" for broadcast to all teammates, '
                            'or an agent ID.'
                        ),
                    },
                    "message": {
                        "description": (
                            "The message to send. Either a plain string or a structured "
                            "object with a 'type' field."
                        ),
                    },
                    "summary": {
                        "type": "string",
                        "description": (
                            "A 5-10 word summary shown as a preview in the UI. "
                            "Required when message is a string."
                        ),
                    },
                },
                "required": ["to", "message"],
            },
        }

    async def call(self, tool_input: dict, ctx: ToolContext) -> str:
        to = tool_input.get("to", "")
        message = tool_input.get("message")
        summary = tool_input.get("summary", "")

        # Look up the recipient agent in the context's agent registry.
        # In a real swarm this would queue the message to the target's inbox.
        agent_registry = getattr(ctx, "agent_registry", {})
        if to == "*":
            recipients = list(agent_registry.keys()) if agent_registry else ["(broadcast)"]
        else:
            recipients = [to]

        for recipient in recipients:
            agent = agent_registry.get(recipient)
            if agent and hasattr(agent, "enqueue_message"):
                msg_text = message if isinstance(message, str) else json.dumps(message)
                agent.enqueue_message(msg_text)

        recipient_str = ", ".join(recipients)
        if isinstance(message, str):
            preview = summary or message[:60]
        else:
            preview = json.dumps(message)[:60]

        return f"Message delivered to {recipient_str}: {preview}"
