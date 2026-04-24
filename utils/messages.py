from __future__ import annotations

# Internal metadata keys that must be stripped before an API call.
# These are used by Forge internals and are not valid OpenAI message fields.
_INTERNAL_MESSAGE_KEYS = frozenset({"_compact_boundary"})


def make_tool_result_message(tool_call_id: str, content: str) -> dict:
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": content,
    }


def make_assistant_tool_call_message(
    tool_calls: list[dict],
    text: str | None = None,
) -> dict:
    """
    Build an assistant message that contains tool_calls (OpenAI format).

    Mirrors the Anthropic API's assistant content array, which can hold both
    text blocks and tool_use blocks in a single message.  In OpenAI format the
    equivalent is content=<text> alongside tool_calls=[...].

    text: the reasoning/prose Claude emitted before or alongside the tool calls.
          Maps to the 'text' content block(s) in the Anthropic response.
    """
    return {
        "role": "assistant",
        "content": text if text else None,
        "tool_calls": [
            {
                "id": tc["id"],
                "type": "function",
                "function": {
                    "name": tc["name"],
                    "arguments": __import__("json").dumps(tc["arguments"]),
                },
            }
            for tc in tool_calls
        ],
    }


def make_assistant_text_message(text: str) -> dict:
    return {"role": "assistant", "content": text}


# ── Compact boundary helpers ──────────────────────────────────────────────────

def get_messages_after_compact_boundary(messages: list[dict]) -> list[dict]:
    """
    Return the slice of messages starting from the last compact boundary.
    Mirrors getMessagesAfterCompactBoundary() in src/utils/messages.ts.

    The compact boundary is the summary message written by compact() that carries
    _compact_boundary=True.  Scanning from the end ensures we always use the most
    recent compaction cut point, so multiple compactions work correctly.

    If no boundary is found (session never compacted), the full list is returned.
    """
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("_compact_boundary"):
            return messages[i:]
    return messages


def normalize_messages_for_api(messages: list[dict]) -> list[dict]:
    """
    Strip internal metadata keys before sending messages to the API.
    Mirrors normalizeMessagesForAPI() / the filtering step in source that
    removes internal fields before calling the Anthropic/OpenAI endpoint.

    Currently strips: _compact_boundary
    """
    return [
        {k: v for k, v in msg.items() if k not in _INTERNAL_MESSAGE_KEYS}
        for msg in messages
    ]
