#!/usr/bin/env python3
"""
Forge - CLI entry point.
Mirrors Claude Code's cli.tsx entrypoint.

Usage:
    python main.py [--cwd PATH] [--mode MODE] [--session-id UUID]
"""
from __future__ import annotations
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

# Command registry must be imported first so all commands self-register
import commands as cmd_registry

from query_engine import QueryEngine
from tools import AgentTool, build_builtin_tools_async
from permissions import PERMISSION_MODES
from ui import EventRenderer, render_error, render_warn

# prompt_toolkit
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.styles import Style as PtStyle

API_KEY = os.environ.get("FORGE_API_KEY") or os.environ.get("CODING_AGENT_API_KEY")
MODEL = os.environ.get("FORGE_MODEL") or os.environ.get(
    "CODING_AGENT_MODEL", "qwen3-coder-plus"
)

# Coordinator mode (mirrors CLAUDE_CODE_COORDINATOR_MODE in Claude Code)
_COORDINATOR_MODE = os.environ.get("FORGE_COORDINATOR_MODE", "").lower() in (
    "1", "true", "yes"
)

_COORDINATOR_SYSTEM_PROMPT = """\
You are a coordinator agent. Your role is to decompose complex tasks and delegate \
them to specialized sub-agents using the Agent tool. Do not use file-editing, \
bash execution, or any other direct tools yourself. Spawn the appropriate sub-agent \
for each subtask and synthesize their outputs into a final answer for the user.\
"""

BANNER = """\
\033[1;36m===============================
          Forge  v0.1
  Powered by Qwen - ReAct Loop
===============================\033[0m
Type your request, or /help for commands.
"""


class SlashCompleter(Completer):
    """
    Autocomplete for slash commands.
    Triggered whenever the buffer starts with '/'.
    Shows all matching commands + aliases, filtered as the user types.
    """
    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        word = text[1:]  # text after the leading '/'
        for cmd in cmd_registry.all_commands():
            names = [cmd["name"]] + cmd.get("aliases", [])
            hint = cmd.get("argument_hint", "")
            desc = cmd.get("description", "")
            for n in names:
                if n.startswith(word):
                    display = f"/{n}"
                    if hint:
                        display += f" {hint}"
                    yield Completion(
                        n,
                        start_position=-len(word),
                        display=display,
                        display_meta=desc,
                    )


_PT_STYLE = PtStyle.from_dict({
    "completion-menu.completion": "bg:#1c3a5c #c0d8f0",
    "completion-menu.completion.current": "bg:#005f87 #ffffff bold",
    "completion-menu.meta.completion": "bg:#162d47 #6a8fa8",
    "completion-menu.meta.completion.current": "bg:#004a6e #b0d0e8",
    "scrollbar.background": "bg:#162d47",
    "scrollbar.button": "bg:#005f87",
})


def _make_session() -> PromptSession:
    return PromptSession(
        completer=SlashCompleter(),
        complete_while_typing=True,
        style=_PT_STYLE,
        mouse_support=False,
    )


async def build_tools(cwd: str | None = None) -> list:
    return await build_builtin_tools_async(cwd=cwd)


async def run(args: argparse.Namespace) -> None:
    cwd = os.path.abspath(args.cwd)
    if not os.path.isdir(cwd):
        print(f"Error: cwd does not exist: {cwd}")
        sys.exit(1)

    tools = await build_tools(cwd)

    from services.api import QwenClient
    agent_api = QwenClient(api_key=API_KEY, model=MODEL)
    # AgentTool needs the full tool pool (including itself) so sub-agents inherit all tools.
    agent_tool = AgentTool(all_tools=tools, api_client=agent_api, max_turns=20, cwd=cwd)
    tools.append(agent_tool)
    agent_tool._all_tools = tools

    # Coordinator mode
    if _COORDINATOR_MODE:
        engine_tools = [agent_tool]
        engine_system_prompt = _COORDINATOR_SYSTEM_PROMPT
    else:
        engine_tools = tools
        engine_system_prompt = None

    engine = QueryEngine(
        api_key=API_KEY,
        model=MODEL,
        cwd=cwd,
        tools=engine_tools,
        permission_mode=args.mode,
        session_id=args.session_id or None,
        **({"system_prompt": engine_system_prompt} if engine_system_prompt else {}),
    )
    engine._total_input_tokens = 0
    engine._total_output_tokens = 0

    print(BANNER)
    print(f"  cwd     : {cwd}")
    print(f"  mode    : {args.mode}")
    print(f"  model   : {MODEL}")
    if _COORDINATOR_MODE:
        print("  role    : coordinator")
    print(f"  session : {engine.session_id}")
    print(f"  log     : {engine.transcript_path}")
    print()

    # Fire SessionStart hooks (mirrors processSessionStartHooks() in sessionStart.ts).
    # "resume" when restoring an existing session, "startup" for new sessions.
    _session_source = "resume" if engine._messages else "startup"
    _hook_msgs = await engine.process_session_start_hooks(
        source=_session_source, model=MODEL
    )
    for _hm in _hook_msgs:
        if _hm.get("output"):
            print(f"[SessionStart hook] {_hm['output']}")

    renderer = EventRenderer(tools=tools)
    session = _make_session()

    while True:
        try:
            raw = await session.prompt_async(
                HTML("<ansibrightgreen><b>></b></ansibrightgreen> ")
            )
            prompt = raw.strip()
        except KeyboardInterrupt:
            continue
        except EOFError:
            print("\nBye.")
            break

        if not prompt:
            continue

        # Slash command handling
        if prompt.startswith("/"):
            parts = prompt[1:].split(None, 1)
            name = parts[0] if parts else ""
            cmd_args = parts[1] if len(parts) > 1 else ""

            if name == "exit":
                print("Bye.")
                break

            cmd = cmd_registry.get(name)
            if cmd:
                print()
                try:
                    result = await cmd["call"](cmd_args, engine)
                    if result:
                        print(result)
                except Exception as e:
                    render_error(str(e))
                print()
                continue

            render_warn(f"Unknown command: /{name}  (try /help)")
            continue

        # Regular prompt -> agent
        print()
        renderer.reset()
        renderer.spinner.start()
        try:
            async for event in engine.submit_message(prompt):
                renderer.handle(event)
                if event.get("type") == "done":
                    usage = event.get("usage") or {}
                    engine._total_input_tokens += usage.get("prompt_tokens", 0)
                    engine._total_output_tokens += usage.get("completion_tokens", 0)
        except KeyboardInterrupt:
            # Signal all in-flight sub-agents to stop.
            engine.abort()
            renderer.spinner.stop()
            render_warn("[Interrupted]")
        except Exception as e:
            renderer.spinner.stop()
            render_error(str(e))
        print()


def main() -> None:
    if not API_KEY:
        print("Error: FORGE_API_KEY is not set.")
        print("Set it in your environment before starting Forge.")
        print("Legacy fallback CODING_AGENT_API_KEY is also supported.")
        sys.exit(1)

    parser = argparse.ArgumentParser(description="Forge - Qwen-powered coding assistant")
    parser.add_argument("--cwd", default=os.getcwd(), help="Working directory")
    parser.add_argument(
        "--mode",
        choices=list(PERMISSION_MODES),
        default="default",
        help="Permission mode (default: default)",
    )
    parser.add_argument(
        "--session-id", default=None,
        help="Resume a session by its UUID",
    )
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
