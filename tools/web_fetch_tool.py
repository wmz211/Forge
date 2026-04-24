from __future__ import annotations
"""
WebFetchTool — fetch a URL and return its content as Markdown.

Uses Jina Reader (https://r.jina.ai/<url>) which requires no API key and
returns clean, LLM-friendly Markdown from any public URL.

For pages exceeding _SUMMARIZE_THRESHOLD chars, calls the Qwen API to
summarize the content before returning — mirrors WebFetchTool.ts behavior
where Claude Haiku is used to distil large pages.
"""
import os
from typing import Any
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from urllib.parse import urlparse

from tool import Tool, ToolContext

# Character cap — roughly 50 000 tokens at 4 chars/token
_MAX_OUTPUT_CHARS = int(os.environ.get("WEB_FETCH_MAX_CHARS", str(200_000)))

# Pages larger than this are summarized via LLM before returning.
# Mirrors WebFetchTool.ts large-page threshold (~100 KB of raw text).
_SUMMARIZE_THRESHOLD = int(os.environ.get("WEB_FETCH_SUMMARIZE_THRESHOLD", str(50_000)))

# Cap how much content we feed to the summarizer (avoid huge prompts)
_SUMMARIZE_INPUT_MAX = 100_000

# Request timeout in seconds
_DEFAULT_TIMEOUT = 30

_JINA_BASE = "https://r.jina.ai/"


async def _summarize_content(content: str, url: str) -> str:
    """
    Call Qwen to summarize large web page content.
    Mirrors WebFetchTool.ts which calls Claude Haiku for the same purpose.
    Returns the summary, or falls back to truncation if the API key is missing.
    """
    api_key = os.environ.get("FORGE_API_KEY", "")
    if not api_key:
        truncated = content[:_MAX_OUTPUT_CHARS]
        last_nl = truncated.rfind("\n")
        if last_nl > 0:
            truncated = truncated[:last_nl]
        return truncated + f"\n\n[... content truncated at {_MAX_OUTPUT_CHARS:,} chars]"

    from services.api import QwenClient
    client = QwenClient(api_key=api_key, model="qwen3.6-flash", enable_thinking=False)

    trimmed = content[:_SUMMARIZE_INPUT_MAX]
    summary_parts: list[str] = []
    async for event in client.stream(
        messages=[{
            "role": "user",
            "content": (
                f"Summarize the following web page content from {url}. "
                "Preserve all important information, code examples, API details, "
                "and key facts. Format the summary in Markdown.\n\n"
                f"Content:\n{trimmed}"
            ),
        }],
        tools=None,
        system_prompt=(
            "You are a precise web content summarizer. Your job is to produce a "
            "comprehensive Markdown summary that a developer could use instead of "
            "reading the full page. Do not omit technical details, code snippets, "
            "or version numbers."
        ),
    ):
        if event["type"] == "text":
            summary_parts.append(event["content"])
        elif event["type"] == "done":
            break

    summary = "".join(summary_parts)
    return (
        f"[Summarized from {url} — original: {len(content):,} chars]\n\n{summary}"
    )


def _is_valid_url(url: str) -> bool:
    try:
        p = urlparse(url)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


class WebFetchTool(Tool):
    name = "WebFetch"
    search_hint = "fetch and extract content from a URL"
    should_defer = True
    description = (
        "Fetches a URL and returns its content as Markdown text. "
        "Use this to read documentation, articles, GitHub files, or any public web page. "
        "Content is converted to clean Markdown via Jina Reader — no API key required.\n\n"
        "Usage notes:\n"
        "- Only public URLs are supported (no login-gated pages)\n"
        "- For very large pages, output may be truncated\n"
        "- Prefer this over Bash curl/wget for readable content"
    )
    is_concurrency_safe = True

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to fetch (must start with http:// or https://)",
                },
                "timeout": {
                    "type": "integer",
                    "description": (
                        f"Request timeout in seconds (default {_DEFAULT_TIMEOUT}). "
                        "Increase for slow sites."
                    ),
                },
            },
            "required": ["url"],
        }

    def render_call_summary(self, args: dict[str, Any]) -> str | None:
        url = args.get("url", "")
        try:
            parsed = urlparse(url)
            return parsed.netloc + (parsed.path or "")
        except Exception:
            return url

    async def call(self, input: dict[str, Any], ctx: ToolContext) -> str:
        url: str = input.get("url", "").strip()
        timeout: int = int(input.get("timeout") or _DEFAULT_TIMEOUT)

        if not url:
            return "<error>url is required</error>"

        if not _is_valid_url(url):
            return (
                f"<error>Invalid URL: '{url}'. "
                "URL must start with http:// or https:// and include a domain.</error>"
            )

        jina_url = _JINA_BASE + url

        req = Request(
            jina_url,
            headers={
                "User-Agent": "Forge/1.0",
                "Accept": "text/plain, text/markdown, */*",
                # Ask Jina for plain-text Markdown output
                "X-Return-Format": "markdown",
            },
        )

        try:
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except HTTPError as e:
            return f"<error>HTTP {e.code} fetching {url}: {e.reason}</error>"
        except URLError as e:
            return f"<error>Failed to fetch {url}: {e.reason}</error>"
        except TimeoutError:
            return f"<error>Request timed out after {timeout}s fetching {url}</error>"
        except Exception as e:
            return f"<error>Unexpected error fetching {url}: {e}</error>"

        try:
            content = raw.decode("utf-8", errors="replace")
        except Exception as e:
            return f"<error>Could not decode response from {url}: {e}</error>"

        if not content.strip():
            return f"<result>Page at {url} returned empty content.</result>"

        # Large pages: summarize with LLM instead of hard truncation.
        # Mirrors WebFetchTool.ts which calls Claude Haiku for pages exceeding
        # a token budget. We threshold on chars (~4 chars/token).
        if len(content) > _SUMMARIZE_THRESHOLD:
            return await _summarize_content(content, url)

        if len(content) > _MAX_OUTPUT_CHARS:
            content = content[:_MAX_OUTPUT_CHARS]
            last_nl = content.rfind("\n")
            if last_nl > 0:
                content = content[:last_nl]
            content += f"\n\n[... content truncated at {_MAX_OUTPUT_CHARS:,} characters]"

        return content
