from __future__ import annotations
"""
WebSearchTool — search the web and return structured results.

Backend priority (first available wins):
  1. Brave Search  — set BRAVE_SEARCH_API_KEY  (highest quality)
  2. Exa Search    — set EXA_API_KEY
  3. DuckDuckGo via Jina Reader — free, no key required (default fallback)
"""
import json
import os
import re
from typing import Any
from urllib.request import Request, urlopen
from urllib.parse import urlencode, quote, parse_qs, urlparse
from urllib.error import URLError, HTTPError

from tool import Tool, ToolContext

_DEFAULT_COUNT = 5
_MAX_COUNT = 20
_TIMEOUT = 20

_BRAVE_API_URL = "https://api.search.brave.com/res/v1/web/search"
_EXA_API_URL   = "https://api.exa.ai/search"
# DuckDuckGo HTML search via Jina Reader (no key needed)
_DDG_HTML_URL  = "https://html.duckduckgo.com/html/?q={query}"
_JINA_BASE     = "https://r.jina.ai/"


# ── DuckDuckGo redirect URL decoder ──────────────────────────────────────────

_DDG_REDIRECT_RE = re.compile(r"[?&]uddg=([^&]+)")

def _decode_ddg_url(href: str) -> str:
    """Extract the real destination URL from a DuckDuckGo redirect link."""
    m = _DDG_REDIRECT_RE.search(href)
    if m:
        from urllib.parse import unquote
        return unquote(m.group(1))
    return href


# ── Markdown result parser for DuckDuckGo output ─────────────────────────────

_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")

_IMAGE_LINE_RE = re.compile(r"^!?\[.*?\]\(")   # lines that are just images/icons

def _strip_md_markup(text: str) -> str:
    """Remove markdown bold, links, and images, leaving plain text."""
    # Remove images: ![alt](url)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    # Replace links: [text](url) → text
    text = _MD_LINK_RE.sub(lambda m: m.group(1), text)
    # Remove bold/italic markers
    text = re.sub(r"\*{1,2}([^*]+)\*{1,2}", r"\1", text)
    return text.strip()


def _parse_ddg_markdown(md: str, count: int) -> str:
    """
    Parse Jina Reader's Markdown rendering of DuckDuckGo HTML into a clean
    numbered result list.  Each result is a heading link followed by a snippet.
    """
    results: list[tuple[str, str, str]] = []   # (title, url, snippet)
    lines = md.splitlines()
    i = 0
    while i < len(lines) and len(results) < count:
        line = lines[i].strip()
        # Result headings: ## [Title](ddg-redirect-url)
        if line.startswith("## ") or line.startswith("# "):
            m = _MD_LINK_RE.search(line)
            if m:
                title = m.group(1).strip()
                url   = _decode_ddg_url(m.group(2))
                # Find the snippet: first non-blank, non-heading, non-image line
                snippet = ""
                j = i + 1
                while j < len(lines):
                    next_line = lines[j].strip()
                    if not next_line or next_line.startswith("#"):
                        j += 1
                        continue
                    # Skip pure icon/image lines and domain/timestamp-only lines
                    if _IMAGE_LINE_RE.match(next_line):
                        j += 1
                        continue
                    cleaned = _strip_md_markup(next_line)
                    # Skip lines that are just a domain name or short date stamp
                    if cleaned and len(cleaned) > 20 and not re.match(
                        r"^[\w.-]+\.\w{2,6}(/\S*)?(\s+\d{4}-\d{2}-\d{2}.*)?$", cleaned
                    ):
                        snippet = cleaned[:300]
                        break
                    j += 1
                if title and url.startswith("http"):
                    results.append((title, url, snippet))
        i += 1

    if not results:
        return "No results found."

    lines_out = []
    for idx, (title, url, snippet) in enumerate(results, 1):
        lines_out.append(f"{idx}. **{title}**")
        lines_out.append(f"   {url}")
        if snippet:
            lines_out.append(f"   {snippet}")
        lines_out.append("")
    return "\n".join(lines_out).strip()


def _ddg_search(query: str, count: int) -> str:
    """Free web search via DuckDuckGo HTML + Jina Reader. No API key needed."""
    ddg_url  = _DDG_HTML_URL.format(query=quote(query))
    jina_url = _JINA_BASE + ddg_url
    req = Request(
        jina_url,
        headers={
            "User-Agent": "Forge/1.0",
            "Accept": "text/plain, text/markdown, */*",
            "X-Return-Format": "markdown",
        },
    )
    with urlopen(req, timeout=_TIMEOUT) as resp:
        md = resp.read().decode("utf-8", errors="replace")

    header = f"Search results for: {query} (via DuckDuckGo)\n\n"
    return header + _parse_ddg_markdown(md, count)


