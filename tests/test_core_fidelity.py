from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from permissions import check_permission
from tool import ToolContext
from tools import build_builtin_tools, core_tools_for_api
from tools.file_read_tool import FileReadTool, MAX_SIZE_BYTES
from tools.todo_write_tool import TodoWriteTool


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


if __name__ == "__main__":
    unittest.main()
