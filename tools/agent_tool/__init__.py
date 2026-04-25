"""
AgentTool — schema + call dispatcher.
Mirrors the public surface of src/tools/AgentTool/AgentTool.tsx.

Execution logic has been extracted to run_agent.py (mirrors runAgent.ts).
Background task lifecycle lives in background.py (mirrors LocalAgentTask.tsx).
Agent definitions live in built_in_agents.py (mirrors built-in/*.ts).
Colour assignment lives in color_manager.py (mirrors agentColorManager.ts).
Worktree management lives in worktree.py (mirrors utils/worktree.ts).
Resume logic lives in resume_agent.py (mirrors resumeAgent.ts).

Responsibilities kept here (mirroring AgentTool.tsx):
  • Tool schema definition (description, prompt, subagent_type, run_in_background, isolation)
  • Dynamic description text (formatAgentLine equivalent)
  • Tool-pool construction per agent definition (_resolve_tools)
  • Top-level call() dispatch: foreground / background / worktree setup / teardown
"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from tool import Tool, ToolContext

from .built_in_agents import (
    AgentDefinition,
    AGENT_TOOL_NAME,
    ONE_SHOT_AGENT_TYPES,
    get_agent_by_type,
    get_built_in_agents,
)
from .color_manager import assign_agent_color, color_label
from .run_agent import run_agent, AgentAbortError
from .worktree import create_worktree, remove_worktree
from . import background as _bg
from utils.file_state_cache import create_empty_cache

ASYNC_AGENT_ALLOWED_TOOLS = frozenset({
    "Read",
    "WebSearch",
    "TodoWrite",
    "Grep",
    "WebFetch",
    "Glob",
    "Bash",
    "PowerShell",
    "Edit",
    "Write",
    "NotebookEdit",
    "ToolSearch",
})

ALL_AGENT_DISALLOWED_TOOLS = frozenset({
    "Agent",
    "AskUserQuestion",
    "TaskOutput",
    "TaskStop",
    "TaskCreate",
    "TaskGet",
    "TaskList",
    "TaskUpdate",
})


# ── Tool-pool helpers ─────────────────────────────────────────────────────────

def _resolve_tools(
    definition: AgentDefinition | None,
    pool: list[Tool],
) -> list[Tool]:
    """
    Apply allowlist / denylist from an AgentDefinition to a tool pool.
    Mirrors filterToolsForAgent() + resolveAgentTools() in agentToolUtils.ts.
    """
    pool = [
        t for t in pool
        if t.name in ASYNC_AGENT_ALLOWED_TOOLS and t.name not in ALL_AGENT_DISALLOWED_TOOLS
    ]

    if definition is None:
        tools = list(pool)
    elif definition.tools is None or definition.tools == ["*"]:
        tools = list(pool)
    else:
        allow = set(definition.tools)
        tools = [t for t in pool if t.name in allow]

    if definition and definition.disallowed_tools:
        deny = set(definition.disallowed_tools)
        tools = [t for t in tools if t.name not in deny]

    for tool in tools:
        if tool.name == "ToolSearch" and hasattr(tool, "set_tools"):
            try:
                tool.set_tools(tools)
            except Exception:
                pass

    return tools


# ── AgentTool ─────────────────────────────────────────────────────────────────

class AgentTool(Tool):
    name = AGENT_TOOL_NAME
    description = ""          # built dynamically; see _build_description()
    is_concurrency_safe = True # mirrors isConcurrencySafe() → true

    def __init__(
        self,
        all_tools: list[Tool],
        api_client: Any,
        max_turns: int = 20,
        # abort_event is shared across all sync sub-agents spawned from the same
        # parent session; set by QueryEngine.abort() on Ctrl+C
        abort_event: asyncio.Event | None = None,
    ) -> None:
        self._all_tools = all_tools
        self._api_client = api_client
        self._max_turns = max_turns
        self._abort_event = abort_event or asyncio.Event()

    # ── Schema ────────────────────────────────────────────────────────────────

    def _build_description(self) -> str:
        """
        Dynamic description listing all agent types.
        Mirrors formatAgentLine() + getPrompt() in AgentTool/prompt.ts.
        """
        agents = get_built_in_agents()
        lines: list[str] = [
            "Launch a new agent to handle complex, multi-step tasks. "
            "Each agent type has specific capabilities and tools available to it.\n",
            "Available agent types and the tools they have access to:",
        ]
        for agent in agents:
            if agent.tools is None and not agent.disallowed_tools:
                tools_desc = "All tools"
            elif agent.tools is None and agent.disallowed_tools:
                tools_desc = "All tools except " + ", ".join(agent.disallowed_tools)
            elif agent.tools:
                deny = set(agent.disallowed_tools or [])
                effective = [t for t in agent.tools if t not in deny]
                tools_desc = ", ".join(effective) if effective else "None"
            else:
                tools_desc = "All tools"
            lines.append(
                f"- {agent.agent_type}: {agent.when_to_use} (Tools: {tools_desc})"
            )
        lines.append(
            "\n**IMPORTANT:** Before spawning a new agent, check if there is already "
            "a running or recently completed agent that you can continue. "
            "Only use this tool when the user explicitly says to use a subagent, "
            "or names one of the agent types above."
        )
        return "\n".join(lines)

    def get_schema(self) -> dict:
        agent_types = [a.agent_type for a in get_built_in_agents()]
        return {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "A short (3-5 word) description of the task",
                },
                "prompt": {
                    "type": "string",
                    "description": "The task for the agent to perform",
                },
                "subagent_type": {
                    "type": "string",
                    "enum": agent_types,
                    "description": (
                        "The type of specialized agent to use for this task. "
                        "If omitted, uses the general-purpose agent."
                    ),
                },
                "run_in_background": {
                    "type": "boolean",
                    "description": (
                        "Set to true to run this agent in the background. "
                        "You will be automatically notified when it completes — "
                        "do NOT sleep, poll, or proactively check on its progress. "
                        "Continue with other work or respond to the user instead."
                    ),
                },
                "isolation": {
                    "type": "string",
                    "enum": ["worktree"],
                    "description": (
                        'Isolation mode. "worktree" creates a temporary git worktree '
                        "so the agent works on an isolated copy of the repo."
                    ),
                },
                "model": {
                    "type": "string",
                    "description": (
                        "Optional model override for this agent. Takes precedence over "
                        "the agent definition's model. If omitted, inherits from the parent."
                    ),
                },
            },
            "required": ["description", "prompt"],
        }

    def to_openai_tool(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self._build_description(),
                "parameters": self.get_schema(),
            },
        }

    def render_call_summary(self, args: dict[str, Any]) -> str | None:
        agent_type = args.get("subagent_type") or "general-purpose"
        desc = args.get("description", "")
        flags = ""
        if args.get("run_in_background"):
            flags += " [background]"
        if args.get("isolation") == "worktree":
            flags += " [worktree]"
        return f"{agent_type}: {desc}{flags}" if desc else f"{agent_type}{flags}"

    # ── Tool-pool construction ────────────────────────────────────────────────

    def _make_sub_agent_pool(self) -> tuple[list[Tool], "AgentTool"]:
        """
        Build the tool pool for a sub-agent.
        Mirrors assembleToolPool() in tools.ts + filterToolsForAgent().

        Returns (full_pool, sub_agent_tool) where:
          full_pool       — all parent tools minus this AgentTool
          sub_agent_tool  — a fresh AgentTool with halved max_turns,
                            injected back so sub-agents can recurse
        """
        base_pool = [t for t in self._all_tools if t.name != self.name]
        sub_agent_tool = AgentTool(
            all_tools=base_pool,
            api_client=self._api_client,
            max_turns=max(5, self._max_turns // 2),
            abort_event=self._abort_event,   # share abort signal chain
        )
        return base_pool + [sub_agent_tool], sub_agent_tool

    # ── Main call ─────────────────────────────────────────────────────────────

    async def call(self, input: dict[str, Any], ctx: ToolContext) -> str:  # noqa: A002
        description: str  = input.get("description") or "Agent task"
        prompt: str       = input["prompt"]
        subagent_type: str = input.get("subagent_type") or "general-purpose"
        run_in_bg: bool   = bool(input.get("run_in_background", False))
        isolation: str | None = input.get("isolation")
        model_override: str | None = input.get("model") or None

        # Abort check before even starting
        if self._abort_event.is_set():
            return "<error>Agent aborted before start.</error>"

        # Resolve agent definition
        definition = get_agent_by_type(subagent_type)
        if definition is None:
            subagent_type = "general-purpose"
            definition = get_agent_by_type("general-purpose")

        assign_agent_color(subagent_type)
        badge = color_label(subagent_type)

        full_pool, _ = self._make_sub_agent_pool()
        sub_tools = _resolve_tools(definition, full_pool)

        system_prompt = (
            definition.get_system_prompt() if definition else
            "You are a focused sub-agent. Complete the task and return a clear summary."
        )

        # Worktree setup
        agent_cwd = ctx.cwd
        worktree_dir: str | None = None
        worktree_branch: str | None = None

        if isolation == "worktree":
            wt_result = await create_worktree(ctx.cwd)
            if isinstance(wt_result, str):          # error string
                return wt_result
            worktree_dir, worktree_branch = wt_result
            agent_cwd = worktree_dir
            print(f"\n  {badge} Worktree: {worktree_dir}")

        bg_note = " (background)" if run_in_bg else ""
        print(f"\n  {badge} Starting{bg_note}: {description}")

        # Resolve session transcript path from ctx (populated by QueryEngine)
        session_transcript_path: Path = getattr(
            ctx, "session_transcript_path",
            Path.home() / ".claude" / "projects" / "unknown" / "session.jsonl",
        )
        parent_session_id: str = getattr(ctx, "session_id", "")

        agent_id = str(uuid.uuid4())

        try:
            if run_in_bg:
                return await self._launch_background(
                    agent_id=agent_id,
                    description=description,
                    prompt=prompt,
                    subagent_type=subagent_type,
                    sub_tools=sub_tools,
                    system_prompt=system_prompt,
                    agent_cwd=agent_cwd,
                    ctx=ctx,
                    badge=badge,
                    worktree_dir=worktree_dir,
                    worktree_branch=worktree_branch,
                    session_transcript_path=session_transcript_path,
                    parent_session_id=parent_session_id,
                    model_override=model_override,
                )

            result = await run_agent(
                prompt=prompt,
                tools=sub_tools,
                api_client=self._api_client,
                cwd=agent_cwd,
                base_system_prompt=system_prompt,
                permission_mode=ctx.permission_mode,
                always_allow=getattr(ctx, "always_allow", []),
                always_deny=getattr(ctx, "always_deny", []),
                always_ask=getattr(ctx, "always_ask", []),
                max_turns=self._max_turns,
                agent_id=agent_id,
                agent_type=subagent_type,
                parent_session_id=parent_session_id,
                description=description,
                session_transcript_path=session_transcript_path,
                abort_event=self._abort_event,
                file_state_cache=create_empty_cache(),
                worktree_path=worktree_dir,
                model_override=model_override,
            )

            print(f"  {badge} Done.")

            worktree_note = ""
            if worktree_dir:
                worktree_note = await remove_worktree(worktree_dir, worktree_branch, ctx.cwd)

            if not result:
                result = "(agent produced no output)"

            # ONE_SHOT agents skip the agentId footer (mirrors ONE_SHOT_BUILTIN_AGENT_TYPES)
            if subagent_type in ONE_SHOT_AGENT_TYPES:
                return result + (f"\n\n{worktree_note}" if worktree_note else "")

            footer = f"\n\n[Agent: {subagent_type} | agentId: {agent_id[:8]} | Status: completed]"
            return result + (f"\n\n{worktree_note}" if worktree_note else "") + footer

        except AgentAbortError:
            if worktree_dir:
                await remove_worktree(worktree_dir, worktree_branch, ctx.cwd)
            return f"<error>Agent '{subagent_type}' was aborted.</error>"
        except Exception as exc:
            if worktree_dir:
                await remove_worktree(worktree_dir, worktree_branch, ctx.cwd)
            return f"<error>Agent '{subagent_type}' failed: {exc}</error>"

    # ── Background launch ─────────────────────────────────────────────────────

    async def _launch_background(
        self,
        *,
        agent_id: str,
        description: str,
        prompt: str,
        subagent_type: str,
        sub_tools: list[Tool],
        system_prompt: str,
        agent_cwd: str,
        ctx: ToolContext,
        badge: str,
        worktree_dir: str | None,
        worktree_branch: str | None,
        session_transcript_path: Path,
        parent_session_id: str,
        model_override: str | None = None,
    ) -> str:
        """
        Fire-and-forget via asyncio.create_task().
        Mirrors registerAsyncAgent() + runAsyncAgentLifecycle() in agentToolUtils.ts.
        """
        task_record = _bg.register_task(
            description=description,
            prompt=prompt,
            agent_type=subagent_type,
        )
        # Use the pre-assigned agent_id for metadata consistency
        # (background registry uses its own UUID, but we record original for sidechain)
        bg_agent_id = task_record.agent_id
        # Wire sidechain metadata so complete/fail can persist .done.json
        task_record.session_transcript_path = session_transcript_path
        task_record.sidechain_agent_id = agent_id

        async def _runner() -> None:
            try:
                result = await run_agent(
                    prompt=prompt,
                    tools=sub_tools,
                    api_client=self._api_client,
                    cwd=agent_cwd,
                    base_system_prompt=system_prompt,
                    permission_mode=ctx.permission_mode,
                    always_allow=getattr(ctx, "always_allow", []),
                    always_deny=getattr(ctx, "always_deny", []),
                    always_ask=getattr(ctx, "always_ask", []),
                    max_turns=self._max_turns,
                    agent_id=agent_id,
                    agent_type=subagent_type,
                    parent_session_id=parent_session_id,
                    description=description,
                    session_transcript_path=session_transcript_path,
                    abort_event=task_record.abort_event,
                    file_state_cache=create_empty_cache(),
                    worktree_path=worktree_dir,
                    model_override=model_override,
                )
                _bg.complete_task(bg_agent_id, result)
                print(f"\n  {badge} Background agent completed: {description}")
            except (asyncio.CancelledError, AgentAbortError):
                _bg.fail_task(bg_agent_id, "Cancelled")
            except Exception as exc:
                _bg.fail_task(bg_agent_id, str(exc))
                print(f"\n  {badge} Background agent failed ({description}): {exc}")
            finally:
                if worktree_dir:
                    await remove_worktree(worktree_dir, worktree_branch, ctx.cwd)

        asyncio_task = asyncio.create_task(_runner())
        task_record.asyncio_task = asyncio_task

        return (
            f"Agent launched in background.\n"
            f"  agentId: {bg_agent_id}\n"
            f"  description: {description}\n"
            f"  subagent_type: {subagent_type}\n"
            f"  status: running\n\n"
            "You will be automatically notified when it completes — "
            "do NOT sleep, poll, or proactively check on its progress."
        )