# ── Paid backends ─────────────────────────────────────────────────────────────

def _brave_search(query: str, count: int) -> str:
    api_key = os.environ["BRAVE_SEARCH_API_KEY"]
    params  = urlencode({"q": query, "count": min(count, _MAX_COUNT)})
    req = Request(
        f"{_BRAVE_API_URL}?{params}",
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": api_key,
        },
    )
    with urlopen(req, timeout=_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    results = data.get("web", {}).get("results", [])
    if not results:
        return "No results found."

    lines = [f"Search results for: {query} (via Brave)\n"]
    for i, r in enumerate(results, 1):
        title       = r.get("title", "(no title)")
        url         = r.get("url", "")
        description = r.get("description", "")
        lines.append(f"{i}. **{title}**")
        lines.append(f"   {url}")
        if description:
            lines.append(f"   {description}")
        lines.append("")
    return "\n".join(lines).strip()


def _exa_search(query: str, count: int) -> str:
    api_key = os.environ["EXA_API_KEY"]
    payload = json.dumps({
        "query": query,
        "numResults": min(count, _MAX_COUNT),
        "useAutoprompt": True,
        "contents": {"text": {"maxCharacters": 1000}},
    }).encode("utf-8")
    req = Request(
        _EXA_API_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
        },
        method="POST",
    )
    with urlopen(req, timeout=_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    results = data.get("results", [])
    if not results:
        return "No results found."

    lines = [f"Search results for: {query} (via Exa)\n"]
    for i, r in enumerate(results, 1):
        title   = r.get("title", "(no title)")
        url     = r.get("url", "")
        snippet = (r.get("text") or r.get("highlight") or "").strip()
        lines.append(f"{i}. **{title}**")
        lines.append(f"   {url}")
        if snippet:
            excerpt = snippet[:300].rsplit(" ", 1)[0] + "…" if len(snippet) > 300 else snippet
            lines.append(f"   {excerpt}")
        lines.append("")
    return "\n".join(lines).strip()


# ── Tool ──────────────────────────────────────────────────────────────────────

class WebSearchTool(Tool):
    name = "WebSearch"
    search_hint = "search the web for current information"
    should_defer = True
    description = (
        "Searches the web and returns a list of relevant results with titles, URLs, "
        "and short descriptions. Works out of the box with no configuration required "
        "(uses DuckDuckGo as a free fallback).\n\n"
        "Optional higher-quality backends (set one env var):\n"
        "  - BRAVE_SEARCH_API_KEY  — Brave Search (recommended)\n"
        "  - EXA_API_KEY           — Exa Search\n\n"
        "Usage notes:\n"
        "- Use for factual lookups, library docs, error messages, or any topic "
        "you need fresh information about\n"
        "- Follow up with WebFetch on a result URL to read the full page"
    )
    is_concurrency_safe = True

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query",
                },
                "count": {
                    "type": "integer",
                    "description": (
                        f"Number of results to return (default {_DEFAULT_COUNT}, "
                        f"max {_MAX_COUNT})"
                    ),
                },
            },
            "required": ["query"],
        }

    def render_call_summary(self, args: dict[str, Any]) -> str | None:
        return args.get("query")

    async def call(self, input: dict[str, Any], ctx: ToolContext) -> str:
        query: str = (input.get("query") or "").strip()
        count: int = int(input.get("count") or _DEFAULT_COUNT)

        if not query:
            return "<error>query is required</error>"
        count = max(1, min(count, _MAX_COUNT))

        try:
            if os.environ.get("BRAVE_SEARCH_API_KEY"):
                return _brave_search(query, count)
            elif os.environ.get("EXA_API_KEY"):
                return _exa_search(query, count)
            else:
                return _ddg_search(query, count)
        except HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")[:200]
            except Exception:
                pass
            return f"<error>HTTP {e.code} from search API: {e.reason}. {body}</error>"
        except URLError as e:
            return f"<error>Search request failed: {e.reason}</error>"
        except Exception as e:
            return f"<error>Unexpected search error: {e}</error>"
