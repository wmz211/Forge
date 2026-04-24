"""
/permissions - View or modify permission rules by source.

Supported sources:
  session (default), user, project, local
"""
from __future__ import annotations

import commands as _reg
from permissions import PERMISSION_MODES, explain_permission
from permission_rules import RULE_SOURCE_ORDER, source_label


_SOURCE_ALIASES = {
    "session": "session",
    "user": "userSettings",
    "project": "projectSettings",
    "local": "localSettings",
    "flag": "flagSettings",
    "policy": "policySettings",
    "cliarg": "cliArg",
    "command": "command",
}


def _parse_source(raw: str | None) -> str | None:
    if not raw:
        return "session"
    key = raw.strip().lower()
    if key == "all":
        return "all"
    return _SOURCE_ALIASES.get(key)


async def call(args: str, engine) -> str:
    parts = args.strip().split()
    if not parts:
        return _show(engine)

    subcmd = parts[0].lower()
    value = parts[1] if len(parts) > 1 else ""
    src = _parse_source(parts[2] if len(parts) > 2 else None)

    if subcmd in ("allow", "deny", "ask") and value:
        if src is None or src == "all":
            return "  Invalid source. Use: session | user | project | local | cliArg | command | flag | policy"
        ok = engine.add_permission_rule(subcmd, value, src)
        if not ok:
            return (
                f"  \033[31mFailed to add rule:\033[0m {value}\n"
                f"  Source [{source_label(src)}] is read-only or unavailable for writes."
            )
        return f"  \033[32mRule added:\033[0m {subcmd} {value} [{source_label(src)}]"

    if subcmd == "clear" and value:
        if src is None:
            return "  Invalid source. Use: session | user | project | local | cliArg | command | flag | policy | all"
        result = engine.clear_permission_rule(value, None if src == "all" else src)
        where = "all sources" if src == "all" else source_label(src)
        blocked = result.get("blocked", [])
        note = ""
        if blocked:
            note = (
                "\n  Skipped read-only sources: "
                + ", ".join(source_label(s) for s in blocked)
            )
        return (
            f"  \033[33mRule cleared:\033[0m {value} [{where}]"
            f"\n  Removed entries: {result.get('cleared', 0)}"
            f"{note}"
        )

    if subcmd == "mode" and value:
        if value not in PERMISSION_MODES:
            return f"  \033[31mUnknown mode:\033[0m {value}\n  Modes: {', '.join(PERMISSION_MODES)}"
        engine.permission_mode = value
        return f"  \033[32mMode set to:\033[0m {value}"

    if subcmd == "why":
        if len(parts) < 2:
            return "  Usage: /permissions why <tool> [command]"
        tool = parts[1]
        raw_command = args.strip().split(None, 2)[2] if len(args.strip().split(None, 2)) > 2 else ""
        tool_input = {"command": raw_command} if raw_command else None
        explanation = explain_permission(
            tool_name=tool,
            mode=engine.permission_mode,
            tool_input=tool_input,
            always_allow_rules=engine.get_effective_rules_with_source("allow"),
            always_deny_rules=engine.get_effective_rules_with_source("deny"),
            always_ask_rules=engine.get_effective_rules_with_source("ask"),
        )
        return _render_why(explanation, raw_command)

    return (
        "  Usage:\n"
        "    /permissions\n"
        "    /permissions allow <rule> [session|user|project|local|cliArg|command]\n"
        "    /permissions deny  <rule> [session|user|project|local|cliArg|command]\n"
        "    /permissions ask   <rule> [session|user|project|local|cliArg|command]\n"
        "    /permissions clear <rule> [session|user|project|local|cliArg|command|flag|policy|all]\n"
        "    /permissions why   <tool> [command]\n"
        "    /permissions mode  <mode>"
    )


