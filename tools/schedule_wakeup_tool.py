"""
ScheduleWakeupTool — schedule when to resume work in /loop dynamic mode.

This tool was added after Claude Code v2.1.88 (not present in the decompiled
source) but is listed in the live session's deferred tools. It is used by the
/loop skill to self-pace iterations.

The tool registers a deferred wakeup that will re-fire the current loop prompt
after a delay. The runtime clamps delaySeconds to [60, 3600].
"""
from __future__ import annotations
import asyncio
import time
from tool import Tool, ToolContext

SCHEDULE_WAKEUP_TOOL_NAME = "ScheduleWakeup"

_DESCRIPTION = (
    "Schedule when to resume work in /loop dynamic mode — register a deferred "
    "wakeup that fires the current loop prompt after a delay."
)

_PROMPT = """\
Use this tool inside a /loop session to self-pace the next iteration.

Pass the same /loop prompt back via `prompt` each turn so the next firing
repeats the task. For an autonomous /loop with no user prompt, pass the
sentinel string "<<autonomous-loop-dynamic>>" as `prompt`.

delaySeconds is clamped to [60, 3600] by the runtime.

Picking the right delay:
  - Under 300 s: cache stays warm. Right for active polling.
  - 300 s+: pays a cache miss. Right when there's no point checking sooner.
  - Don't pick exactly 300 s — either drop to 270 s (stay in cache) or
    commit to 1200 s+ (cache miss amortised over a long wait).
  - Default for idle ticks: 1200 s–1800 s (20–30 min).

Returns confirmation of the scheduled wakeup.
"""

_MIN_DELAY = 60
_MAX_DELAY = 3600


class ScheduleWakeupTool(Tool):
    """
    ScheduleWakeupTool — schedule deferred wakeup for /loop dynamic mode.

    Input schema:
      delaySeconds: int — seconds until wakeup (clamped to [60, 3600]).
      prompt: str — the /loop prompt to fire on wakeup.
      reason: str — one-sentence explanation shown to the user.
    """

    name = SCHEDULE_WAKEUP_TOOL_NAME
    description = _DESCRIPTION
    should_defer = True
    is_concurrency_safe = True

    def get_schema(self) -> dict:
        return {
            "name": self.name,
            "description": _DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": {
                    "delaySeconds": {
                        "type": "number",
                        "description": (
                            f"Seconds from now to wake up. "
                            f"Clamped to [{_MIN_DELAY}, {_MAX_DELAY}] by the runtime."
                        ),
                    },
                    "prompt": {
                        "type": "string",
                        "description": (
                            "The /loop input to fire on wake-up. Pass the same /loop "
                            "input verbatim each turn. For autonomous /loop with no "
                            'user prompt, pass "<<autonomous-loop-dynamic>>".'
                        ),
                    },
                    "reason": {
                        "type": "string",
                        "description": (
                            "One short sentence explaining the chosen delay. "
                            "Goes to telemetry and is shown to the user. Be specific."
                        ),
                    },
                },
                "required": ["delaySeconds", "prompt", "reason"],
            },
        }

    def is_read_only(self, input: dict | None = None) -> bool:
        return True

    async def call(self, tool_input: dict, ctx: ToolContext) -> str:
        delay_raw = tool_input.get("delaySeconds", 300)
        delay = max(_MIN_DELAY, min(_MAX_DELAY, int(delay_raw)))
        prompt = tool_input.get("prompt", "")
        reason = tool_input.get("reason", "")

        fire_at = time.time() + delay

        # Store the wakeup on the context so the REPL / server layer can
        # schedule actual re-invocation. The exact mechanism depends on the
        # host (FastAPI server, REPL loop, etc.).
        pending = getattr(ctx, "pending_wakeups", None)
        if pending is None:
            ctx.pending_wakeups = []
        ctx.pending_wakeups.append({
            "fireAt": fire_at,
            "delaySeconds": delay,
            "prompt": prompt,
            "reason": reason,
        })

        import datetime
        fire_dt = datetime.datetime.fromtimestamp(fire_at).strftime("%H:%M:%S")
        return (
            f"Wakeup scheduled in {delay}s (at {fire_dt}). "
            f"Reason: {reason}"
        )
