from __future__ import annotations
import asyncio
from typing import AsyncGenerator, Any, Callable

from tool import Tool, ToolContext
from services.compact import (
    compact,
    calculate_token_warning_state,
    snip_compact_if_needed,
)
from utils.tokens import token_count_with_estimation
from utils.messages import (
    make_tool_result_message,
    make_assistant_tool_call_message,
    make_assistant_text_message,
    get_messages_after_compact_boundary,
    normalize_messages_for_api,
)
from utils.tool_result_budget import apply_tool_result_budget
from tools import core_tools_for_api

DEFAULT_SYSTEM_PROMPT = """\
You are an expert coding assistant with access to tools for reading, writing, \
and executing code. Work step-by-step: explore before editing, verify after changes. \
Be concise in explanations. Use tools when needed rather than guessing file contents.\
"""

MAX_TURNS = 50  # hard limit per session turn

# Mirrors MAX_OUTPUT_TOKENS_RECOVERY_LIMIT in query.ts.
# When the API returns stop_reason="length" (max output tokens hit), retry up
# to this many times by injecting a meta recovery message.  After exhausting the
# limit the partial response is surfaced and the loop terminates normally.
MAX_OUTPUT_TOKENS_RECOVERY_LIMIT = 3

# Recovery message injected when the model hits max_output_tokens.
# Mirrors the createUserMessage in query.ts recovery path.
_MAX_OUTPUT_TOKENS_RECOVERY_MESSAGE = (
    "Output token limit hit. Resume directly — no apology, no recap of what "
    "you were doing. Pick up mid-thought if that is where the cut happened. "
    "Break remaining work into smaller pieces."
)


def _partition_tool_calls(
    tool_calls: list[dict], tools_by_name: dict[str, Tool]
) -> list[tuple[bool, list[dict]]]:
    """
    Split tool calls into batches of [concurrent-safe] or [single non-safe].
    Mirrors Claude Code's partitionToolCalls in toolOrchestration.ts.

    Source pattern:
      const parsedInput = tool?.inputSchema.safeParse(toolUse.input)
      const isConcurrencySafe = parsedInput?.success
        ? Boolean(tool?.isConcurrencySafe(parsedInput.data))
        : false

    We skip Zod validation (no schema validator here) and pass arguments
    directly — equivalent to parsedInput.success=true with data=arguments.
    """
    batches: list[tuple[bool, list[dict]]] = []
    for tc in tool_calls:
        tool = tools_by_name.get(tc["name"])
        if tool is None:
            safe = False
        else:
            args = tc.get("arguments")
            safe = bool(
                tool.is_concurrency_safe_for_input(args)
                if isinstance(args, dict)
                else tool.is_concurrency_safe_for_input({})
            )
        if safe and batches and batches[-1][0]:
            batches[-1][1].append(tc)
        else:
            batches.append((safe, [tc]))
    return batches


async def _run_tool(
    tc: dict,
    tools_by_name: dict[str, Tool],
    ctx: ToolContext,
) -> tuple[str, str]:
    """Execute a single tool call. Returns (tool_call_id, result_text)."""
    tool = tools_by_name.get(tc["name"])
    if tool is None:
        return tc["id"], f"<error>Unknown tool: {tc['name']}</error>"

    desc = f"{tc['name']}({tc['arguments']})"
    try:
        ok, validation_message = await tool.validate_input(
            tc["arguments"] if isinstance(tc.get("arguments"), dict) else {},
            ctx,
        )
    except Exception as e:
        ok, validation_message = False, str(e)
    if not ok:
        return tc["id"], f"<error>{validation_message or 'Invalid tool input'}</error>"

    if isinstance(tc.get("arguments"), dict):
        args_for_permission = dict(tc["arguments"])
        args_for_permission["_cwd"] = ctx.cwd
        args_for_permission["_additional_working_directories"] = list(ctx.additional_working_directories)
    else:
        args_for_permission = None
    if not ctx.confirm_fn(tc["name"], desc, args_for_permission):
        return tc["id"], f"<error>Permission denied for {tc['name']}</error>"

    try:
        result = await tool.call(tc["arguments"], ctx)
    except Exception as e:
        result = f"<error>{e}</error>"

    return tc["id"], result


