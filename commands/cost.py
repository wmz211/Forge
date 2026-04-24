"""
/cost — Show token usage and estimated cost for this session.
Mirrors Claude Code's /cost command in commands/cost/cost.ts.

Claude Code calls formatTotalCost() which sums all API responses' token counts.
We track usage via engine._last_usage and a per-session accumulator.
Pricing: Qwen3-coder-plus (dashscope), approximate USD rates.
"""
from __future__ import annotations
import commands as _reg

# Approximate Qwen3-coder-plus pricing (USD per million tokens, 2025)
# Input: $3.50/M tokens, Output: $7.00/M tokens (non-cached)
_INPUT_PRICE_PER_M  = 3.50
_OUTPUT_PRICE_PER_M = 7.00


async def call(args: str, engine) -> str:
    total_in  = getattr(engine, "_total_input_tokens",  0)
    total_out = getattr(engine, "_total_output_tokens", 0)

    cost_in  = total_in  / 1_000_000 * _INPUT_PRICE_PER_M
    cost_out = total_out / 1_000_000 * _OUTPUT_PRICE_PER_M
    total_cost = cost_in + cost_out

    lines = [
        "\033[1mToken usage this session:\033[0m",
        f"  Input tokens  : \033[36m{total_in:,}\033[0m",
        f"  Output tokens : \033[36m{total_out:,}\033[0m",
        f"  Total tokens  : \033[1;36m{total_in + total_out:,}\033[0m",
        "",
        f"  Estimated cost: \033[1;33m${total_cost:.4f} USD\033[0m",
        f"  (Input ${cost_in:.4f} + Output ${cost_out:.4f})",
        "",
        "\033[90m  Rates: Qwen3-coder-plus ~$3.50/M input, ~$7.00/M output\033[0m",
    ]
    return "\n".join(lines)


_reg.register({
    "name": "cost",
    "description": "Show token usage and estimated cost for this session",
    "call": call,
})
