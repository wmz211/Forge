from __future__ import annotations
"""
Compaction execution — summarise the conversation via a forked LLM call.
Mirrors src/services/compact/compact.ts (compactConversation / buildPostCompactMessages).

Key architecture (faithfully reproduced from source):
  - System prompt  : "You are a helpful AI assistant tasked with summarising conversations."
                     (not the long BASE_COMPACT_PROMPT — that is the USER message)
  - Summary request: get_compact_prompt() → sent as the last user message
  - Format helpers : strip_images_from_messages, get_messages_after_compact_boundary
  - Post-compact   : create_compact_boundary_message + get_compact_user_summary_message
  - Hooks          : executePreCompactHooks / executePostCompactHooks wired around compaction
"""
import json

from .prompt import (
    COMPACT_SYSTEM_PROMPT,
    get_compact_prompt,
    get_compact_user_summary_message,
)
from utils.messages import (
    create_compact_boundary_message,
    get_messages_after_compact_boundary,
    normalize_messages_for_api,
    strip_images_from_messages,
)
from utils.hooks import execute_pre_compact_hooks, execute_post_compact_hooks

# ERROR_MESSAGE_NOT_ENOUGH_MESSAGES mirrors the source constant.
ERROR_MESSAGE_NOT_ENOUGH_MESSAGES = "Not enough messages to compact."

# POST_COMPACT_MAX_FILES_TO_RESTORE mirrors compact.ts constant.
POST_COMPACT_MAX_FILES_TO_RESTORE = 5
POST_COMPACT_TOKEN_BUDGET = 50_000
POST_COMPACT_MAX_TOKENS_PER_FILE = 5_000


