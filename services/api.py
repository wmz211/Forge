from __future__ import annotations
import json
import os
from typing import AsyncGenerator, Any

from openai import AsyncOpenAI

QWEN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = "qwen3-coder-plus"
MAX_TOKENS = 8192

# Set FORGE_ENABLE_THINKING=1 to enable Qwen3 extended thinking.
# Mirrors the extended-thinking / thinkingConfig support in Claude Code's
# services/api/claude.ts — thinking blocks improve reasoning quality on hard
# problems at the cost of higher latency and token usage.
_THINKING_ENABLED = os.environ.get("FORGE_ENABLE_THINKING", "").lower() in (
    "1", "true", "yes"
)


class QwenClient:
    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        enable_thinking: bool | None = None,
    ):
        self.model = model
        # Per-instance thinking toggle; falls back to env var
        self._thinking = _THINKING_ENABLED if enable_thinking is None else enable_thinking
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=QWEN_BASE_URL,
        )

    async def stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        system_prompt: str | None = None,
    ) -> AsyncGenerator[dict, None]:
        """
        Stream a chat completion from Qwen.

        Yields dicts:
          {"type": "text", "content": str}
          {"type": "thinking", "content": str}          — Qwen3 thinking blocks
          {"type": "tool_call", "id": str, "name": str, "arguments": dict}
          {"type": "done", "stop_reason": str, "usage": dict | None}

        The "done" event includes usage counts from the API response so that
        token_count_with_estimation() in compact.py can use real values instead
        of character-based estimates. Mirrors tokenCountFromLastAPIResponse()
        in tokens.ts which reads usage from the API response object.

        Qwen3 thinking blocks
        ---------------------
        When enable_thinking=True, Qwen3 returns reasoning content in the
        `reasoning_content` delta field (OpenAI-compat streaming) before the
        main text response.  We emit these as {"type": "thinking"} events so
        the UI can optionally render them.  The thinking content is NOT included
        in the message history sent back to the model (it's ephemeral context
        from the model's own reasoning step), mirroring the thinking-block
        preservation rules in Claude Code's query.ts.
        """
        api_messages = []
        if system_prompt:
            api_messages.append({"role": "system", "content": system_prompt})
        api_messages.extend(messages)

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": api_messages,
            "max_tokens": MAX_TOKENS,
            "stream": True,
            "stream_options": {"include_usage": True},
            "extra_body": {"enable_thinking": self._thinking},
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        pending_calls: dict[int, dict] = {}
        stop_reason = "stop"
        usage: dict | None = None

        async with await self._client.chat.completions.create(**kwargs) as stream:
            async for chunk in stream:
                # Usage is provided in the final streaming chunk (stream_options)
                if hasattr(chunk, "usage") and chunk.usage is not None:
                    usage = {
                        "prompt_tokens": chunk.usage.prompt_tokens,
                        "completion_tokens": chunk.usage.completion_tokens,
                    }

                choice = chunk.choices[0] if chunk.choices else None
                if not choice:
                    continue

                delta = choice.delta
                stop_reason = choice.finish_reason or stop_reason

                # ── Thinking blocks (Qwen3 extended reasoning) ──────────────
                # reasoning_content is the Qwen3 field for thinking output.
                # Mirrors the thinking_blocks handling in Claude Code's
                # services/api/claude.ts — yielded separately so the UI can
                # show/hide them without affecting the assistant text buffer.
                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    yield {"type": "thinking", "content": reasoning}

                if delta.content:
                    yield {"type": "text", "content": delta.content}

                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in pending_calls:
                            pending_calls[idx] = {"id": "", "name": "", "arguments": ""}
                        if tc.id:
                            pending_calls[idx]["id"] = tc.id
                        if tc.function and tc.function.name:
                            pending_calls[idx]["name"] += tc.function.name
                        if tc.function and tc.function.arguments:
                            pending_calls[idx]["arguments"] += tc.function.arguments

        for idx in sorted(pending_calls):
            call = pending_calls[idx]
            try:
                args = json.loads(call["arguments"]) if call["arguments"] else {}
            except json.JSONDecodeError:
                args = {"_raw": call["arguments"]}
            yield {
                "type": "tool_call",
                "id": call["id"],
                "name": call["name"],
                "arguments": args,
            }

        yield {"type": "done", "stop_reason": stop_reason, "usage": usage}
