#!/usr/bin/env python3
"""
Forge HTTP server — provides a streaming SSE API for IDE plugins.

Usage:
    python server.py --cwd /path/to/project --port 8765
"""
from __future__ import annotations
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

try:
    from fastapi import FastAPI, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import StreamingResponse, JSONResponse
    import uvicorn
except ImportError:
    print("ERROR: fastapi and uvicorn are required for the server mode.")
    print("Install with:  pip install fastapi uvicorn")
    sys.exit(1)

from pydantic import BaseModel

from query_engine import QueryEngine
from tools import (
    BashTool, FileReadTool, FileEditTool, FileWriteTool,
    GlobTool, GrepTool, WebFetchTool, WebSearchTool, AgentTool,
)
from services.api import QwenClient
from query import DEFAULT_SYSTEM_PROMPT

# ── Config ────────────────────────────────────────────────────────────────────

API_KEY = os.environ.get("FORGE_API_KEY")
MODEL   = os.environ.get("FORGE_MODEL", "qwen3-coder-plus")

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="Forge", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# One engine per server instance (single-session mode)
_engine: QueryEngine | None = None


def _get_engine(cwd: str) -> QueryEngine:
    global _engine
    if _engine is None or _engine.cwd != cwd:
        tools = _build_tools(cwd)
        agent_api = QwenClient(api_key=API_KEY, model=MODEL)
        agent_tool = AgentTool(all_tools=tools, api_client=agent_api, max_turns=20)
        tools.append(agent_tool)
        agent_tool._all_tools = tools
        _engine = QueryEngine(
            api_key=API_KEY,
            model=MODEL,
            cwd=cwd,
            tools=tools,
            permission_mode="bypassPermissions",
        )
    return _engine


def _build_tools(cwd: str) -> list:
    return [
        BashTool(),
        FileReadTool(),
        FileEditTool(),
        FileWriteTool(),
        GlobTool(),
        GrepTool(),
        WebFetchTool(),
        WebSearchTool(),
    ]


# ── Models ────────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    cwd: str = ""


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"ok": True, "model": MODEL}


@app.post("/clear")
async def clear(req: ChatRequest):
    cwd = req.cwd or os.getcwd()
    engine = _get_engine(cwd)
    engine.clear()
    return {"ok": True}


@app.post("/chat")
async def chat(req: ChatRequest):
    cwd = req.cwd or os.getcwd()
    engine = _get_engine(cwd)

    async def event_stream():
        try:
            async for event in engine.submit_message(req.message):
                # Filter out internal persistence events
                if event["type"] in ("assistant_message", "tool_result_message", "injected_message"):
                    continue
                data = json.dumps(event, ensure_ascii=False)
                yield f"data: {data}\n\n"
        except Exception as e:
            err = json.dumps({"type": "error", "message": str(e)})
            yield f"data: {err}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    if not API_KEY:
        print("Error: FORGE_API_KEY is not set.")
        print("Set it in your environment before starting server mode.")
        sys.exit(1)

    parser = argparse.ArgumentParser(description="Forge HTTP server")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--cwd", default=os.getcwd())
    args = parser.parse_args()

    # Pre-warm the engine
    _get_engine(os.path.abspath(args.cwd))

    print(f"CodingAgent server listening on http://{args.host}:{args.port}")
    print(f"  cwd  : {args.cwd}")
    print(f"  model: {MODEL}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