def _show(engine) -> str:
    lines = [
        "\033[1mPermission configuration:\033[0m",
        f"  Mode : \033[33m{engine.permission_mode}\033[0m",
        "",
    ]
    lines += _render_behavior_block("allow", "\033[32m")
    lines += _render_rules_by_source(engine, "allow")
    lines += ["", *(_render_behavior_block("deny", "\033[31m"))]
    lines += _render_rules_by_source(engine, "deny")
    lines += ["", *(_render_behavior_block("ask", "\033[33m"))]
    lines += _render_rules_by_source(engine, "ask")
    lines += [
        "",
        "  Source order: " + " -> ".join(source_label(s) for s in RULE_SOURCE_ORDER),
        "  Enabled setting sources: "
        + ", ".join(source_label(s) for s in engine.get_enabled_sources()),
        f"  Policy source origin: {engine.get_policy_origin()}",
        "  Read-only sources: flag, policy",
        "  Modes available: " + "  ".join(PERMISSION_MODES),
    ]
    return "\n".join(lines)


def _render_behavior_block(name: str, color: str) -> list[str]:
    return [f"  Always-{name} rules by source:"]


def _render_rules_by_source(engine, behavior: str) -> list[str]:
    rules_by_source = engine.get_rules_by_source(behavior)
    out: list[str] = []
    has_any = False
    for src in RULE_SOURCE_ORDER:
        rules = rules_by_source.get(src, [])
        if not rules:
            continue
        has_any = True
        out.append(f"    [{source_label(src)}]")
        for rule in rules:
            out.append(f"      * {rule}")
    if not has_any:
        out.append("    (none)")
    return out


def _render_why(explanation: dict, raw_command: str) -> str:
    matched = explanation.get("matched", {})
    final = explanation.get("final", {})
    tool_mode_result = explanation.get("toolModeResult", {})
    lines = [
        "\033[1mPermission Why\033[0m",
        f"  Tool : {explanation.get('tool')}",
        f"  Mode : \033[33m{explanation.get('mode')}\033[0m",
    ]
    if raw_command:
        lines.append(f"  Input: {raw_command}")
    lines += ["", "  Evaluation order:"]

    deny = matched.get("deny")
    ask = matched.get("ask")
    allow = matched.get("allow")
    if deny:
        lines.append(f"    1) deny matched: {deny['rule']}  [source: {deny['sourceDisplay']}]")
    else:
        lines.append("    1) deny matched: (none)")
    if ask:
        lines.append(f"    2) ask matched : {ask['rule']}  [source: {ask['sourceDisplay']}]")
    else:
        lines.append("    2) ask matched : (none)")
    lines.append(
        f"    3) tool/mode   : {tool_mode_result.get('behavior', 'unknown')}"
    )
    if allow:
        lines.append(f"    4) allow matched: {allow['rule']}  [source: {allow['sourceDisplay']}]")
    else:
        lines.append("    4) allow matched: (none)")
    lines.append("    5) passthrough->ask, then dontAsk transform if needed")

    behavior = str(final.get("behavior", "unknown")).upper()
    color = "32" if behavior == "ALLOW" else "31" if behavior == "DENY" else "33"
    lines += ["", f"  Final: \033[{color}m{behavior}\033[0m"]
    message = final.get("message")
    if message:
        lines.append(f"  Reason: {message}")
    decision = final.get("decisionReason")
    if isinstance(decision, dict) and decision.get("type") == "rule":
        rule = decision.get("rule", {})
        rv = rule.get("ruleValue", {})
        t = rv.get("toolName", "")
        c = rv.get("ruleContent")
        display = f"{t}({c})" if c else str(t)
        lines.append(
            f"  Matched decision rule: {display}  [source: {rule.get('source')}]"
        )
    return "\n".join(lines)


_reg.register(
    {
        "name": "permissions",
        "description": "View or modify permission rules by source",
        "argument_hint": "[allow|deny|ask|clear|mode ...]",
        "call": call,
    }
)
