from __future__ import annotations
"""
NotebookEdit tool — edit cells in Jupyter notebook (.ipynb) files.
Mirrors src/tools/NotebookEditTool/NotebookEditTool.ts.

Operations:
  replace  — overwrite a cell's source (default)
  insert   — insert a new cell after cell_id (or at start if no cell_id)
  delete   — remove a cell

Must-read-before-edit is enforced (mirrors readFileState guard in source).
"""

import json
import os
import random
import string
import tempfile
from typing import Any

from tool import Tool, ToolContext

NOTEBOOK_EDIT_TOOL_NAME = "NotebookEdit"

_DESCRIPTION = "Edit, insert, or delete cells in Jupyter notebooks (.ipynb files)"

_PROMPT = """\
Edit a Jupyter notebook (.ipynb) cell.

Usage notes:
- You must use your Read tool to read the notebook before editing it.
- cell_id: ID of the cell to edit/delete, or the cell after which to insert.
  Use the cell's `id` field from the notebook JSON, or a `cell-N` index (0-based).
- edit_mode defaults to 'replace'. Use 'insert' to add a new cell, 'delete' to remove.
- new_source is the new cell content (required for replace/insert, ignored for delete).
- cell_type ('code' or 'markdown') defaults to the existing cell type. Required for insert.
- Editing a code cell clears its outputs and execution_count (mirrors Jupyter behaviour).
- Only works on .ipynb files. For other files, use the Edit tool.
"""

_IPYNB_INDENT = 1  # mirrors IPYNB_INDENT = 1 in source


def _parse_cell_id(cell_id: str) -> int | None:
    """
    Mirrors parseCellId() in utils/notebook.ts.
    Accepts "cell-N" (0-based index) → returns N, else None.
    """
    if cell_id.startswith("cell-"):
        try:
            return int(cell_id[5:])
        except ValueError:
            return None
    # Also accept bare integers as a convenience
    try:
        return int(cell_id)
    except ValueError:
        return None


def _random_cell_id(length: int = 13) -> str:
    """Generate a random cell ID like JavaScript's Math.random().toString(36).substring(2,15)."""
    chars = string.digits + string.ascii_lowercase
    return "".join(random.choices(chars, k=length))


