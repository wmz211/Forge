from __future__ import annotations

import asyncio
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

from permissions import check_permission
from tool import ToolContext
from tool import Tool
from tools import build_builtin_tools, core_tools_for_api
from tools.ask_user_question_tool import AskUserQuestionTool
from tools.bash_tool import BashTool
from tools.file_read_tool import FileReadTool, MAX_SIZE_BYTES
from tools.notebook_edit_tool import NotebookEditTool
from tools.task_tools import TaskOutputTool
from tools.todo_write_tool import TodoWriteTool
from tools.agent_tool import _resolve_tools
from utils.file_state_cache import FileState
from utils.hooks import execute_pre_tool_use_hooks, execute_user_prompt_submit_hooks
from query import _run_tool


def run(coro):
    return asyncio.run(coro)


class PermissionFidelityTests(unittest.TestCase):
    def test_workspace_read_and_accept_edits_write_are_allowed(self):
        cwd = os.getcwd()
        self.assertEqual(
            check_permission("Read", "default", {"file_path": "tool.py", "_cwd": cwd})["behavior"],
            "allow",
        )
        self.assertEqual(
            check_permission("Write", "acceptEdits", {"file_path": "x.txt", "_cwd": cwd})["behavior"],
            "allow",
        )

    def test_sensitive_write_path_requires_approval(self):
        result = check_permission(
            "Write",
            "acceptEdits",
            {"file_path": ".git/config", "_cwd": os.getcwd()},
        )
        self.assertEqual(result["behavior"], "ask")
        self.assertEqual(result["decisionReason"]["type"], "safetyCheck")

    def test_additional_working_directory_allows_paths_outside_cwd(self):
        with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as extra:
            target = str(Path(extra) / "allowed.txt")
            result = check_permission(
                "Read",
                "default",
                {
                    "file_path": target,
                    "_cwd": cwd,
                    "_additional_working_directories": [extra],
                },
            )
            self.assertEqual(result["behavior"], "allow")


