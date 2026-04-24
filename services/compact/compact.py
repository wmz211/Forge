from __future__ import annotations
"""
Compaction execution — summarise the conversation via a forked LLM call.
Mirrors src/services/compact/compact.ts  (compactConversation / buildPostCompactMessages).
"""
import json

from .prompt import COMPACT_SYSTEM_PROMPT, get_compact_user_summary_message

# ── Compact boundary marker ───────────────────────────────────────────────────
# Added to the first message of every compacted output so that
# get_messages_after_compact_boundary() (utils/messages.py) can locate the
# most recent compaction cut point on the next iteration.
# Mirrors the special content block inserted by buildPostCompactMessages() in
# compact.ts to mark where the boundary sits in the message history.
COMPACT_BOUNDARY_KEY = "_compact_boundary"


def _format_message_for_summary(msg: dict) -> str:
    """
    Convert a single message dict into a human-readable block for the
    summarisation prompt.  Preserves full content (no truncation) and renders
    tool calls / tool results in a readable format.

    This replaces the old approach of joining role+content[:3000] strings,
    matching the source's intent of giving the summarisation model the full
    conversation context (mirrors the message objects passed to runForkedAgent
    in compact.ts, converted to text because the OpenAI-compat API rejects
    raw tool_call messages in a no-tools request).
    """
    role    = msg.get("role", "unknown").upper()
    content = msg.get("content") or ""

    # Normalise list-style content (Anthropic multi-block format)
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                parts.append(str(block))
                continue
            btype = block.get("type", "")
            if btype == "text":
                parts.append(block.get("text", ""))
            elif btype == "tool_result":
                inner = block.get("content", "")
                if isinstance(inner, list):
                    inner = "\n".join(
                        b.get("text", str(b)) if isinstance(b, dict) else str(b)
                        for b in inner
                    )
                parts.append(f"[tool_result]\n{inner}")
            else:
                parts.append(f"[{btype}] {json.dumps(block)}")
        content = "\n".join(parts)

    # Render tool calls inline (OpenAI format)
    tool_calls = msg.get("tool_calls") or []
    if tool_calls:
        tc_lines = []
        for tc in tool_calls:
            fn   = tc.get("function", {})
            name = fn.get("name", "?")
            args = fn.get("arguments", "{}")
            try:
                args_pretty = json.dumps(json.loads(args), ensure_ascii=False)
            except (json.JSONDecodeError, TypeError):
                args_pretty = args
            tc_lines.append(f"  {name}({args_pretty})")
        tool_block = "[Tool calls]\n" + "\n".join(tc_lines)
        content = f"{content}\n{tool_block}".strip() if content else tool_block

    return f"[{role}]\n{content}" if content else f"[{role}]"


async def compact(
    messages: list[dict],
    api_client,
    system_override: str | None = None,
) -> list[dict]:
    """
    Compact the conversation by summarising it with the LLM.
    Mirrors compactConversation() + buildPostCompactMessages() in compact.ts.

    Changes vs. the previous implementation
    ----------------------------------------
    Bug 1 fixed — full content, no per-message 3000-char truncation:
      The summarisation model receives the complete conversation text so that
      it can produce an accurate, detailed summary.  Tool calls and tool results
      are rendered as readable text blocks (required because the OpenAI-compat
      API rejects raw tool_call messages in a no-tools request).

    Bug 2 fixed — compact boundary marker:
      The first message in the returned list carries COMPACT_BOUNDARY_KEY=True.
      get_messages_after_compact_boundary() uses this marker to find the most
      recent compaction cut point, matching buildPostCompactMessages() in source.
    """
    print("\n  \033[33m[Auto-compact triggered — summarising conversation…]\033[0m")

    # Build full conversation text — no truncation (Bug 1 fix).
    history_text = "\n\n---\n\n".join(
        _format_message_for_summary(m) for m in messages
    )
    compact_input = [{"role": "user", "content": history_text}]

    # Run forked no-tools summarisation call (mirrors runForkedAgent).
    raw_summary = ""
    async for event in api_client.stream(
        messages=compact_input,
        tools=None,
        system_prompt=system_override or COMPACT_SYSTEM_PROMPT,
    ):
        if event["type"] == "text":
            raw_summary += event["content"]

    summary_content = get_compact_user_summary_message(raw_summary)

    # Rebuild with boundary marker (Bug 2 fix).
    # Mirrors buildPostCompactMessages(): the returned list always begins with
    # the compaction summary tagged as a boundary so callers can locate it.
    compacted = [
        {
            "role": "user",
            "content": summary_content,
            COMPACT_BOUNDARY_KEY: True,       # stripped by normalize_messages_for_api()
        }
    ]
    print("  \033[33m[Compact complete]\033[0m\n")
    return compacted
