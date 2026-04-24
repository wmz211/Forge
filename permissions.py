from __future__ import annotations

"""
Permission system aligned with Claude Code permission semantics.

References:
  - src/types/permissions.ts
  - src/utils/permissions/permissions.ts

Implemented fidelity points:
  - External permission modes include `dontAsk`
  - Rule precedence: deny > ask > tool check > allow > passthrough->ask
  - `passthrough` behavior converted to `ask`
  - `dontAsk` transforms ask decisions into deny
  - Bash compound command subcommand decision aggregation (`subcommandResults`)
"""

from dataclasses import dataclass
import fnmatch
import os
import re
from typing import Any


# Mirrors EXTERNAL_PERMISSION_MODES
PERMISSION_MODES = (
    "acceptEdits",
    "bypassPermissions",
    "default",
    "dontAsk",
    "plan",
)


@dataclass(frozen=True)
class PermissionRuleValue:
    toolName: str
    ruleContent: str | None = None


@dataclass(frozen=True)
class PermissionRule:
    source: str
    ruleBehavior: str
    ruleValue: PermissionRuleValue


def _allow(decision_reason: dict | None = None, updated_input: dict | None = None) -> dict:
    out = {"behavior": "allow"}
    if decision_reason is not None:
        out["decisionReason"] = decision_reason
    if updated_input is not None:
        out["updatedInput"] = updated_input
    return out


def _deny(message: str, decision_reason: dict) -> dict:
    return {"behavior": "deny", "message": message, "decisionReason": decision_reason}


def _ask(message: str, decision_reason: dict | None = None, updated_input: dict | None = None) -> dict:
    out = {"behavior": "ask", "message": message}
    if decision_reason is not None:
        out["decisionReason"] = decision_reason
    if updated_input is not None:
        out["updatedInput"] = updated_input
    return out


def _passthrough(message: str, decision_reason: dict | None = None) -> dict:
    out = {"behavior": "passthrough", "message": message}
    if decision_reason is not None:
        out["decisionReason"] = decision_reason
    return out


# ---- Tool classification (local approximation of tool.checkPermissions) ----
_READ_ONLY_TOOLS = frozenset(
    {
        "Read",
        "Glob",
        "Grep",
        "WebFetch",
        "WebSearch",
        "FileReadTool",
        "GlobTool",
        "GrepTool",
        "WebFetchTool",
        "WebSearchTool",
    }
)
_FILE_EDIT_TOOLS = frozenset({"Edit", "Write", "FileEditTool", "FileWriteTool"})
_FILE_READ_TOOLS = frozenset({"Read", "FileReadTool"})
_FILE_WRITE_TOOLS = frozenset({"Edit", "Write", "FileEditTool", "FileWriteTool", "NotebookEdit"})
_DANGEROUS_PATH_SEGMENTS = frozenset({
    ".git",
    ".hg",
    ".svn",
    ".claude",
    ".ssh",
    ".gnupg",
    ".aws",
    ".config",
})
_SUSPICIOUS_WINDOWS_PATH_RE = re.compile(
    r"(?i)(::\$DATA|[\\/](CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|[\\/]|$)|[. ]+[\\/])"
)

# Bash command families mirrored from tools/bash_tool.py
_SEARCH_CMDS = frozenset({"find", "grep", "rg", "ag", "ack", "locate", "which", "whereis"})
_READ_CMDS = frozenset(
    {
        "cat",
        "head",
        "tail",
        "less",
        "more",
        "wc",
        "stat",
        "file",
        "strings",
        "jq",
        "awk",
        "cut",
        "sort",
        "uniq",
        "tr",
    }
)
_LIST_CMDS = frozenset({"ls", "tree", "du"})
_NEUTRAL_CMDS = frozenset({"echo", "printf", "true", "false", ":"})
_ENV_VAR_RE = re.compile(r"^[A-Za-z_]\w*=")
_POWERSHELL_READ_CMDS = frozenset({
    "get-childitem",
    "gci",
    "dir",
    "ls",
    "get-content",
    "gc",
    "type",
    "select-string",
    "measure-object",
    "get-item",
    "gi",
    "test-path",
    "resolve-path",
    "where-object",
    "select-object",
    "sort-object",
})