def _format_message_for_summary(msg: dict) -> str:
    """
    Convert a single message dict into a human-readable block for the
    summarisation prompt.

    Preserves full content (no truncation) and renders tool calls / tool
    results in a readable format.  Required because the OpenAI-compat API
    rejects raw tool_call messages in a no-tools request.

    Mirrors the conversion done by normalizeMessagesForAPI + the text
    representation sent to runForkedAgent in compact.ts.
    """
    role    = msg.get("role", "unknown").upper()
    content = msg.get("content") or ""

    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                parts.append(str(block))
                continue
            btype = block.get("type", "")
            if btype == "text":
                parts.append(block.get("text", ""))
            elif btype == "image":
                parts.append("[image]")
            elif btype == "document":
                parts.append("[document]")
            elif btype == "tool_result":
                inner = block.get("content", "")
                if isinstance(inner, list):
                    inner = "\n".join(
                        b.get("text", str(b)) if isinstance(b, dict) else str(b)
                        for b in inner
                    )
                parts.append(f"[tool_result]\n{inner}")
            elif btype == "thinking":
                parts.append(f"[thinking]\n{block.get('thinking', '')}")
            else:
                parts.append(f"[{btype}] {json.dumps(block)}")
        content = "\n".join(parts)

    # Render OpenAI-format tool_calls inline
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
    custom_instructions: str | None = None,
    is_auto_compact: bool = True,
    pre_compact_token_count: int = 0,
    cwd: str = "",
    session_id: str = "",
    transcript_path: str = "",
) -> list[dict]:
    """
    Compact the conversation by summarising it with the LLM.
    Mirrors compactConversation() + buildPostCompactMessages() in compact.ts.

    Architecture (faithfully mirrors the source):
    ─────────────────────────────────────────────
    1. Strip images from messages (mirrors stripImagesFromMessages)
    2. Build summary request: get_compact_prompt() as the final user message
    3. Call model with:
         system_prompt = COMPACT_SYSTEM_PROMPT   ← "You are a helpful AI..."
         messages      = [conversation_text, summaryRequest]
         tools         = None  ← no tool use during compaction
    4. Post-process: formatCompactSummary → getCompactUserSummaryMessage
    5. Build result:
         [createCompactBoundaryMessage, summaryUserMessage]
       mirrors buildPostCompactMessages() ordering.

    Returns
    -------
    list[dict]
        The post-compact message list starting with the boundary marker.
        The boundary marker carries _compact_boundary=True so
        get_messages_after_compact_boundary() finds it on the next iteration.
    """
    if not messages:
        raise ValueError(ERROR_MESSAGE_NOT_ENOUGH_MESSAGES)

    trigger: str = "auto" if is_auto_compact else "manual"

    print("\n  \033[33m[Auto-compact triggered — summarising conversation…]\033[0m")

    # Step 1a: Execute PreCompact hooks (mirrors executePreCompactHooks in query.ts).
    # Successful hook stdout becomes additional custom_instructions for the compact prompt.
    pre_hook_result = await execute_pre_compact_hooks(
        trigger=trigger,  # type: ignore[arg-type]
        custom_instructions=custom_instructions,
        cwd=cwd,
        session_id=session_id,
        transcript_path=transcript_path,
    )
    if pre_hook_result.get("new_custom_instructions"):
        extra = pre_hook_result["new_custom_instructions"]
        custom_instructions = (
            f"{custom_instructions}\n\n{extra}" if custom_instructions else extra
        )
    if pre_hook_result.get("user_display_message"):
        print(f"  {pre_hook_result['user_display_message']}")

    # Step 1b: Get messages after the last compact boundary (mirrors the source).
    messages_to_summarize = get_messages_after_compact_boundary(messages)

    # Step 2: Strip images before compaction (mirrors stripImagesFromMessages).
    # Prevents the compaction API call from hitting prompt-too-long on image-heavy sessions.
    stripped = strip_images_from_messages(messages_to_summarize)

    # Step 3: Convert to text for the no-tools summarization model.
    # Necessary because the OpenAI-compat endpoint rejects tool_call role messages
    # when no tools are declared.
    history_text = "\n\n---\n\n".join(
        _format_message_for_summary(m) for m in stripped
    )

    # Step 4: Build the summary request (mirrors summaryRequest in compact.ts).
    # The compact prompt (BASE_COMPACT_PROMPT) is sent as a USER message,
    # NOT as the system prompt. The system prompt is the short summarization instruction.
    compact_prompt = get_compact_prompt(custom_instructions)
    summary_request = {"role": "user", "content": compact_prompt}

    # Combine conversation history + summary request as the message array.
    compact_messages = [
        {"role": "user", "content": history_text},
        summary_request,
    ]

    # Step 5: Stream the summary (mirrors streamCompactSummary / queryModelWithStreaming).
    raw_summary = ""
    async for event in api_client.stream(
        messages=compact_messages,
        tools=None,
        system_prompt=COMPACT_SYSTEM_PROMPT,
    ):
        if event["type"] == "text":
            raw_summary += event["content"]

    if not raw_summary:
        raise RuntimeError(
            "Failed to generate conversation summary - response did not contain valid text content"
        )

    # Step 6: Format summary (mirrors formatCompactSummary).
    summary_content = get_compact_user_summary_message(
        raw_summary,
        suppress_follow_up_questions=is_auto_compact,
    )

    # Step 7: Build post-compact messages (mirrors buildPostCompactMessages).
    # Order: boundaryMarker, summaryMessages (mirrors the source's ordering).
    boundary_marker = create_compact_boundary_message(
        trigger="auto" if is_auto_compact else "manual",
        pre_compact_token_count=pre_compact_token_count,
    )
    summary_message = {
        "role": "user",
        "content": summary_content,
        "_is_compact_summary": True,
    }

    # Step 8: Execute PostCompact hooks (mirrors executePostCompactHooks in query.ts).
    post_hook_result = await execute_post_compact_hooks(
        trigger=trigger,  # type: ignore[arg-type]
        compact_summary=raw_summary,
        cwd=cwd,
        session_id=session_id,
        transcript_path=transcript_path,
    )
    if post_hook_result.get("user_display_message"):
        print(f"  {post_hook_result['user_display_message']}")

    print("  \033[33m[Compact complete]\033[0m\n")
    return [boundary_marker, summary_message]