class FileReadFidelityTests(unittest.TestCase):
    def test_large_file_window_read_is_allowed_when_offset_limit_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "large.txt"
            line = "x" * 120 + "\n"
            lines = (MAX_SIZE_BYTES // len(line)) + 100
            path.write_text(line * lines, encoding="utf-8")
            ctx = ToolContext(cwd=tmp, permission_mode="default", confirm_fn=lambda *args: True)

            output = run(FileReadTool().call({"file_path": str(path), "offset": 3, "limit": 2}, ctx))

            self.assertIn("3\t", output)
            self.assertIn("4\t", output)
            self.assertIn("[Showing lines 3-4", output)


class TodoWriteFidelityTests(unittest.TestCase):
    def test_todo_write_stores_and_clears_all_completed_lists(self):
        tool = TodoWriteTool()
        ctx = ToolContext(cwd=".", permission_mode="default", confirm_fn=lambda *args: True, session_id="s")
        todos = [
            {"content": "Run tests", "activeForm": "Running tests", "status": "in_progress"},
        ]
        valid, message = run(tool.validate_input({"todos": todos}, ctx))
        self.assertTrue(valid, message)
        run(tool.call({"todos": todos}, ctx))
        self.assertEqual(ctx.todos["s"][0]["content"], "Run tests")

        done = [
            {"content": "Run tests", "activeForm": "Running tests", "status": "completed"},
        ]
        run(tool.call({"todos": done}, ctx))
        self.assertEqual(ctx.todos["s"], [])

    def test_todo_write_rejects_multiple_in_progress_items(self):
        tool = TodoWriteTool()
        ctx = ToolContext(cwd=".", permission_mode="default", confirm_fn=lambda *args: True)
        valid, message = run(tool.validate_input({
            "todos": [
                {"content": "A", "activeForm": "Doing A", "status": "in_progress"},
                {"content": "B", "activeForm": "Doing B", "status": "in_progress"},
            ]
        }, ctx))
        self.assertFalse(valid)
        self.assertIn("at most one", message)


class ToolSearchFidelityTests(unittest.TestCase):
    def test_core_tools_hide_deferred_tools_when_tool_search_available(self):
        tools = build_builtin_tools()
        visible = [tool.name for tool in core_tools_for_api(tools)]
        self.assertIn("ToolSearch", visible)
        self.assertNotIn("WebSearch", visible)
        self.assertNotIn("TodoWrite", visible)

    def test_tool_search_select_and_keyword(self):
        tools = build_builtin_tools()
        search = next(tool for tool in tools if tool.name == "ToolSearch")
        ctx = ToolContext(cwd=".", permission_mode="default", confirm_fn=lambda *args: True)

        selected = run(search.call({"query": "select:TodoWrite"}, ctx))
        self.assertIn("TodoWrite", selected)

        keyword = run(search.call({"query": "web current", "max_results": 5}, ctx))
        self.assertIn("WebSearch", keyword)
        self.assertIn("WebFetch", keyword)
        self.assertNotIn("- TodoWrite", keyword)


class AskUserQuestionFidelityTests(unittest.TestCase):
    def test_single_select_reads_numbered_choice_without_shadowing_builtin_input(self):
        tool = AskUserQuestionTool()
        ctx = ToolContext(cwd=".", permission_mode="default", confirm_fn=lambda *args: True)
        old_stdin = sys.stdin
        try:
            sys.stdin = io.StringIO("2\n")
            result = run(tool.call({
                "question": "Pick one",
                "options": [
                    {"label": "A", "value": "alpha"},
                    {"label": "B", "value": "beta"},
                ],
            }, ctx))
        finally:
            sys.stdin = old_stdin

        self.assertEqual(result, "beta")

    def test_preview_is_rejected_for_multi_select(self):
        tool = AskUserQuestionTool()
        ctx = ToolContext(cwd=".", permission_mode="default", confirm_fn=lambda *args: True)

        valid, message = run(tool.validate_input({
            "question": "Compare",
            "multiSelect": True,
            "options": [
                {"label": "A", "value": "a", "preview": "layout A"},
            ],
        }, ctx))

        self.assertFalse(valid)
        self.assertIn("single-select", message)


class NotebookEditFidelityTests(unittest.TestCase):
    def _notebook_content(self) -> str:
        return json.dumps({
            "nbformat": 4,
            "nbformat_minor": 5,
            "metadata": {"language_info": {"name": "python"}},
            "cells": [
                {
                    "cell_type": "code",
                    "id": "abc123",
                    "source": "print('old')",
                    "metadata": {},
                    "execution_count": 1,
                    "outputs": [{"output_type": "stream", "text": "old\n"}],
                }
            ],
        })

    def test_delete_mode_does_not_require_new_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "notebook.ipynb"
            content = self._notebook_content()
            path.write_text(content, encoding="utf-8")
            ctx = ToolContext(cwd=tmp, permission_mode="default", confirm_fn=lambda *args: True)
            ctx.file_state_cache.set(
                str(path),
                FileState(content=content, mtime_at_read=os.path.getmtime(path)),
            )
            tool = NotebookEditTool()

            valid, message = run(tool.validate_input({
                "notebook_path": str(path),
                "cell_id": "abc123",
                "edit_mode": "delete",
            }, ctx))
            self.assertTrue(valid, message)

            result = run(tool.call({
                "notebook_path": str(path),
                "cell_id": "abc123",
                "edit_mode": "delete",
            }, ctx))

            self.assertIn("Deleted cell abc123", result)
            updated = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(updated["cells"], [])

    def test_replace_mode_requires_new_source_and_clears_code_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "notebook.ipynb"
            content = self._notebook_content()
            path.write_text(content, encoding="utf-8")
            ctx = ToolContext(cwd=tmp, permission_mode="default", confirm_fn=lambda *args: True)
            ctx.file_state_cache.set(
                str(path),
                FileState(content=content, mtime_at_read=os.path.getmtime(path)),
            )
            tool = NotebookEditTool()

            valid, message = run(tool.validate_input({
                "notebook_path": str(path),
                "cell_id": "abc123",
                "edit_mode": "replace",
            }, ctx))
            self.assertFalse(valid)
            self.assertIn("new_source is required", message)

            valid, message = run(tool.validate_input({
                "notebook_path": str(path),
                "cell_id": "abc123",
                "new_source": "print('new')",
            }, ctx))
            self.assertTrue(valid, message)
            run(tool.call({
                "notebook_path": str(path),
                "cell_id": "abc123",
                "new_source": "print('new')",
            }, ctx))

            cell = json.loads(path.read_text(encoding="utf-8"))["cells"][0]
            self.assertEqual(cell["source"], "print('new')")
            self.assertIsNone(cell["execution_count"])
            self.assertEqual(cell["outputs"], [])


class HookFidelityTests(unittest.TestCase):
    def _hook_command(self, kind: str) -> str:
        if os.name == "nt":
            if kind == "block":
                return "cmd /c echo blocked 1>&2 & exit /b 2"
            return "cmd /c echo hook-context"
        if kind == "block":
            return "sh -c 'echo blocked >&2; exit 2'"
        return "sh -c 'echo hook-context'"

    def test_pre_tool_use_hook_exit_code_two_blocks_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings_dir = Path(tmp) / ".claude"
            settings_dir.mkdir()
            (settings_dir / "settings.json").write_text(json.dumps({
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [{"type": "command", "command": self._hook_command("block")}],
                        }
                    ]
                }
            }), encoding="utf-8")

            result = run(execute_pre_tool_use_hooks(
                "Bash", {"command": "echo hi"}, tmp, "sid", "transcript.jsonl"
            ))

            self.assertTrue(result["block"])
            self.assertIn("blocked", result["block_reason"])

    def test_user_prompt_submit_hook_stdout_becomes_additional_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings_dir = Path(tmp) / ".claude"
            settings_dir.mkdir()
            (settings_dir / "settings.json").write_text(json.dumps({
                "hooks": {
                    "UserPromptSubmit": [
                        {
                            "matcher": "*",
                            "hooks": [{"type": "command", "command": self._hook_command("context")}],
                        }
                    ]
                }
            }), encoding="utf-8")

            result = run(execute_user_prompt_submit_hooks(
                "hello", tmp, "sid", "transcript.jsonl"
            ))

            self.assertFalse(result["block"])
            self.assertIn("hook-context", result["additional_context"])

    def test_permission_request_hook_can_approve_ask_decision(self):
        class FakeWriteTool(Tool):
            name = "Write"
            description = "fake write"

            def get_schema(self):
                return {"type": "object", "properties": {}}

            async def call(self, input, ctx):
                return json.dumps(input)

        with tempfile.TemporaryDirectory() as tmp:
            settings_dir = Path(tmp) / ".claude"
            settings_dir.mkdir()
            hook_output = {
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": {
                        "behavior": "allow",
                        "updatedInput": {
                            "file_path": "hook-approved.txt",
                            "content": "updated by hook",
                        },
                    },
                }
            }
            hook_file = Path(tmp) / "hook-output.json"
            hook_file.write_text(json.dumps(hook_output), encoding="utf-8")
            if os.name == "nt":
                command = f'cmd /c type "{hook_file}"'
            else:
                command = f"cat {hook_file}"
            (settings_dir / "settings.json").write_text(json.dumps({
                "hooks": {
                    "PermissionRequest": [
                        {
                            "matcher": "Write",
                            "hooks": [{"type": "command", "command": command}],
                        }
                    ]
                }
            }), encoding="utf-8")

            ctx = ToolContext(
                cwd=tmp,
                permission_mode="default",
                confirm_fn=lambda *args: False,
                session_id="sid",
                session_transcript_path=Path(tmp) / "transcript.jsonl",
            )
            result_id, result = run(_run_tool({
                "id": "tc1",
                "name": "Write",
                "arguments": {"file_path": "original.txt", "content": "original"},
            }, {"Write": FakeWriteTool()}, ctx))

            self.assertEqual(result_id, "tc1")
            data = json.loads(result)
            self.assertEqual(data["file_path"], "hook-approved.txt")
            self.assertEqual(data["content"], "updated by hook")

    def test_pre_tool_use_json_can_allow_and_rewrite_input(self):
        class FakeWriteTool(Tool):
            name = "Write"
            description = "fake write"

            def get_schema(self):
                return {"type": "object", "properties": {}}

            async def call(self, input, ctx):
                return json.dumps(input)

        with tempfile.TemporaryDirectory() as tmp:
            settings_dir = Path(tmp) / ".claude"
            settings_dir.mkdir()
            hook_output = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "permissionDecisionReason": "trusted rewrite",
                    "updatedInput": {
                        "file_path": "prehook-approved.txt",
                        "content": "rewritten by prehook",
                    },
                }
            }
            hook_file = Path(tmp) / "prehook-output.json"
            hook_file.write_text(json.dumps(hook_output), encoding="utf-8")
            command = f'cmd /c type "{hook_file}"' if os.name == "nt" else f"cat {hook_file}"
            (settings_dir / "settings.json").write_text(json.dumps({
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Write",
                            "hooks": [{"type": "command", "command": command}],
                        }
                    ]
                }
            }), encoding="utf-8")

            ctx = ToolContext(
                cwd=tmp,
                permission_mode="default",
                confirm_fn=lambda *args: False,
                session_id="sid",
                session_transcript_path=Path(tmp) / "transcript.jsonl",
            )
            _, result = run(_run_tool({
                "id": "tc1",
                "name": "Write",
                "arguments": {"file_path": "original.txt", "content": "original"},
            }, {"Write": FakeWriteTool()}, ctx))

            data = json.loads(result)
            self.assertEqual(data["file_path"], "prehook-approved.txt")
            self.assertEqual(data["content"], "rewritten by prehook")