def _split_command_parts(command: str) -> list[str]:
    parts = re.split(r"\|\||&&|[|;]", command or "")
    return [p.strip() for p in parts if p.strip()]


def _base_command(part: str) -> str:
    for tok in part.strip().split():
        if _ENV_VAR_RE.match(tok):
            continue
        return os.path.basename(tok)
    return ""


def _is_read_like_bash_command(command: str) -> bool:
    parts = _split_command_parts(command)
    if not parts:
        return False
    has_non_neutral = False
    for part in parts:
        base = _base_command(part)
        if not base:
            continue
        if base in _NEUTRAL_CMDS:
            continue
        has_non_neutral = True
        if base == "cd":
            return False
        if base not in _SEARCH_CMDS and base not in _READ_CMDS and base not in _LIST_CMDS:
            return False
    return has_non_neutral


def _is_read_like_powershell_command(command: str) -> bool:
    parts = _split_command_parts(command)
    if not parts:
        return False
    has_non_neutral = False
    for part in parts:
        base = _base_command(part).lower()
        if base in ("write-output", "echo", ""):
            continue
        has_non_neutral = True
        if base not in _POWERSHELL_READ_CMDS:
            return False
    return has_non_neutral


def _extract_file_path(tool_name: str, tool_input: dict | None) -> str | None:
    if not isinstance(tool_input, dict):
        return None
    for key in ("file_path", "path", "notebook_path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _norm_abs(path: str, cwd: str) -> str:
    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        expanded = os.path.join(cwd, expanded)
    return os.path.normcase(os.path.abspath(os.path.normpath(expanded)))


def _is_under(path: str, roots: list[str]) -> bool:
    for root in roots:
        try:
            if os.path.commonpath([path, root]) == root:
                return True
        except ValueError:
            continue
    return False


def _has_dangerous_segment(path: str) -> str | None:
    parts = re.split(r"[\\/]+", path)
    for part in parts:
        if part in _DANGEROUS_PATH_SEGMENTS:
            return part
    return None


def _filesystem_mode_result(tool_name: str, mode: str, tool_input: dict | None) -> dict | None:
    path = _extract_file_path(tool_name, tool_input)
    if path is None:
        return None

    cwd = str((tool_input or {}).get("_cwd") or os.getcwd())
    raw_additional = (tool_input or {}).get("_additional_working_directories") or []
    additional = [str(x) for x in raw_additional if isinstance(x, str)]
    abs_path = _norm_abs(path, cwd)
    allowed_roots = [_norm_abs(cwd, cwd)] + [_norm_abs(p, cwd) for p in additional]
    updated_input = dict(tool_input or {})
    updated_input.pop("_cwd", None)
    updated_input.pop("_additional_working_directories", None)

    if path.startswith("\\\\") or path.startswith("//"):
        return _ask(
            f"Claude requested permissions to use {tool_name} on a UNC/network path.",
            {"type": "workingDir", "reason": "Network paths require explicit approval."},
            updated_input=updated_input,
        )

    if _SUSPICIOUS_WINDOWS_PATH_RE.search(path):
        return _ask(
            f"Claude requested permissions to use {tool_name} on a suspicious Windows path.",
            {"type": "safetyCheck", "reason": "Suspicious Windows path pattern requires approval."},
            updated_input=updated_input,
        )

    dangerous = _has_dangerous_segment(abs_path)
    if dangerous and tool_name in _FILE_WRITE_TOOLS:
        return _ask(
            f"Claude requested permissions to write to a sensitive path containing {dangerous}.",
            {"type": "safetyCheck", "reason": f"Sensitive path segment: {dangerous}"},
            updated_input=updated_input,
        )

    if not _is_under(abs_path, allowed_roots):
        return _ask(
            f"Claude requested permissions to use {tool_name} outside the working directory.",
            {"type": "workingDir", "reason": "Path is outside cwd/additional directories."},
            updated_input=updated_input,
        )

    if tool_name in _FILE_READ_TOOLS:
        return _allow({"type": "mode", "mode": mode}, updated_input=updated_input)

    if tool_name in _FILE_WRITE_TOOLS:
        if mode == "bypassPermissions":
            return _allow({"type": "mode", "mode": mode}, updated_input=updated_input)
        if mode == "acceptEdits":
            return _allow({"type": "mode", "mode": mode}, updated_input=updated_input)
        return _ask(
            f"Current permission mode ({_permission_mode_title(mode)}) requires approval for this {tool_name} command",
            {"type": "mode", "mode": mode},
            updated_input=updated_input,
        )

    return None


def _permission_mode_title(mode: str) -> str:
    return mode


def _rule_value_to_string(rule: PermissionRuleValue) -> str:
    if rule.ruleContent is None:
        return rule.toolName
    return f"{rule.toolName}({rule.ruleContent})"


def _source_display_name(source: str) -> str:
    mapping = {
        "userSettings": "user settings",
        "projectSettings": "shared project settings",
        "localSettings": "project local settings",
        "flagSettings": "command line arguments",
        "policySettings": "enterprise managed settings",
        "cliArg": "CLI argument",
        "command": "command configuration",
        "session": "current session",
    }
    return mapping.get(source, source)


def _rule_decision(rule: PermissionRule) -> dict:
    return {
        "type": "rule",
        "rule": {
            "source": rule.source,
            "ruleBehavior": rule.ruleBehavior,
            "ruleValue": {
                "toolName": rule.ruleValue.toolName,
                **(
                    {"ruleContent": rule.ruleValue.ruleContent}
                    if rule.ruleValue.ruleContent is not None
                    else {}
                ),
            },
        },
    }


_RULE_CONTENT_RE = re.compile(r"^(.+)\((.*)\)$")


def _parse_rule_string(rule: str) -> PermissionRuleValue:
    raw = (rule or "").strip()
    m = _RULE_CONTENT_RE.match(raw)
    if m:
        tool = m.group(1).strip()
        content = m.group(2).strip()
        return PermissionRuleValue(toolName=tool, ruleContent=(content or None))
    return PermissionRuleValue(toolName=raw)


def _normalize_rules(
    rules: list[Any] | None,
    behavior: str,
) -> list[PermissionRule]:
    if not rules:
        return []
    out: list[PermissionRule] = []
    for item in rules:
        if isinstance(item, str):
            out.append(
                PermissionRule(
                    source="session",
                    ruleBehavior=behavior,
                    ruleValue=_parse_rule_string(item),
                )
            )
            continue
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or "session")
        if "rule" in item and isinstance(item["rule"], str):
            value = _parse_rule_string(item["rule"])
        elif "toolName" in item:
            value = PermissionRuleValue(
                toolName=str(item.get("toolName") or ""),
                ruleContent=(
                    str(item.get("ruleContent"))
                    if item.get("ruleContent") is not None
                    else None
                ),
            )
        else:
            continue
        if value.toolName:
            out.append(
                PermissionRule(
                    source=source,
                    ruleBehavior=behavior,
                    ruleValue=value,
                )
            )
    return out