class NotebookEditTool(Tool):
    name = NOTEBOOK_EDIT_TOOL_NAME
    description = _DESCRIPTION
    is_concurrency_safe = False
    should_defer = True

    def get_schema(self) -> dict:
        return {
            "name": self.name,
            "description": _PROMPT,
            "parameters": {
                "type": "object",
                "properties": {
                    "notebook_path": {
                        "type": "string",
                        "description": "Absolute path to the Jupyter notebook (.ipynb) file",
                    },
                    "cell_id": {
                        "type": "string",
                        "description": (
                            "ID of the cell to edit/delete, or the cell after which to insert. "
                            "Use the cell's `id` field or `cell-N` (0-based index). "
                            "Not required when inserting at the beginning."
                        ),
                    },
                    "new_source": {
                        "type": "string",
                        "description": "New source code or markdown for the cell (required for replace/insert)",
                    },
                    "cell_type": {
                        "type": "string",
                        "enum": ["code", "markdown"],
                        "description": "Cell type. Defaults to existing cell type. Required for insert.",
                    },
                    "edit_mode": {
                        "type": "string",
                        "enum": ["replace", "insert", "delete"],
                        "description": "Edit operation: replace (default), insert, or delete.",
                    },
                },
                "required": ["notebook_path"],
            },
        }

    def to_openai_tool(self) -> dict:
        schema = self.get_schema()
        return {
            "type": "function",
            "function": {
                "name": schema["name"],
                "description": schema["description"],
                "parameters": schema["parameters"],
            },
        }

    async def validate_input(
        self, input: dict[str, Any], ctx: ToolContext
    ) -> tuple[bool, str | None]:
        notebook_path = input.get("notebook_path", "")
        edit_mode = input.get("edit_mode", "replace")
        cell_id = input.get("cell_id")
        cell_type = input.get("cell_type")

        if not os.path.isabs(notebook_path):
            notebook_path = os.path.join(ctx.cwd, notebook_path)
        notebook_path = os.path.normpath(notebook_path)

        if not notebook_path.lower().endswith(".ipynb"):
            return False, (
                "File must be a Jupyter notebook (.ipynb file). "
                "For editing other file types, use the Edit tool."
            )

        if edit_mode not in ("replace", "insert", "delete"):
            return False, "Edit mode must be replace, insert, or delete."

        if edit_mode == "insert" and not cell_type:
            return False, "Cell type is required when using edit_mode=insert."

        if edit_mode in ("replace", "insert") and "new_source" not in input:
            return False, "new_source is required when using edit_mode=replace or insert."

        # Must-read-before-edit (mirrors readFileState guard in source)
        cached = ctx.file_state_cache.get(notebook_path)
        if cached is None:
            return False, (
                "File has not been read yet. Read it first before writing to it."
            )

        # Concurrent modification check
        if cached.mtime_at_read is not None:
            try:
                current_mtime = os.path.getmtime(notebook_path)
                if current_mtime > cached.mtime_at_read + 0.001:
                    ctx.file_state_cache.delete(notebook_path)
                    return False, (
                        "File has been modified since read, either by the user or by a linter. "
                        "Read it again before attempting to write it."
                    )
            except OSError:
                pass

        if not os.path.exists(notebook_path):
            return False, "Notebook file does not exist."

        try:
            with open(notebook_path, "r", encoding="utf-8") as f:
                notebook = json.load(f)
        except json.JSONDecodeError:
            return False, "Notebook is not valid JSON."
        except Exception as e:
            return False, f"Failed to read notebook: {e}"

        if not cell_id:
            if edit_mode != "insert":
                return False, "Cell ID must be specified when not inserting a new cell."
        else:
            # Find by actual id first
            cell_index = next(
                (i for i, c in enumerate(notebook.get("cells", [])) if c.get("id") == cell_id),
                -1,
            )
            if cell_index == -1:
                parsed = _parse_cell_id(cell_id)
                if parsed is not None:
                    if parsed >= len(notebook.get("cells", [])):
                        return False, f"Cell with index {parsed} does not exist in notebook."
                else:
                    return False, f'Cell with ID "{cell_id}" not found in notebook.'

        return True, None

    async def call(self, input: dict[str, Any], ctx: ToolContext) -> str:
        notebook_path = input.get("notebook_path", "")
        new_source: str = input.get("new_source", "")
        cell_id: str | None = input.get("cell_id")
        cell_type: str | None = input.get("cell_type")
        edit_mode: str = input.get("edit_mode", "replace")

        if not os.path.isabs(notebook_path):
            notebook_path = os.path.join(ctx.cwd, notebook_path)
        notebook_path = os.path.normpath(notebook_path)

        try:
            with open(notebook_path, "r", encoding="utf-8") as f:
                notebook = json.load(f)
        except Exception as e:
            return f"<error>Failed to read notebook: {e}</error>"

        cells: list[dict] = notebook.get("cells", [])

        # Determine cell index
        if not cell_id:
            cell_index = 0  # insert at beginning
        else:
            # Try exact ID match first
            cell_index = next(
                (i for i, c in enumerate(cells) if c.get("id") == cell_id),
                -1,
            )
            if cell_index == -1:
                parsed = _parse_cell_id(cell_id)
                if parsed is not None:
                    cell_index = parsed
                # else validateInput already caught this

            if edit_mode == "insert":
                cell_index += 1  # insert AFTER the specified cell

        # Convert replace → insert if appending past end (mirrors source)
        if edit_mode == "replace" and cell_index == len(cells):
            edit_mode = "insert"
            if not cell_type:
                cell_type = "code"

        # Decide whether to generate a new cell ID (nbformat >= 4.5)
        nbformat = notebook.get("nbformat", 4)
        nbformat_minor = notebook.get("nbformat_minor", 0)
        use_cell_ids = nbformat > 4 or (nbformat == 4 and nbformat_minor >= 5)

        language = (
            notebook.get("metadata", {}).get("language_info", {}).get("name", "python")
        )

        if edit_mode == "delete":
            if cell_index < 0 or cell_index >= len(cells):
                return f"<error>Cell index {cell_index} out of range.</error>"
            removed_id = cells[cell_index].get("id", f"cell-{cell_index}")
            cells.pop(cell_index)
            result_msg = f"Deleted cell {removed_id}"
            new_cell_id = removed_id

        elif edit_mode == "insert":
            new_cell_id = _random_cell_id() if use_cell_ids else None
            if cell_type == "markdown":
                new_cell: dict = {
                    "cell_type": "markdown",
                    "source": new_source,
                    "metadata": {},
                }
            else:
                new_cell = {
                    "cell_type": "code",
                    "source": new_source,
                    "metadata": {},
                    "execution_count": None,
                    "outputs": [],
                }
            if new_cell_id:
                new_cell["id"] = new_cell_id
            cells.insert(cell_index, new_cell)
            result_msg = f"Inserted cell {new_cell_id or cell_index} with {new_source[:80]}"

        else:  # replace
            if cell_index < 0 or cell_index >= len(cells):
                return f"<error>Cell index {cell_index} out of range.</error>"
            target = cells[cell_index]
            target["source"] = new_source
            if target.get("cell_type") == "code":
                target["execution_count"] = None
                target["outputs"] = []
            if cell_type and cell_type != target.get("cell_type"):
                target["cell_type"] = cell_type
            new_cell_id = target.get("id", f"cell-{cell_index}")
            result_msg = f"Updated cell {new_cell_id} with {new_source[:80]}"

        notebook["cells"] = cells

        # Write atomically (mirrors writeTextContent in source)
        updated_content = json.dumps(notebook, ensure_ascii=False, indent=_IPYNB_INDENT)
        dir_ = os.path.dirname(notebook_path) or "."
        fd, tmp_path = tempfile.mkstemp(dir=dir_)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(updated_content)
            os.replace(tmp_path, notebook_path)
        except Exception as e:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            return f"<error>Failed to write notebook: {e}</error>"

        # Update file state cache (mirrors readFileState.set in source)
        try:
            mtime = os.path.getmtime(notebook_path)
        except OSError:
            mtime = None
        from utils.file_state_cache import FileState
        ctx.file_state_cache.set(
            notebook_path,
            FileState(content=updated_content, mtime_at_read=mtime),
        )

        return result_msg
