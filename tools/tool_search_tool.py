from __future__ import annotations

import re
from typing import Any

from tool import Tool, ToolContext


def _split_tool_name(name: str) -> list[str]:
    if name.startswith("mcp__"):
        raw = name[5:].replace("__", " ").replace("_", " ")
    else:
        raw = re.sub(r"([a-z])([A-Z])", r"\1 \2", name).replace("_", " ")
    return [part.lower() for part in raw.split() if part]


class ToolSearchTool(Tool):
    name = "ToolSearch"
    description = (
        'Search deferred tools. Use "select:<tool_name>" for direct selection, '
        "or keywords to search tool names, descriptions, and search hints."
    )
    is_concurrency_safe = True
    max_result_size_chars = 100_000
    always_load = True

    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools = tools or []

    def set_tools(self, tools: list[Tool]) -> None:
        self._tools = tools

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        'Query to find deferred tools. Use "select:<tool_name>" '
                        "for direct selection, or keywords to search."
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return (default: 5)",
                },
            },
            "required": ["query"],
        }

    def render_call_summary(self, args: dict[str, Any]) -> str | None:
        return str(args.get("query") or "")

    async def call(self, input: dict[str, Any], ctx: ToolContext) -> str:
        query = str(input.get("query") or "").strip()
        max_results = int(input.get("max_results") or 5)
        max_results = max(1, min(max_results, 20))

        deferred = [t for t in self._tools if getattr(t, "should_defer", False)]
        all_tools = list(self._tools)

        select_match = re.match(r"^select:(.+)$", query, flags=re.I)
        if select_match:
            requested = [item.strip() for item in select_match.group(1).split(",") if item.strip()]
            matches: list[str] = []
            missing: list[str] = []
            for name in requested:
                tool = _find_tool(deferred, name) or _find_tool(all_tools, name)
                if tool and tool.name not in matches:
                    matches.append(tool.name)
                else:
                    missing.append(name)
            return _format_result(matches, query, len(deferred), missing)

        matches = _keyword_search(query, deferred, all_tools, max_results)
        return _format_result(matches, query, len(deferred), [])


def _find_tool(tools: list[Tool], name: str) -> Tool | None:
    needle = name.lower()
    for tool in tools:
        names = [tool.name, *getattr(tool, "aliases", ())]
        if any(n.lower() == needle for n in names):
            return tool
    return None


def _keyword_search(query: str, deferred: list[Tool], all_tools: list[Tool], max_results: int) -> list[str]:
    q = query.lower().strip()
    if not q:
        return []

    exact = _find_tool(deferred, q) or _find_tool(all_tools, q)
    if exact:
        return [exact.name]

    required: list[str] = []
    optional: list[str] = []
    for term in q.split():
        if term.startswith("+") and len(term) > 1:
            required.append(term[1:])
        else:
            optional.append(term)
    scoring_terms = required + optional if required else optional

    scored: list[tuple[int, str]] = []
    for tool in deferred:
        name_parts = _split_tool_name(tool.name)
        haystack = " ".join(
            [
                tool.name.lower(),
                " ".join(name_parts),
                str(getattr(tool, "description", "")).lower(),
                str(getattr(tool, "search_hint", "")).lower(),
            ]
        )
        if required and not all(term in haystack for term in required):
            continue
        score = 0
        for term in scoring_terms:
            if term in name_parts:
                score += 10
            elif any(term in part for part in name_parts):
                score += 5
            if getattr(tool, "search_hint", "") and term in tool.search_hint.lower():
                score += 4
            if re.search(rf"\b{re.escape(term)}\b", haystack):
                score += 2
        threshold = 1 if len(scoring_terms) == 1 else 3
        if score >= threshold:
            scored.append((score, tool.name))

    scored.sort(key=lambda item: (-item[0], item[1]))
    return [name for _, name in scored[:max_results]]


def _format_result(matches: list[str], query: str, total_deferred: int, missing: list[str]) -> str:
    payload = {
        "matches": matches,
        "query": query,
        "total_deferred_tools": total_deferred,
    }
    lines = [
        f"<tool_search_result>{payload}</tool_search_result>",
    ]
    if matches:
        lines.append("Matching deferred tools:")
        lines.extend(f"- {name}" for name in matches)
    else:
        lines.append("No matching deferred tools found.")
    if missing:
        lines.append("Missing requested tools: " + ", ".join(missing))
    return "\n".join(lines)