def _tool_matches_rule(tool_name: str, tool_input: dict | None, rule: PermissionRule) -> bool:
    rule_tool = rule.ruleValue.toolName

    # Tool-name match (supports MCP prefix/server wildcard style rules)
    tool_name_match = False
    if tool_name == rule_tool:
        tool_name_match = True
    elif rule_tool.endswith("__*"):
        prefix = rule_tool[:-3]
        tool_name_match = tool_name == prefix or tool_name.startswith(prefix + "__")
    elif rule_tool.startswith("mcp__") and "__" not in rule_tool[len("mcp__") :]:
        tool_name_match = tool_name.startswith(rule_tool + "__")

    if not tool_name_match:
        return False

    # Rule without content applies to whole tool.
    if rule.ruleValue.ruleContent is None:
        return True

    # Content-aware rules are only applied for Bash currently.
    if tool_name not in ("Bash", "BashTool"):
        return False
    if not isinstance(tool_input, dict):
        return False
    command = str(tool_input.get("command") or "")
    if not command:
        return False

    pattern = rule.ruleValue.ruleContent
    # Source supports content-specific matching; we use shell-style wildcards.
    # Fallback to substring if no glob metacharacters are present.
    if any(ch in pattern for ch in ("*", "?", "[")):
        return fnmatch.fnmatch(command, pattern)
    return pattern in command


