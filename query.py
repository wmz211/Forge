from __future__ import annotations
import asyncio
import time
from typing import AsyncGenerator, Any, Callable

from tool import Tool, ToolContext
from services.compact import (
    compact,
    calculate_token_warning_state,
    snip_compact_if_needed,
    microcompact_messages,
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
from utils.hooks import (
    execute_permission_request_hooks,
    execute_permission_denied_hooks,
    execute_post_tool_use_failure_hooks,
    execute_pre_tool_use_hooks,
    execute_post_tool_use_hooks,
    execute_stop_hooks,
)
from tools import core_tools_for_api
from permissions import check_permission

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

    tool_input = tc["arguments"] if isinstance(tc.get("arguments"), dict) else {}

    desc = f"{tc['name']}({tc['arguments']})"
    try:
        ok, validation_message = await tool.validate_input(tool_input, ctx)
    except Exception as e:
        ok, validation_message = False, str(e)
    if not ok:
        error = validation_message or "Invalid tool input"
        failure_hook = await execute_post_tool_use_failure_hooks(
            tool_name=tc["name"],
            tool_input=tool_input,
            error=error,
            cwd=ctx.cwd,
            session_id=ctx.session_id,
            transcript_path=str(ctx.session_transcript_path),
            permission_mode=ctx.permission_mode,
        )
        return tc["id"], _append_hook_context(f"<error>{error}</error>", tc["name"], "PostToolUseFailure", failure_hook)

    if isinstance(tc.get("arguments"), dict):
        args_for_permission = dict(tool_input)
        args_for_permission["_cwd"] = ctx.cwd
        args_for_permission["_additional_working_directories"] = list(ctx.additional_working_directories)
    else:
        args_for_permission = None

    # PreToolUse hooks — mirrors executePreToolUseHooks() in query.ts.
    # A non-zero exit code from any hook blocks the tool call.
    pre_hook = await execute_pre_tool_use_hooks(
        tool_name=tc["name"],
        tool_input=tool_input,
        cwd=ctx.cwd,
        session_id=ctx.session_id,
        transcript_path=str(ctx.session_transcript_path),
        permission_mode=ctx.permission_mode,
    )
    if pre_hook.get("block"):
        reason = pre_hook.get("block_reason", "Blocked by PreToolUse hook")
        return tc["id"], f"<error>{reason}</error>"
    pre_hook_allows = pre_hook.get("permission_decision") == "allow"
    updated_input = pre_hook.get("updated_input")
    if isinstance(updated_input, dict):
        tool_input = {
            k: v for k, v in updated_input.items()
            if k not in ("_cwd", "_additional_working_directories")
        }
        args_for_permission = dict(tool_input)
        args_for_permission["_cwd"] = ctx.cwd
        args_for_permission["_additional_working_directories"] = list(ctx.additional_working_directories)

    permission_result = check_permission(
        tool_name=tc["name"],
        mode=ctx.permission_mode,
        tool_input=args_for_permission,
        always_allow_rules=getattr(ctx, "always_allow", []),
        always_deny_rules=getattr(ctx, "always_deny", []),
        always_ask_rules=getattr(ctx, "always_ask", []),
    )
    hook_decision = None
    if (
        not pre_hook_allows
        and permission_result.get("behavior") == "ask"
        and args_for_permission is not None
    ):
        hook_decision = await execute_permission_request_hooks(
            tool_name=tc["name"],
            tool_input=args_for_permission,
            permission_result=permission_result,
            cwd=ctx.cwd,
            session_id=ctx.session_id,
            transcript_path=str(ctx.session_transcript_path),
            permission_mode=ctx.permission_mode,
        )

    if hook_decision and hook_decision.get("behavior") == "deny":
        message = hook_decision.get("message", f"Permission denied for {tc['name']}")
        denied_hook = await execute_permission_denied_hooks(
            tool_name=tc["name"],
            tool_input=args_for_permission,
            denial_message=message,
            cwd=ctx.cwd,
            session_id=ctx.session_id,
            transcript_path=str(ctx.session_transcript_path),
            permission_mode=ctx.permission_mode,
        )
        return tc["id"], _format_permission_denied_result(tc["name"], message, denied_hook)

    if hook_decision and hook_decision.get("behavior") == "allow":
        updated = hook_decision.get("updatedInput")
        if isinstance(updated, dict):
            tool_input = {
                k: v for k, v in updated.items()
                if k not in ("_cwd", "_additional_working_directories")
            }
    elif pre_hook_allows or permission_result.get("behavior") == "allow":
        pass
    else:
        if not ctx.confirm_fn(tc["name"], desc, args_for_permission):
            message = f"Permission denied for {tc['name']}"
            denied_hook = await execute_permission_denied_hooks(
                tool_name=tc["name"],
                tool_input=args_for_permission,
                denial_message=message,
                cwd=ctx.cwd,
                session_id=ctx.session_id,
                transcript_path=str(ctx.session_transcript_path),
                permission_mode=ctx.permission_mode,
            )
            return tc["id"], _format_permission_denied_result(tc["name"], message, denied_hook)

    try:
        result = await tool.call(tool_input, ctx)
    except Exception as e:
        error = str(e)
        failure_hook = await execute_post_tool_use_failure_hooks(
            tool_name=tc["name"],
            tool_input=tool_input,
            error=error,
            cwd=ctx.cwd,
            session_id=ctx.session_id,
            transcript_path=str(ctx.session_transcript_path),
            permission_mode=ctx.permission_mode,
        )
        return tc["id"], _append_hook_context(f"<error>{error}</error>", tc["name"], "PostToolUseFailure", failure_hook)

    # PostToolUse hooks — mirrors executePostToolUseHooks() in query.ts.
    # Errors from PostToolUse hooks are non-blocking (logged but not surfaced to model).
    post_hook = await execute_post_tool_use_hooks(
        tool_name=tc["name"],
        tool_input=tool_input,
        tool_response=result,
        cwd=ctx.cwd,
        session_id=ctx.session_id,
        transcript_path=str(ctx.session_transcript_path),
        permission_mode=ctx.permission_mode,
    )
    if post_hook.get("updated_tool_output") is not None:
        updated = post_hook["updated_tool_output"]
        result = updated if isinstance(updated, str) else str(updated)
    if post_hook.get("additional_context"):
        result = _append_hook_context(result, tc["name"], "PostToolUse", post_hook)

    return tc["id"], result


def _yield_missing_tool_result_blocks(
    assistant_messages: list[dict],
    error_message: str,
) -> list[dict]:
    """
    Synthesize tool_result error blocks for any tool_use blocks in assistant_messages
    that have no corresponding tool_result. Called when an error interrupts tool
    execution mid-stream.

    Mirrors yieldMissingToolResultBlocks() in query.ts — the generator yields
    createUserMessage({ content: [{ type: 'tool_result', is_error: true, ... }] })
    for each orphaned tool_use block. We return a list of message dicts instead
    of yielding, so the caller can both append them to history and yield events.
    """
    result_messages = []
    for asst_msg in assistant_messages:
        content = asst_msg.get("tool_calls") or []
        for tc in content:
            tc_id = tc.get("id") or tc.get("tool_call_id", "")
            result_messages.append({
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tc_id,
                        "content": error_message,
                        "is_error": True,
                    }
                ],
            })
    return result_messages