async def query_loop(
    messages: list[dict],
    tools: list[Tool],
    api_client,
    ctx: ToolContext,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    max_turns: int = MAX_TURNS,
    last_usage: dict | None = None,
    message_source: Callable[[], list[str]] | None = None,
) -> AsyncGenerator[dict, None]:
    """
    Core ReAct agent loop. Mirrors Claude Code's queryLoop() in query.ts.

    last_usage: usage dict from the previous API response
      {'prompt_tokens': int, 'completion_tokens': int}
      Passed to needs_compaction() so token_count_with_estimation() can use
      real counts instead of character estimation (mirrors tokenCountWithEstimation
      in tokens.ts).

    Yields events:
      {"type": "text", "content": str}
      {"type": "tool_use", "name": str, "arguments": ..., "id": str}
      {"type": "tool_result", "name": str, "result": str, "id": str}
      {"type": "assistant_message", "message": dict}    — for session persistence
      {"type": "tool_result_message", "message": dict}  — for session persistence
      {"type": "done", "reason": str, "usage": dict | None}
        reason values:
          stop           — no tool calls, model finished naturally
          max_turns      — hard turn limit reached
          blocking_limit — context at blocking threshold even after compaction

    New in this revision
    --------------------
    Defense ① tool_result_budget:
      Mirrors applyToolResultBudget() in query.ts — truncates oversized tool
      results when context is above the warning threshold.  Runs BEFORE snip
      (defense ②) on each iteration.

    Max-output-tokens recovery (mirrors query.ts:1223):
      When stop_reason == "length" (API hit MAX_OUTPUT_TOKENS), inject a meta
      recovery message and continue the loop for up to MAX_OUTPUT_TOKENS_RECOVERY_LIMIT
      additional iterations without consuming a turn slot.  After exhausting the
      limit the partial assistant response is yielded normally and the loop
      terminates.
    """
    tools_by_name = {t.name: t for t in tools}
    api_visible_tools = core_tools_for_api(tools) if tools else []
    openai_tools = [t.to_openai_tool() for t in api_visible_tools] if api_visible_tools else None

    current_messages = list(messages)
    turn = 0
    current_usage = last_usage
    # Mirrors maxOutputTokensRecoveryCount in query.ts State.
    max_output_tokens_recovery_count = 0

    while turn < max_turns:
        turn += 1

        # ── Compaction pipeline (mirrors queryLoop in query.ts) ──────────────
        #
        # Correct source order (defenses run before the blocking check):
        #   [defense ①] applyToolResultBudget      ← NEW: truncate oversized results
        #   [defense ②] snipCompactIfNeeded        ← keep head+tail, drop middle
        #   [defense ③] microcompact               — not yet implemented
        #   [defense ④] applyCollapsesIfNeeded     — not yet implemented
        #   [check]     isAtBlockingLimit           — exit if still too full
        #   [defense ⑤] autocompact (isAboveAutoCompactThreshold)
        #
        # Step A — extract only messages after the last compact boundary.
        # Mirrors: let messagesForQuery = [...getMessagesAfterCompactBoundary(messages)]
        messages_for_query = get_messages_after_compact_boundary(current_messages)

        # Step A½ — [defense ①] truncate oversized tool results.
        # Mirrors: messagesForQuery = await applyToolResultBudget(messagesForQuery, ...)
        messages_for_query = apply_tool_result_budget(messages_for_query, current_usage)

        # Step B — [defense ②] snip stale middle messages when above warning threshold.
        # Mirrors: snipCompactIfNeeded(messagesForQuery, ...) → snipTokensFreed
        messages_for_query, snip_tokens_freed = snip_compact_if_needed(
            messages_for_query, current_usage
        )

        # Step C — blocking-limit check.
        # Subtract snip_tokens_freed from the running estimate so a successful snip
        # doesn't immediately re-trigger a heavier compaction step.
        # Mirrors: calculateTokenWarningState(tokenCount - snipTokensFreed, model)
        _raw_count    = token_count_with_estimation(messages_for_query, current_usage)
        _token_count  = max(0, _raw_count - snip_tokens_freed)
        _warning_state = calculate_token_warning_state(_token_count)
        if _warning_state["isAtBlockingLimit"]:
            _error_text = (
                "The context window is full and cannot be compacted further. "
                "Use /compact to summarize the conversation, start a new session, "
                "or reduce the amount of context before continuing."
            )
            yield {"type": "text", "content": _error_text}
            yield {"type": "done", "reason": "blocking_limit", "usage": current_usage}
            return

        # Step D — [defense ⑤] autocompact when above the autocompact threshold.
        # Runs AFTER the blocking check so we don't waste a summarisation call
        # on a context that is already too large to recover from.
        # Mirrors: isAtWarningLimit → runAutoCompact(messagesForQuery)
        if _warning_state["isAboveAutoCompactThreshold"]:
            messages_for_query = await compact(messages_for_query, api_client)
            # Replace message history with the compacted slice.
            # The compact boundary marker inside messages_for_query[0] will be
            # found by get_messages_after_compact_boundary() on the next iteration.
            current_messages = list(messages_for_query)
            current_usage = None
            # Recalculate state with post-compact token count
            _token_count  = token_count_with_estimation(current_messages, current_usage)
            _warning_state = calculate_token_warning_state(_token_count)

        # Step E — strip internal metadata keys before sending to the API.
        # Mirrors: normalizeMessagesForAPI(messagesForQuery) — removes fields like
        # _compact_boundary that are valid in our internal history but rejected by
        # the OpenAI-compat endpoint.
        api_messages = normalize_messages_for_api(messages_for_query)

        # ── Call Qwen API (streaming) ─────────────────────────────────────────
        text_buffer = ""
        tool_calls_this_turn: list[dict] = []
        stop_reason = "stop"

        async for event in api_client.stream(
            messages=api_messages,
            tools=openai_tools,
            system_prompt=system_prompt,
        ):
            if event["type"] == "text":
                text_buffer += event["content"]
                yield {"type": "text", "content": event["content"]}

            elif event["type"] == "tool_call":
                tool_calls_this_turn.append(event)
                yield {
                    "type": "tool_use",
                    "name": event["name"],
                    "arguments": event["arguments"],
                    "id": event["id"],
                }

            elif event["type"] == "done":
                stop_reason = event["stop_reason"]
                current_usage = event.get("usage")

        # ── Max-output-tokens recovery (mirrors query.ts:1223) ────────────────
        # When the API hits MAX_OUTPUT_TOKENS (finish_reason="length"), we have a
        # partial response.  Rather than terminating, we can ask the model to
        # continue from where it stopped — up to MAX_OUTPUT_TOKENS_RECOVERY_LIMIT times.
        if stop_reason == "length" and max_output_tokens_recovery_count < MAX_OUTPUT_TOKENS_RECOVERY_LIMIT:
            max_output_tokens_recovery_count += 1

            # Persist the partial assistant message so the model has context
            if text_buffer or tool_calls_this_turn:
                if tool_calls_this_turn:
                    asst_msg = make_assistant_tool_call_message(
                        tool_calls_this_turn, text=text_buffer or None
                    )
                else:
                    asst_msg = make_assistant_text_message(text_buffer)
                current_messages.append(asst_msg)
                yield {"type": "assistant_message", "message": asst_msg}

            # Inject recovery prompt (mirrors createUserMessage({ isMeta: true }))
            recovery_msg = {
                "role": "user",
                "content": _MAX_OUTPUT_TOKENS_RECOVERY_MESSAGE,
            }
            current_messages.append(recovery_msg)

            # Don't increment turn for the recovery retry (mirrors query.ts State flow)
            turn -= 1
            # Reset buffers for next iteration
            text_buffer = ""
            tool_calls_this_turn = []
            # Continue to next iteration WITHOUT yielding "done"
            continue

        # After exhausting recovery attempts, reset counter for the next user turn
        if stop_reason == "length":
            max_output_tokens_recovery_count = 0

        # ③ Build and append assistant message to history.
        # Mirrors the source: a single assistant message may contain both text
        # blocks and tool_use blocks (Anthropic content array → OpenAI content +
        # tool_calls fields).  text_buffer must not be silently dropped when
        # tool calls are present — pass it as the content field.
        if tool_calls_this_turn:
            asst_msg = make_assistant_tool_call_message(
                tool_calls_this_turn, text=text_buffer or None
            )
            current_messages.append(asst_msg)
            yield {"type": "assistant_message", "message": asst_msg}
        elif text_buffer:
            asst_msg = make_assistant_text_message(text_buffer)
            current_messages.append(asst_msg)
            yield {"type": "assistant_message", "message": asst_msg}

        # ④ No tool calls → agent is done
        if not tool_calls_this_turn:
            yield {"type": "done", "reason": "stop", "usage": current_usage}
            return

        # ⑤ Execute tools — concurrent for read-only, serial for write
        batches = _partition_tool_calls(tool_calls_this_turn, tools_by_name)
        tool_results: list[tuple[str, str, str]] = []  # (id, name, result)

        for is_concurrent, batch in batches:
            if is_concurrent:
                tasks = [_run_tool(tc, tools_by_name, ctx) for tc in batch]
                results = await asyncio.gather(*tasks)
                for (tc_id, result), tc in zip(results, batch):
                    tool_results.append((tc_id, tc["name"], result))
            else:
                for tc in batch:
                    tc_id, result = await _run_tool(tc, tools_by_name, ctx)
                    tool_results.append((tc_id, tc["name"], result))

        # ⑥ Append tool results to history and yield events
        for tc_id, tc_name, result in tool_results:
            result_msg = make_tool_result_message(tc_id, result)
            current_messages.append(result_msg)
            yield {"type": "tool_result_message", "message": result_msg}
            yield {
                "type": "tool_result",
                "name": tc_name,
                "result": result,
                "id": tc_id,
            }

        # ⑦ Drain pending injected messages between turns.
        # Mirrors drainPendingMessages() called from attachments.ts between query loop turns.
        # Allows the UI layer (or parent agent) to inject continuation prompts into a
        # running background agent via enqueue_message() / SendMessage.
        if message_source:
            for injected_text in message_source():
                injected_msg = {"role": "user", "content": injected_text}
                current_messages.append(injected_msg)
                yield {"type": "injected_message", "message": injected_msg}

        # ⑧ Reset max_output_tokens counter on a successful turn (tool calls executed)
        max_output_tokens_recovery_count = 0

        # Loop continues → next iteration calls the model again

    yield {"type": "done", "reason": "max_turns", "usage": current_usage}
