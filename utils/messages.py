from __future__ import annotations
"""
Message construction and query utilities.
Mirrors src/utils/messages.ts (selected helpers) and src/types/message.ts.
"""
import uuid as _uuid

# Internal metadata keys stripped before an API call.
_INTERNAL_MESSAGE_KEYS = frozenset({"_compact_boundary", "_usage"})

# ── Synthetic / special message content strings ───────────────────────────────
# Mirrors SYNTHETIC_MESSAGES / CANCEL_MESSAGE / REJECT_MESSAGE in messages.ts.

INTERRUPT_MESSAGE = "[Request interrupted by user]"
INTERRUPT_MESSAGE_FOR_TOOL_USE = "[Request interrupted by user for tool use]"
CANCEL_MESSAGE = (
    "The user doesn't want to take this action right now. STOP what you are doing "
    "and wait for the user to tell you how to proceed."
)
REJECT_MESSAGE = (
    "The user doesn't want to proceed with this tool use. The tool use was rejected "
    "(eg. if it was a file edit, the new_string was NOT written to the file). "
    "STOP what you are doing and wait for the user to tell you how to proceed."
)
NO_RESPONSE_REQUESTED = "No response requested."

SYNTHETIC_MESSAGES = frozenset({
    INTERRUPT_MESSAGE,
    INTERRUPT_MESSAGE_FOR_TOOL_USE,
    CANCEL_MESSAGE,
    REJECT_MESSAGE,
    NO_RESPONSE_REQUESTED,
})

# ── Basic message factories ───────────────────────────────────────────────────

def make_tool_result_message(tool_call_id: str, content: str) -> dict:
    """Build an OpenAI-format tool-result message."""
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
    Build an assistant message containing tool_calls (OpenAI format).

    Mirrors the Anthropic content array (text blocks + tool_use blocks) mapped
    to the OpenAI wire format (content=text, tool_calls=[...]).

    tool_calls items: {id, name, arguments (dict)}
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
                    "arguments": __import__("json").dumps(tc.get("arguments") or {}),
                },
            }
            for tc in tool_calls
        ],
    }


def make_assistant_text_message(text: str) -> dict:
    """Build a plain-text assistant message."""
    return {"role": "assistant", "content": text}


def create_user_message(
    content: str,
    is_meta: bool = False,
    is_compact_summary: bool = False,
) -> dict:
    """
    Mirrors createUserMessage() in messages.ts.
    Attaches optional metadata flags used by the compaction pipeline.
    """
    msg: dict = {"role": "user", "content": content}
    if is_meta:
        msg["_is_meta"] = True
    if is_compact_summary:
        msg["_is_compact_summary"] = True
    return msg


# ── Compact boundary helpers ──────────────────────────────────────────────────

def create_compact_boundary_message(
    trigger: str = "auto",
    pre_compact_token_count: int = 0,
) -> dict:
    """
    Mirrors createCompactBoundaryMessage() in messages.ts.

    The boundary marker is a synthetic user message that:
    - carries _compact_boundary=True so getMessagesAfterCompactBoundary can find it
    - stores trigger (auto|manual) and the pre-compact token count

    In the source this is a SystemCompactBoundaryMessage; here we reuse a
    regular user-role dict with internal metadata keys.
    """
    return {
        "role": "user",
        "content": (
            f"[Compact boundary — trigger={trigger}, "
            f"pre_compact_tokens={pre_compact_token_count}]"
        ),
        "_compact_boundary": True,
        "_compact_trigger": trigger,
        "_pre_compact_token_count": pre_compact_token_count,
        "_uuid": str(_uuid.uuid4()),
    }


def is_compact_boundary_message(msg: dict) -> bool:
    """
    Mirrors isCompactBoundaryMessage() in messages.ts.
    Returns True when the message is a compaction boundary marker.
    """
    return bool(msg.get("_compact_boundary"))


def get_messages_after_compact_boundary(messages: list[dict]) -> list[dict]:
    """
    Return the slice of messages starting from the last compact boundary.
    Mirrors getMessagesAfterCompactBoundary() in messages.ts.

    Scans from the end so multiple compactions work correctly — only the
    most recent boundary is used.

    If no boundary is found, the full list is returned (session not yet compacted).
    """
    for i in range(len(messages) - 1, -1, -1):
        if is_compact_boundary_message(messages[i]):
            return messages[i:]
    return messages


def normalize_messages_for_api(messages: list[dict]) -> list[dict]:
    """
    Strip internal metadata keys before sending messages to the API.
    Mirrors normalizeMessagesForAPI() — removes _compact_boundary and other
    internal fields that are not valid OpenAI/Anthropic message fields.
    """
    return [
        {k: v for k, v in msg.items() if k not in _INTERNAL_MESSAGE_KEYS}
        for msg in messages
    ]


# ── Assistant message inspection ──────────────────────────────────────────────

def get_assistant_message_text(message: dict) -> str | None:
    """
    Mirrors getAssistantMessageText() in messages.ts.
    Returns the text content from an assistant message, or None if absent.
    Handles both string content and list-of-blocks content.
    """
    if message.get("role") != "assistant":
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content or None
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        text = "".join(parts)
        return text or None
    return None


def get_last_assistant_message(messages: list[dict]) -> dict | None:
    """
    Mirrors getLastAssistantMessage() in messages.ts.
    Scans from end for the most recent assistant message.
    """
    for msg in reversed(messages):
        if msg.get("role") == "assistant":
            return msg
    return None


def strip_images_from_messages(messages: list[dict]) -> list[dict]:
    """
    Mirrors stripImagesFromMessages() from compact.ts.
    Replaces image/document blocks with text placeholders before compaction.
    Images are not needed for summarisation and can cause prompt-too-long errors.
    """
    result = []
    for msg in messages:
        if msg.get("role") != "user":
            result.append(msg)
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            result.append(msg)
            continue

        has_media = False
        new_content = []
        for block in content:
            if not isinstance(block, dict):
                new_content.append(block)
                continue
            btype = block.get("type", "")
            if btype == "image":
                has_media = True
                new_content.append({"type": "text", "text": "[image]"})
            elif btype == "document":
                has_media = True
                new_content.append({"type": "text", "text": "[document]"})
            elif btype == "tool_result" and isinstance(block.get("content"), list):
                tool_has_media = False
                new_tool_content = []
                for item in block["content"]:
                    if not isinstance(item, dict):
                        new_tool_content.append(item)
                    elif item.get("type") == "image":
                        tool_has_media = True
                        new_tool_content.append({"type": "text", "text": "[image]"})
                    elif item.get("type") == "document":
                        tool_has_media = True
                        new_tool_content.append({"type": "text", "text": "[document]"})
                    else:
                        new_tool_content.append(item)
                if tool_has_media:
                    has_media = True
                    new_content.append({**block, "content": new_tool_content})
                else:
                    new_content.append(block)
            else:
                new_content.append(block)

        if not has_media:
            result.append(msg)
        else:
            result.append({**msg, "content": new_content})
    return result
