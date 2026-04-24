"""
/model [name] — Show or interactively switch the active model.
Mirrors Claude Code's /model command in commands/model/.

With no argument: shows an arrow-key radio-list selection.
With an argument: switches directly to the named model.
"""
from __future__ import annotations
import commands as _reg

KNOWN_MODELS = [
    "qwen3-coder-plus",
    "qwen3-coder-flash",
    "qwen3.6-max",
    "qwen3.6-plus",
    "qwen3.6-flash",
]


async def call(args: str, engine) -> str:
    target = args.strip()

    if not target:
        from prompt_toolkit.shortcuts import radiolist_dialog
        from prompt_toolkit.styles import Style as PtStyle

        current = engine._api.model
        style = PtStyle.from_dict({
            "dialog":              "bg:#1c1c1c",
            "dialog.body":         "bg:#1c1c1c #ffffff",
            "dialog frame.label":  "bg:#005f87 #ffffff bold",
            "dialog shadow":       "bg:#000000",
            "radio-list":          "bg:#1c1c1c #ffffff",
            "radio-selected":      "fg:cyan bold",
            "button":              "bg:#005f87 #ffffff",
            "button.focused":      "bg:#0087af #ffffff bold",
        })

        choice = await radiolist_dialog(
            title="Select Model",
            text="↑↓ to move  ·  Space/Enter to confirm  ·  Ctrl-C to cancel",
            values=[(m, m) for m in KNOWN_MODELS],
            default=current if current in KNOWN_MODELS else KNOWN_MODELS[0],
            style=style,
        ).run_async()

        if not choice:
            return ""
        target = choice

    old = engine._api.model
    if old == target:
        return f"  Already using \033[1;36m{target}\033[0m"
    engine._api.model = target
    return f"  \033[32mSwitched:\033[0m \033[90m{old}\033[0m → \033[1;36m{target}\033[0m"


_reg.register({
    "name": "model",
    "description": "Show or switch the active AI model",
    "argument_hint": "[model-name]",
    "call": call,
})
