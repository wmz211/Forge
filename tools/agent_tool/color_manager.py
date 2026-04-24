"""
Agent color manager — assigns a distinct ANSI color to each sub-agent type.
Mirrors src/tools/AgentTool/agentColorManager.ts.

The original stores colors in a global AppState map (getAgentColorMap()).
Here we use a module-level dict for the same effect within a session.
"""
from __future__ import annotations

# Mirrors AGENT_COLORS in agentColorManager.ts
AGENT_COLORS: tuple[str, ...] = (
    "red", "blue", "green", "yellow", "purple", "cyan", "magenta",
)

# ANSI escape codes (256-color approximations of the original theme colors)
_ANSI: dict[str, str] = {
    "red":     "\033[31m",
    "blue":    "\033[34m",
    "green":   "\033[32m",
    "yellow":  "\033[33m",
    "purple":  "\033[35m",
    "cyan":    "\033[36m",
    "magenta": "\033[95m",
}
_RESET = "\033[0m"
_BOLD  = "\033[1m"

# Module-level state: agent_type -> color_name
# Mirrors getAgentColorMap() reading from bootstrap/state.js
_color_map: dict[str, str] = {}
_next_idx: int = 0


def get_agent_color(agent_type: str) -> str:
    """
    Return the ANSI start code for this agent type, or '' if uncolored.
    Mirrors getAgentColor() from agentColorManager.ts:
      general-purpose → undefined (no color)
      others          → assigned color from the palette
    """
    if agent_type == "general-purpose":
        return ""
    color_name = _color_map.get(agent_type)
    return _ANSI.get(color_name, "") if color_name else ""


def assign_agent_color(agent_type: str) -> str:
    """
    Assign a palette color to agent_type if not already assigned, then return
    its ANSI start code.  Mirrors setAgentColor() in agentColorManager.ts.
    """
    global _next_idx
    if agent_type == "general-purpose":
        return ""
    if agent_type not in _color_map:
        _color_map[agent_type] = AGENT_COLORS[_next_idx % len(AGENT_COLORS)]
        _next_idx += 1
    return get_agent_color(agent_type)


def color_label(agent_type: str) -> str:
    """
    Return a colored, bold agent-type label: e.g. '\033[34m[Explore]\033[0m'.
    Used in progress messages the same way UI.tsx renders the agent badge.
    """
    code = get_agent_color(agent_type)
    label = f"[{agent_type}]"
    if not code:
        return label
    return f"{code}{_BOLD}{label}{_RESET}"


def reset() -> None:
    """Clear all assignments (useful between test sessions)."""
    global _next_idx
    _color_map.clear()
    _next_idx = 0