def _first_matching_rule(
    tool_name: str,
    tool_input: dict | None,
    rules: list[Any] | None,
    behavior: str,
) -> PermissionRule | None:
    for rule in _normalize_rules(rules, behavior):
        if _tool_matches_rule(tool_name, tool_input, rule):
            return rule
    return None


def _bash_subcommand_permission_result(
    subcommand: str,
    mode: str,
    always_allow_rules: list[Any] | None,
    always_deny_rules: list[Any] | None,
    always_ask_rules: list[Any] | None,
) -> dict:
    """
    Evaluate one Bash subcommand with the same rule/mode logic.
    This mirrors `subcommandResults` decision building in source permissions flow.
    """
    local_input = {"command": subcommand}

    deny_rule = _first_matching_rule("Bash", local_input, always_deny_rules, "deny")
    if deny_rule is not None:
        return _deny(
            f"Permission rule '{_rule_value_to_string(deny_rule.ruleValue)}' from {_source_display_name(deny_rule.source)} requires denying this Bash command",
            _rule_decision(deny_rule),
        )

    ask_rule = _first_matching_rule("Bash", local_input, always_ask_rules, "ask")
    if ask_rule is not None:
        return _ask(
            f"Permission rule '{_rule_value_to_string(ask_rule.ruleValue)}' from {_source_display_name(ask_rule.source)} requires approval for this Bash command",
            _rule_decision(ask_rule),
            updated_input=local_input,
        )

    allow_rule = _first_matching_rule("Bash", local_input, always_allow_rules, "allow")
    if allow_rule is not None:
        return _allow(_rule_decision(allow_rule), updated_input=local_input)

    if mode == "bypassPermissions":
        return _allow({"type": "mode", "mode": mode}, updated_input=local_input)

    if mode == "plan":
        if _is_read_like_bash_command(subcommand):
            return _allow({"type": "mode", "mode": mode}, updated_input=local_input)
        return _ask(
            f"Current permission mode ({_permission_mode_title(mode)}) requires approval for this Bash command",
            {"type": "mode", "mode": mode},
            updated_input=local_input,
        )

    # acceptEdits/default/dontAsk: Bash generally requires approval unless explicit rules allow.
    return _ask(
        "Allow Bash?",
        {"type": "mode", "mode": mode},
        updated_input=local_input,
    )