class BackgroundTaskFidelityTests(unittest.TestCase):
    def test_background_bash_output_is_retrievable_by_task_output(self):
        async def scenario(tmp: str) -> dict:
            ctx = ToolContext(cwd=tmp, permission_mode="default", confirm_fn=lambda *args: True)
            bash_result = await BashTool().call({
                "command": "echo forge-bg",
                "run_in_background": True,
            }, ctx)
            match = re.search(r"task_id=([0-9a-f-]+)", bash_result)
            self.assertIsNotNone(match, bash_result)

            output = await TaskOutputTool().call({
                "task_id": match.group(1),
                "block": True,
                "timeout": 5000,
            }, ctx)
            return json.loads(output)

        with tempfile.TemporaryDirectory() as tmp:
            data = run(scenario(tmp))

            self.assertEqual(data["retrieval_status"], "success")
            self.assertIn("forge-bg", data["task"]["output"])


class AgentToolFilteringFidelityTests(unittest.TestCase):
    def test_async_agent_tool_pool_blocks_main_thread_only_tools(self):
        pool = build_builtin_tools()
        resolved = _resolve_tools(None, pool)
        names = {tool.name for tool in resolved}

        self.assertIn("Read", names)
        self.assertIn("ToolSearch", names)
        self.assertNotIn("Agent", names)
        self.assertNotIn("AskUserQuestion", names)
        self.assertNotIn("TaskOutput", names)
        self.assertNotIn("TaskStop", names)
        self.assertNotIn("TaskCreate", names)

        search = next(tool for tool in resolved if tool.name == "ToolSearch")
        ctx = ToolContext(cwd=".", permission_mode="default", confirm_fn=lambda *args: True)
        result = run(search.call({"query": "select:AskUserQuestion"}, ctx))
        self.assertIn("Missing requested tools: AskUserQuestion", result)


if __name__ == "__main__":
    unittest.main()
