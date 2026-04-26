"""
CronCreate / CronDelete / CronList tools.

Mirrors src/tools/ScheduleCronTool/{CronCreateTool,CronDeleteTool,CronListTool}.ts.

Feature-gated: enabled only when env var FORGE_AGENT_TRIGGERS=1.
Provides in-session scheduling (no disk persistence unless FORGE_CRON_DURABLE=1).

Session-local cron store: list[dict] kept in module-level _CRON_TASKS so all
three tools share state within a single server/session process.
"""
from __future__ import annotations
import os
import re
import uuid
import time
from typing import Optional
from tool import Tool, ToolContext

CRON_CREATE_TOOL_NAME = "CronCreate"
CRON_DELETE_TOOL_NAME = "CronDelete"
CRON_LIST_TOOL_NAME = "CronList"

_DEFAULT_MAX_AGE_DAYS = 30
MAX_JOBS = 50

# In-memory cron task store (session-only by default).
_CRON_TASKS: list[dict] = []


def _is_enabled() -> bool:
    return os.environ.get("FORGE_AGENT_TRIGGERS", "0") == "1"


def _parse_cron(expr: str) -> bool:
    """Validate a 5-field cron expression. Returns True if valid."""
    parts = expr.strip().split()
    if len(parts) != 5:
        return False
    ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
    for part, (lo, hi) in zip(parts, ranges):
        for seg in part.split(","):
            seg = seg.strip()
            if seg == "*":
                continue
            step_match = re.match(r'^\*/(\d+)$', seg)
            if step_match:
                continue
            range_match = re.match(r'^(\d+)-(\d+)$', seg)
            if range_match:
                a, b = int(range_match.group(1)), int(range_match.group(2))
                if not (lo <= a <= hi and lo <= b <= hi):
                    return False
                continue
            if re.match(r'^\d+$', seg):
                v = int(seg)
                if not (lo <= v <= hi):
                    return False
                continue
            return False
    return True


def _cron_to_human(expr: str) -> str:
    """Very basic human-readable cron description."""
    parts = expr.strip().split()
    if parts == ["*"] * 5:
        return "every minute"
    minute, hour = parts[0], parts[1]
    if minute.startswith("*/") and hour == "*":
        return f"every {minute[2:]} minutes"
    if minute == "0" and hour == "*":
        return "every hour"
    if minute == "0" and hour != "*" and parts[2] == "*":
        return f"daily at {hour}:00"
    return expr


# ─────────────────────────────────────────────────────────────────────────────


class CronCreateTool(Tool):
    """
    Schedule a prompt to run at a future time.

    Mirrors CronCreateTool in src/tools/ScheduleCronTool/CronCreateTool.ts.
    Feature-gated: FORGE_AGENT_TRIGGERS=1.
    """

    name = CRON_CREATE_TOOL_NAME
    description = "Schedule a prompt to run at a future time — recurring or one-shot."
    should_defer = True

    def is_enabled(self) -> bool:
        return _is_enabled()

    def get_schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "cron": {
                        "type": "string",
                        "description": (
                            'Standard 5-field cron expression in local time: "M H DoM Mon DoW" '
                            '(e.g. "*/5 * * * *" = every 5 minutes).'
                        ),
                    },
                    "prompt": {
                        "type": "string",
                        "description": "The prompt to enqueue at each fire time.",
                    },
                    "recurring": {
                        "type": "boolean",
                        "description": (
                            f"true (default) = fire on every cron match until deleted or "
                            f"auto-expired after {_DEFAULT_MAX_AGE_DAYS} days. "
                            "false = fire once at the next match, then auto-delete."
                        ),
                    },
                    "durable": {
                        "type": "boolean",
                        "description": (
                            "true = persist to .claude/scheduled_tasks.json and survive restarts. "
                            "false (default) = in-memory only, dies when this session ends."
                        ),
                    },
                },
                "required": ["cron", "prompt"],
            },
        }

    async def validate_input(self, tool_input: dict, ctx: ToolContext):
        cron = tool_input.get("cron", "")
        if not _parse_cron(cron):
            return False, f"Invalid cron expression '{cron}'. Expected 5 fields: M H DoM Mon DoW."
        if len(_CRON_TASKS) >= MAX_JOBS:
            return False, f"Maximum of {MAX_JOBS} scheduled jobs reached. Delete some with CronDelete."
        return True, None

    async def call(self, tool_input: dict, ctx: ToolContext) -> str:
        import json
        cron = tool_input["cron"]
        prompt = tool_input["prompt"]
        recurring = tool_input.get("recurring", True)
        durable = tool_input.get("durable", False)

        job_id = str(uuid.uuid4())[:8]
        human = _cron_to_human(cron)
        job = {
            "id": job_id,
            "cron": cron,
            "humanSchedule": human,
            "prompt": prompt,
            "recurring": recurring,
            "durable": durable,
            "createdAt": time.time(),
        }
        _CRON_TASKS.append(job)

        if durable and os.environ.get("FORGE_CRON_DURABLE"):
            _persist_cron_tasks(ctx.cwd)

        return json.dumps({
            "id": job_id,
            "humanSchedule": human,
            "recurring": recurring,
            "durable": durable,
        })


class CronDeleteTool(Tool):
    """
    Cancel a scheduled cron job by ID.

    Mirrors CronDeleteTool in src/tools/ScheduleCronTool/CronDeleteTool.ts.
    """

    name = CRON_DELETE_TOOL_NAME
    description = "Cancel a scheduled cron job by ID."
    should_defer = True

    def is_enabled(self) -> bool:
        return _is_enabled()

    def get_schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Job ID returned by CronCreate.",
                    },
                },
                "required": ["id"],
            },
        }

    async def validate_input(self, tool_input: dict, ctx: ToolContext):
        job_id = tool_input.get("id", "")
        if not any(j["id"] == job_id for j in _CRON_TASKS):
            return False, f"No scheduled job with id '{job_id}'."
        return True, None

    async def call(self, tool_input: dict, ctx: ToolContext) -> str:
        import json
        job_id = tool_input["id"]
        global _CRON_TASKS
        _CRON_TASKS = [j for j in _CRON_TASKS if j["id"] != job_id]
        return json.dumps({"id": job_id})


class CronListTool(Tool):
    """
    List all scheduled cron jobs in the current session.

    Mirrors CronListTool in src/tools/ScheduleCronTool/CronListTool.ts.
    """

    name = CRON_LIST_TOOL_NAME
    description = "List scheduled cron jobs."
    is_concurrency_safe = True
    should_defer = True

    def is_enabled(self) -> bool:
        return _is_enabled()

    def get_schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        }

    def is_read_only(self, input: dict | None = None) -> bool:
        return True

    async def call(self, tool_input: dict, ctx: ToolContext) -> str:
        import json
        jobs = [
            {
                "id": j["id"],
                "cron": j["cron"],
                "humanSchedule": j["humanSchedule"],
                "prompt": j["prompt"][:80] + ("..." if len(j["prompt"]) > 80 else ""),
                "recurring": j.get("recurring", True),
                "durable": j.get("durable", False),
            }
            for j in _CRON_TASKS
        ]
        return json.dumps({"jobs": jobs})


def _persist_cron_tasks(cwd: str) -> None:
    """Write durable tasks to .claude/scheduled_tasks.json (mirrors cronTasks.ts)."""
    import json
    from pathlib import Path
    dest = Path(cwd) / ".claude" / "scheduled_tasks.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    durable = [j for j in _CRON_TASKS if j.get("durable")]
    dest.write_text(json.dumps(durable, indent=2))