def _tool_mode_result(
    tool_name: str,
    mode: str,
    tool_input: dict | None = None,
    always_allow_rules: list[Any] | None = None,
    always_deny_rules: list[Any] | None = None,
    always_ask_rules: list[Any] | None = None,
) -> dict:
    """
    Local approximation of tool.checkPermissions + mode interaction.
    Returns allow/ask/passthrough (never deny here).
    """
    fs_result = _filesystem_mode_result(tool_name, mode, tool_input)
    if fs_result is not None:
        return fs_result

    if tool_name in _READ_ONLY_TOOLS:
        return _allow({"type": "mode", "mode": mode}, updated_input=tool_input)

    if tool_name in ("TodoWrite", "TodoWriteTool"):
        return _allow({"type": "mode", "mode": mode}, updated_input=tool_input)

    if tool_name in ("ToolSearch", "ToolSearchTool"):
        return _allow({"type": "mode", "mode": mode}, updated_input=tool_input)

    if tool_name in ("Bash", "BashTool"):
        command = ""
        if isinstance(tool_input, dict):
            command = str(tool_input.get("command") or "")
        if command and (("&&" in command) or ("||" in command) or ("|" in command) or (";" in command)):
            parts = _split_command_parts(command)
            reasons: dict[str, dict] = {}
            any_deny = False
            any_ask = False
            for part in parts:
                res = _bash_subcommand_permission_result(
                    part,
                    mode,
                    always_allow_rules,
                    always_deny_rules,
                    always_ask_rules,
                )
                reasons[part] = res
                if res["behavior"] == "deny":
                    any_deny = True
                elif res["behavior"] == "ask":
                    any_ask = True

            decision = {"type": "subcommandResults", "reasons": reasons}
            if any_deny:
                return _deny(
                    "This Bash command contains one or more denied operations.",
                    decision,
                )
            if any_ask:
                needs_approval = [cmd for cmd, res in reasons.items() if res["behavior"] == "ask"]
                listed = ", ".join(needs_approval) if needs_approval else "one or more parts"
                return _ask(
                    f"This Bash command contains multiple operations. The following part(s) require approval: {listed}",
                    decision,
                    updated_input=tool_input,
                )
            return _allow(decision, updated_input=tool_input)

        # Non-compound Bash command.
        return _bash_subcommand_permission_result(
            command or "bash",
            mode,
            always_allow_rules,
            always_deny_rules,
            always_ask_rules,
        )

    if tool_name in ("PowerShell", "PowerShellTool"):
        command = ""
        if isinstance(tool_input, dict):
            command = str(tool_input.get("command") or "")
        if mode == "bypassPermissions":
            return _allow({"type": "mode", "mode": mode}, updated_input=tool_input)
        if mode == "plan" and _is_read_like_powershell_command(command):
            return _allow({"type": "mode", "mode": mode}, updated_input=tool_input)
        return _ask(
            f"Current permission mode ({_permission_mode_title(mode)}) requires approval for this PowerShell command",
            {"type": "mode", "mode": mode},
            updated_input=tool_input,
        )

    if mode == "bypassPermissions":
        return _allow({"type": "mode", "mode": mode}, updated_input=tool_input)

    if mode == "plan":
        return _ask(
            f"Current permission mode ({_permission_mode_title(mode)}) requires approval for this {tool_name} command",
            {"type": "mode", "mode": mode},
            updated_input=tool_input,
        )

    if mode == "acceptEdits":
        if tool_name in _FILE_EDIT_TOOLS:
            return _allow({"type": "mode", "mode": mode}, updated_input=tool_input)
        return _ask(
            f"Current permission mode ({_permission_mode_title(mode)}) requires approval for this {tool_name} command",
            {"type": "mode", "mode": mode},
            updated_input=tool_input,
        )

    # default/dontAsk/unknown: edits ask, others passthrough (converted to ask later)
    if tool_name in _FILE_EDIT_TOOLS:
        return _ask(f"Allow {tool_name}?", {"type": "mode", "mode": mode}, updated_input=tool_input)

    return _passthrough(f"Claude requested permissions to use {tool_name}.")


def check_permission(
    tool_name: str,
    mode: str,
    tool_input: dict | None = None,
    always_allow_rules: list[Any] | None = None,
    always_deny_rules: list[Any] | None = None,
    always_ask_rules: list[Any] | None = None,
) -> dict:
    """
    Claude Code-like permission decision flow (adapted for this runtime).
    """
    deny_rule = _first_matching_rule(tool_name, tool_input, always_deny_rules, "deny")
    if deny_rule is not None:
        return _deny(
            f"Permission rule '{_rule_value_to_string(deny_rule.ruleValue)}' from {_source_display_name(deny_rule.source)} requires denying this {tool_name} command",
            _rule_decision(deny_rule),
        )

    ask_rule = _first_matching_rule(tool_name, tool_input, always_ask_rules, "ask")
    if ask_rule is not None:
        return _ask(
            f"Permission rule '{_rule_value_to_string(ask_rule.ruleValue)}' from {_source_display_name(ask_rule.source)} requires approval for this {tool_name} command",
            _rule_decision(ask_rule),
            updated_input=tool_input,
        )

    tool_permission_result = _tool_mode_result(
        tool_name,
        mode,
        tool_input,
        always_allow_rules,
        always_deny_rules,
        always_ask_rules,
    )

    if tool_permission_result["behavior"] == "deny":
        return tool_permission_result

    allow_rule = _first_matching_rule(tool_name, tool_input, always_allow_rules, "allow")
    if allow_rule is not None:
        return _allow(_rule_decision(allow_rule), updated_input=tool_input)

    # Convert passthrough -> ask (source step 3 behavior).
    result = tool_permission_result
    if result["behavior"] == "passthrough":
        result = _ask(
            f"Claude requested permissions to use {tool_name}, but you haven't granted it yet.",
            result.get("decisionReason"),
            updated_input=tool_input,
        )

    # dontAsk mode transforms ask -> deny (source-level mode transform).
    if mode == "dontAsk" and result["behavior"] == "ask":
        return _deny(
            result.get("message", f"{tool_name} is blocked in dontAsk mode."),
            {"type": "mode", "mode": "dontAsk"},
        )

    return result