def _append_hook_context(result: str, tool_name: str, event: str, hook_result: dict | None) -> str:
    if not hook_result or not hook_result.get("additional_context"):
        return result
    return (
        f"{result}\n\n"
        f"<hook_additional_context event=\"{event}\" tool=\"{tool_name}\">\n"
        f"{hook_result['additional_context']}\n"
        f"</hook_additional_context>"
    )


def _format_permission_denied_result(tool_name: str, message: str, hook_result: dict | None) -> str:
    result = f"<error>{message}</error>"
    if hook_result and hook_result.get("retry"):
        result += "\nThe PermissionDenied hook indicated this command is now approved. You may retry it if you would like."
    return _append_hook_context(result, tool_name, "PermissionDenied", hook_result)


async def query_loop(
    messages: list[dict],
    tools: list[Tool],
    api_client,
    ctx: ToolContext,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    max_turns: int = MAX_TURNS,
    last_usage: dict | None = None,
    message_source: Callable[[], list[str]] | None = None,
    stop_hook_active: bool = False,
) -> AsyncGenerator[dict, None]:
    """
    Core ReAct agent loop. Mirrors Claude Code's queryLoop() in query.ts.

    Compaction pipeline (5 defenses, mirrors query.ts order):
      [defense ①] applyToolResultBudget      — truncate oversized tool results
      [defense ②] snipCompactIfNeeded        — remove stale middle messages
      [defense ③] microcompactMessages       — time-based tool-result clearing
      [defense ④] applyCollapsesIfNeeded     — not implemented (CONTEXT_COLLAPSE feature)
      [check]     isAtBlockingLimit           — exit if still too full
      [defense ⑤] autocompact (compact)      — full LLM summarisation

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

        # Mirrors: yield { type: 'stream_request_start' } in query.ts queryLoop.
        # Signals to callers (e.g. UI layer, IDE server) that a new API request
        # is about to be sent so they can show a "thinking" indicator.
        yield {"type": "stream_request_start"}

        # ── Compaction pipeline (mirrors queryLoop in query.ts) ──────────────
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

        # Step C — [defense ③] time-based microcompact.
        # Mirrors: microcompactMessages(messagesForQuery, toolUseContext, querySource)
        # Clears old compactable tool results when the server cache has expired.
        mc_result = microcompact_messages(messages_for_query)
        messages_for_query = mc_result["messages"]
        snip_tokens_freed += mc_result.get("tokens_saved", 0)

        # Step D — blocking-limit check.
        # Subtract snip_tokens_freed (snip + microcompact) from the running estimate
        # so successful clearing doesn't immediately retrigger a heavier step.
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

        # Step E — [defense ⑤] autocompact when above the autocompact threshold.
        # Mirrors: isAtWarningLimit → runAutoCompact(messagesForQuery)
        if _warning_state["isAboveAutoCompactThreshold"]:
            pre_compact_token_count = _raw_count
            compact_result = await compact(
                messages_for_query,
                api_client,
                is_auto_compact=True,
                pre_compact_token_count=pre_compact_token_count,
                cwd=ctx.cwd,
                session_id=ctx.session_id,
                transcript_path=str(ctx.session_transcript_path),
            )
            # Replace message history with the compacted slice.
            # The compact boundary marker inside compact_result[0] will be
            # found by get_messages_after_compact_boundary() on the next iteration.
            current_messages = list(compact_result)
            current_usage = None
            # Recalculate state with post-compact token count
            _token_count  = token_count_with_estimation(current_messages, current_usage)
            _warning_state = calculate_token_warning_state(_token_count)

        # Step F — strip internal metadata keys before sending to the API.
        # Mirrors: normalizeMessagesForAPI(messagesForQuery) — removes fields like
        # _compact_boundary that are valid in our internal history but rejected by
        # the OpenAI-compat endpoint.
        api_messages = normalize_messages_for_api(messages_for_query)

        # ── Call Qwen API (streaming) ─────────────────────────────────────────
        text_buffer = ""
        tool_calls_this_turn: list[dict] = []
        assistant_messages_this_turn: list[dict] = []
        stop_reason = "stop"

        try:
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

        except Exception as api_error:
            # Mirrors the catch block in query.ts queryLoop: if an error occurs
            # mid-stream after tool_use blocks have been emitted, we must synthesize
            # tool_result error blocks for each orphaned tool_use — otherwise the
            # message history will be malformed (tool_use without tool_result).
            error_text = str(api_error)
            if tool_calls_this_turn and not assistant_messages_this_turn:
                assistant_messages_this_turn.append({
                    "role": "assistant",
                    "content": text_buffer,
                    "tool_calls": tool_calls_this_turn,
                })
            for missing_msg in _yield_missing_tool_result_blocks(
                assistant_messages_this_turn, error_text
            ):
                current_messages.append(missing_msg)
                yield {"type": "tool_result_message", "message": missing_msg}
            yield {"type": "error", "message": error_text}
            yield {"type": "done", "reason": "model_error", "usage": current_usage}
            return

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
        asst_msg: dict | None = None
        if tool_calls_this_turn:
            asst_msg = make_assistant_tool_call_message(
                tool_calls_this_turn, text=text_buffer or None
            )
        elif text_buffer:
            asst_msg = make_assistant_text_message(text_buffer)

        if asst_msg is not None:
            # Attach timestamp for time-based microcompact to measure inactivity gaps.
            # Mirrors the timestamps stored on AssistantMessage in the source.
            asst_msg["_timestamp"] = time.time()
            current_messages.append(asst_msg)
            assistant_messages_this_turn.append(asst_msg)
            yield {"type": "assistant_message", "message": asst_msg}

        # ④ No tool calls → check Stop hooks, then done
        # Mirrors the Stop hook path in query.ts: executeStopHooks() is called
        # when there are no tool calls; exit code 2 re-injects the feedback as a
        # user message and continues the loop (model keeps working).
        if not tool_calls_this_turn:
            stop_hook_result = await execute_stop_hooks(
                last_assistant_message=text_buffer or None,
                cwd=ctx.cwd,
                session_id=ctx.session_id,
                transcript_path=str(ctx.session_transcript_path),
                permission_mode=ctx.permission_mode,
                stop_hook_active=stop_hook_active,
            )
            if stop_hook_result.get("block"):
                # Exit code 2 → inject feedback and let the model continue.
                # Mirrors the re-injection path in query.ts for Stop hooks.
                feedback = stop_hook_result.get("block_reason", "Stop hook requested continuation")
                feedback_msg = {"role": "user", "content": feedback}
                current_messages.append(feedback_msg)
                yield {"type": "injected_message", "message": feedback_msg}
                # Mark stop_hook_active so re-entrant Stop hooks can detect the cycle.
                stop_hook_active = True
                continue
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
