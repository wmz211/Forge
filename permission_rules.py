from __future__ import annotations

import json
import os
from pathlib import Path


# Mirrors settings/constants.ts ordering + permissions.ts rule source ordering.
SETTING_SOURCES = (
    "userSettings",
    "projectSettings",
    "localSettings",
    "flagSettings",
    "policySettings",
)

RULE_SOURCE_ORDER = (
    "userSettings",
    "projectSettings",
    "localSettings",
    "flagSettings",
    "policySettings",
    "cliArg",
    "command",
    "session",
)

PERSISTABLE_SOURCES = ("userSettings", "projectSettings", "localSettings")
RUNTIME_MUTABLE_SOURCES = ("session", "cliArg", "command")
READONLY_SOURCES = ("flagSettings", "policySettings")
_LAST_POLICY_ORIGIN = "none"


def empty_rules_by_source() -> dict[str, list[str]]:
    return {src: [] for src in RULE_SOURCE_ORDER}


def source_label(source: str) -> str:
    labels = {
        "userSettings": "user",
        "projectSettings": "project",
        "localSettings": "local",
        "flagSettings": "flag",
        "policySettings": "policy",
        "cliArg": "cliArg",
        "command": "command",
        "session": "session",
    }
    return labels.get(source, source)


def source_is_readonly(source: str) -> bool:
    return source in READONLY_SOURCES


def get_policy_origin() -> str:
    return _LAST_POLICY_ORIGIN


def get_enabled_setting_sources() -> tuple[str, ...]:
    """
    Minimal mirror of getEnabledSettingSources():
    - user/project/local are controlled by FORGE_SETTING_SOURCES
    - policy/flag are always included
    """
    raw = (os.environ.get("FORGE_SETTING_SOURCES") or "").strip()
    mapping = {
        "user": "userSettings",
        "project": "projectSettings",
        "local": "localSettings",
    }
    enabled: list[str] = []
    if raw:
        for item in raw.split(","):
            key = item.strip().lower()
            src = mapping.get(key)
            if src and src not in enabled:
                enabled.append(src)
    else:
        enabled.extend(("userSettings", "projectSettings", "localSettings"))

    # Always include policy/flag, mirroring source behavior.
    if "flagSettings" not in enabled:
        enabled.append("flagSettings")
    if "policySettings" not in enabled:
        enabled.append("policySettings")
    return tuple(enabled)


def _settings_path_for_source(source: str, cwd: str) -> Path | None:
    if source == "userSettings":
        return Path.home() / ".claude" / "settings.json"
    if source == "projectSettings":
        return Path(cwd) / ".claude" / "settings.json"
    if source == "localSettings":
        return Path(cwd) / ".claude" / "settings.local.json"
    if source == "flagSettings":
        p = os.environ.get("FORGE_FLAG_SETTINGS_PATH")
        if p:
            return Path(p)
        return None
    if source == "policySettings":
        # Base file-based policy source; full chain is handled separately.
        return Path.home() / ".claude" / "managed-settings.json"
    return None


def _path_from_env(name: str) -> Path | None:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return None
    return Path(raw)