def make_confirm_fn(
    mode: str,
    always_allow: list[Any] | None = None,
    always_deny: list[Any] | None = None,
    always_ask: list[Any] | None = None,
):
    """
    Return a synchronous confirm function used by query.py tool execution.
    """

    def confirm(tool_name: str, description: str, tool_input: dict | None = None) -> bool:
        result = check_permission(
            tool_name=tool_name,
            mode=mode,
            tool_input=tool_input,
            always_allow_rules=always_allow,
            always_deny_rules=always_deny,
            always_ask_rules=always_ask,
        )
        behavior = result["behavior"]

        if behavior == "allow":
            return True

        if behavior == "deny":
            message = result.get("message", f"{tool_name} denied.")
            print(f"\n\033[31m[Permission denied]\033[0m {message}")
            return False

        # behavior == "ask"
        print(f"\n\033[33m[Permission]\033[0m Tool: \033[1m{tool_name}\033[0m")
        message = result.get("message")
        if message:
            print(f"  {message}")
        decision = result.get("decisionReason")
        if isinstance(decision, dict) and decision.get("type") == "rule":
            rule = decision.get("rule", {})
            src = _source_display_name(str(rule.get("source") or "session"))
            value = rule.get("ruleValue", {})
            tool = value.get("toolName", "")
            content = value.get("ruleContent")
            display_rule = f"{tool}({content})" if content else str(tool)
            print(f"  Matched rule: {display_rule}  [source: {src}]")
        print(f"  {description}")
        try:
            answer = input("  Allow? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        return answer in ("y", "yes")

    return confirm


def explain_permission(
    tool_name: str,
    mode: str,
    tool_input: dict | None = None,
    always_allow_rules: list[Any] | None = None,
    always_deny_rules: list[Any] | None = None,
    always_ask_rules: list[Any] | None = None,
) -> dict:
    """
    Return a structured explanation of permission evaluation order and result.
    Mirrors the high-level source order:
      deny rule -> ask rule -> tool/mode check -> allow rule -> passthrough->ask -> dontAsk transform
    """
    deny_rule = _first_matching_rule(tool_name, tool_input, always_deny_rules, "deny")
    ask_rule = _first_matching_rule(tool_name, tool_input, always_ask_rules, "ask")
    allow_rule = _first_matching_rule(tool_name, tool_input, always_allow_rules, "allow")

    tool_mode_result = _tool_mode_result(
        tool_name,
        mode,
        tool_input,
        always_allow_rules,
        always_deny_rules,
        always_ask_rules,
    )

    final = check_permission(
        tool_name=tool_name,
        mode=mode,
        tool_input=tool_input,
        always_allow_rules=always_allow_rules,
        always_deny_rules=always_deny_rules,
        always_ask_rules=always_ask_rules,
    )

    def _rule_to_dict(rule: PermissionRule | None) -> dict | None:
        if rule is None:
            return None
        return {
            "source": rule.source,
            "sourceDisplay": _source_display_name(rule.source),
            "behavior": rule.ruleBehavior,
            "rule": _rule_value_to_string(rule.ruleValue),
            "toolName": rule.ruleValue.toolName,
            "ruleContent": rule.ruleValue.ruleContent,
        }

    return {
        "tool": tool_name,
        "mode": mode,
        "input": tool_input,
        "matched": {
            "deny": _rule_to_dict(deny_rule),
            "ask": _rule_to_dict(ask_rule),
            "allow": _rule_to_dict(allow_rule),
        },
        "toolModeResult": {
            "behavior": tool_mode_result.get("behavior"),
            "decisionReason": tool_mode_result.get("decisionReason"),
            "message": tool_mode_result.get("message"),
        },
        "final": final,
    }
