"""
/help — Show all available slash commands.
Mirrors Claude Code's /help command.
"""
from __future__ import annotations
import commands as _reg


async def call(args: str, engine) -> str:
    lines = ["\033[1mAvailable commands:\033[0m\n"]
    for cmd in sorted(_reg.all_commands(), key=lambda c: c["name"]):
        name = cmd["name"]
        hint = cmd.get("argument_hint", "")
        aliases = cmd.get("aliases", [])
        desc = cmd.get("description", "")
        alias_str = f"  (alias: {', '.join(aliases)})" if aliases else ""
        lines.append(f"  \033[1;36m/{name}\033[0m {hint}")
        lines.append(f"      {desc}{alias_str}")
    return "\n".join(lines)


_reg.register({
    "name": "help",
    "description": "Show all available slash commands",
    "aliases": ["?"],
    "call": call,
})