def _read_json(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _read_permissions_behavior(path: Path, behavior: str) -> list[str]:
    data = _read_json(path)
    perms = data.get("permissions")
    if not isinstance(perms, dict):
        return []
    rules = perms.get(behavior)
    if not isinstance(rules, list):
        return []
    return [str(x) for x in rules if isinstance(x, str)]


def _load_flag_inline_permissions(behavior: str) -> list[str]:
    raw = (os.environ.get("FORGE_FLAG_SETTINGS_INLINE") or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    perms = data.get("permissions")
    if not isinstance(perms, dict):
        return []
    rules = perms.get(behavior)
    if not isinstance(rules, list):
        return []
    return [str(x) for x in rules if isinstance(x, str)]


def _load_policy_permissions_first_source_wins(behavior: str) -> tuple[list[str], str]:
    """
    Minimal mirror of policy first-source-wins:
      remote > admin > file > hkcu
    """
    candidates: list[tuple[str, Path | None]] = [
        ("remote", _path_from_env("FORGE_REMOTE_MANAGED_SETTINGS_PATH")),
        ("admin", _path_from_env("FORGE_ADMIN_MANAGED_SETTINGS_PATH")),
        ("file", Path.home() / ".claude" / "managed-settings.json"),
        ("hkcu", _path_from_env("FORGE_HKCU_MANAGED_SETTINGS_PATH")),
    ]
    for origin, path in candidates:
        if path is None or not path.exists():
            continue
        rules = _read_permissions_behavior(path, behavior)
        if rules:
            return rules, origin
        # first-source-wins: if permissions object exists, this source wins
        data = _read_json(path)
        if isinstance(data.get("permissions"), dict):
            return [], origin
    return [], "none"


def load_rules_from_source(cwd: str, source: str, behavior: str) -> list[str]:
    global _LAST_POLICY_ORIGIN
    if source == "policySettings":
        rules, origin = _load_policy_permissions_first_source_wins(behavior)
        _LAST_POLICY_ORIGIN = origin
        return rules
    if source == "flagSettings":
        inline = _load_flag_inline_permissions(behavior)
        if inline:
            return inline
    path = _settings_path_for_source(source, cwd)
    if path is None:
        return []
    data = _read_json(path)
    perms = data.get("permissions")
    if not isinstance(perms, dict):
        return []
    rules = perms.get(behavior)
    if not isinstance(rules, list):
        return []
    return [str(x) for x in rules if isinstance(x, str)]


def load_rules_by_source(cwd: str, behavior: str) -> dict[str, list[str]]:
    out = empty_rules_by_source()
    enabled = set(get_enabled_setting_sources())
    for source in SETTING_SOURCES:
        if source in enabled:
            out[source] = load_rules_from_source(cwd, source, behavior)
    return out


def load_additional_directories(cwd: str) -> list[str]:
    """
    Minimal mirror of permissions.additionalDirectories from settings.
    Directories are collected from enabled user/project/local/policy/flag sources
    in source order and de-duplicated.
    """
    out: list[str] = []
    seen: set[str] = set()
    enabled = set(get_enabled_setting_sources())
    for source in SETTING_SOURCES:
        if source not in enabled:
            continue
        path = _settings_path_for_source(source, cwd)
        if path is None:
            continue
        data = _read_json(path)
        perms = data.get("permissions")
        if not isinstance(perms, dict):
            continue
        dirs = perms.get("additionalDirectories")
        if not isinstance(dirs, list):
            continue
        for item in dirs:
            if not isinstance(item, str):
                continue
            resolved = os.path.abspath(os.path.join(cwd, item)) if not os.path.isabs(item) else os.path.abspath(item)
            if resolved not in seen:
                seen.add(resolved)
                out.append(resolved)
    return out


def flatten_rules_by_source(rules_by_source: dict[str, list[str]]) -> list[str]:
    out: list[str] = []
    for source in RULE_SOURCE_ORDER:
        out.extend(rules_by_source.get(source, []))
    return out


def add_rule_to_source(cwd: str, source: str, behavior: str, rule: str) -> bool:
    if source not in PERSISTABLE_SOURCES:
        return False
    path = _settings_path_for_source(source, cwd)
    if path is None:
        return False
    data = _read_json(path)
    perms = data.get("permissions")
    if not isinstance(perms, dict):
        perms = {}
    rules = perms.get(behavior)
    if not isinstance(rules, list):
        rules = []
    if rule not in rules:
        rules.append(rule)
    perms[behavior] = rules
    data["permissions"] = perms
    _write_json(path, data)
    return True


def remove_rule_from_source(cwd: str, source: str, behavior: str, rule: str) -> bool:
    if source not in PERSISTABLE_SOURCES:
        return False
    path = _settings_path_for_source(source, cwd)
    if path is None:
        return False
    data = _read_json(path)
    perms = data.get("permissions")
    if not isinstance(perms, dict):
        return True
    rules = perms.get(behavior)
    if not isinstance(rules, list):
        return True
    perms[behavior] = [x for x in rules if x != rule]
    data["permissions"] = perms
    _write_json(path, data)
    return True
