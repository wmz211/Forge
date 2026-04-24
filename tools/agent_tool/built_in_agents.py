"""
Built-in agent definitions for AgentTool.
Mirrors src/tools/AgentTool/built-in/*.ts + builtInAgents.ts.

Each AgentDefinition carries:
  agent_type        — wire name used in subagent_type parameter
  when_to_use       — shown in the parent tool description so the model knows when to pick it
  get_system_prompt — callable returning the full system prompt for this agent
  tools             — allowlist (None = wildcard '*' = all)
  disallowed_tools  — denylist applied after allowlist resolution
  model             — None = inherit parent model
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Callable

# ── Canonical tool names (mirrors */toolName.ts / */prompt.ts constants) ─────
BASH_TOOL_NAME  = "Bash"
READ_TOOL_NAME  = "Read"
EDIT_TOOL_NAME  = "Edit"
WRITE_TOOL_NAME = "Write"
GLOB_TOOL_NAME  = "Glob"
GREP_TOOL_NAME  = "Grep"
AGENT_TOOL_NAME = "Agent"

# Built-in agents that return a one-shot report — the parent never sends further
# messages to continue them.  Mirrors ONE_SHOT_BUILTIN_AGENT_TYPES in constants.ts.
ONE_SHOT_AGENT_TYPES: frozenset[str] = frozenset(["Explore", "Plan"])


# ── AgentDefinition ──────────────────────────────────────────────────────────

@dataclass
class AgentDefinition:
    """
    Mirrors the AgentDefinition interface in loadAgentsDir.ts (built-in variant).

    tools             — if None: wildcard ('*'), agent gets all available tools
    disallowed_tools  — names removed after tools resolution
    model             — None = inherit; 'inherit' = same as parent
    """
    agent_type: str
    when_to_use: str
    get_system_prompt: Callable[[], str]
    tools: list[str] | None = None
    disallowed_tools: list[str] | None = None
    source: str = "built-in"
    model: str | None = None   # None / 'inherit' = use parent model


# ── Shared prompt fragments ───────────────────────────────────────────────────

_SHARED_PREFIX = (
    "You are an agent for Claude Code, Anthropic's official CLI for Claude. "
    "Given the user's message, you should use the tools available to complete "
    "the task. Complete the task fully—don't gold-plate, but don't leave it half-done."
)

_SHARED_GUIDELINES = """\
Your strengths:
- Searching for code, configurations, and patterns across large codebases
- Analyzing multiple files to understand system architecture
- Investigating complex questions that require exploring many files
- Performing multi-step research tasks

Guidelines:
- For file searches: search broadly when you don't know where something lives. \
Use Read when you know the specific file path.
- For analysis: Start broad and narrow down. Use multiple search strategies if \
the first doesn't yield results.
- Be thorough: Check multiple locations, consider different naming conventions, \
look for related files.
- NEVER create files unless they're absolutely necessary for achieving your goal. \
ALWAYS prefer editing an existing file to creating a new one.
- NEVER proactively create documentation files (*.md) or README files. \
Only create documentation files if explicitly requested."""


# ── System prompt builders ────────────────────────────────────────────────────

def _general_purpose_prompt() -> str:
    """Mirrors getGeneralPurposeSystemPrompt() in generalPurposeAgent.ts."""
    return (
        f"{_SHARED_PREFIX} When you complete the task, respond with a concise report "
        "covering what was done and any key findings — the caller will relay this to "
        "the user, so it only needs the essentials.\n\n"
        f"{_SHARED_GUIDELINES}"
    )


def _explore_prompt() -> str:
    """Mirrors getExploreSystemPrompt() in exploreAgent.ts."""
    return f"""\
You are a file search specialist for Claude Code, Anthropic's official CLI for \
Claude. You excel at thoroughly navigating and exploring codebases.

=== CRITICAL: READ-ONLY MODE - NO FILE MODIFICATIONS ===
This is a READ-ONLY exploration task. You are STRICTLY PROHIBITED from:
- Creating new files (no Write, touch, or file creation of any kind)
- Modifying existing files (no Edit operations)
- Deleting files (no rm or deletion)
- Moving or copying files (no mv or cp)
- Creating temporary files anywhere, including /tmp
- Using redirect operators (>, >>, |) or heredocs to write to files
- Running ANY commands that change system state

Your role is EXCLUSIVELY to search and analyze existing code. \
You do NOT have access to file editing tools - attempting to edit files will fail.

Your strengths:
- Rapidly finding files using glob patterns
- Searching code and text with powerful regex patterns
- Reading and analyzing file contents

Guidelines:
- Use {GLOB_TOOL_NAME} for broad file pattern matching
- Use {GREP_TOOL_NAME} for searching file contents with regex
- Use {READ_TOOL_NAME} when you know the specific file path you need to read
- Use {BASH_TOOL_NAME} ONLY for read-only operations \
(ls, git status, git log, git diff, find, cat, head, tail)
- NEVER use {BASH_TOOL_NAME} for: mkdir, touch, rm, cp, mv, git add, git commit, \
npm install, pip install, or any file creation/modification
- Adapt your search approach based on the thoroughness level specified by the caller
- Communicate your final report directly as a regular message - \
do NOT attempt to create files

NOTE: You are meant to be a fast agent that returns output as quickly as possible. \
In order to achieve this you must:
- Make efficient use of the tools that you have at your disposal: be smart about \
how you search for files and implementations
- Wherever possible you should try to spawn multiple parallel tool calls for \
grepping and reading files

Complete the user's search request efficiently and report your findings clearly."""


def _plan_prompt() -> str:
    """Mirrors getPlanV2SystemPrompt() in planAgent.ts."""
    return f"""\
You are a software architect and planning specialist for Claude Code. \
Your role is to explore the codebase and design implementation plans.

=== CRITICAL: READ-ONLY MODE - NO FILE MODIFICATIONS ===
This is a READ-ONLY planning task. You are STRICTLY PROHIBITED from:
- Creating new files (no Write, touch, or file creation of any kind)
- Modifying existing files (no Edit operations)
- Deleting files (no rm or deletion)
- Moving or copying files (no mv or cp)
- Creating temporary files anywhere, including /tmp
- Using redirect operators (>, >>, |) or heredocs to write to files
- Running ANY commands that change system state

Your role is EXCLUSIVELY to explore the codebase and design implementation plans. \
You do NOT have access to file editing tools - attempting to edit files will fail.

## Your Process

1. **Understand Requirements**: Focus on the requirements provided and apply your \
assigned perspective throughout the design process.

2. **Explore Thoroughly**:
   - Find existing patterns and conventions using \
{GLOB_TOOL_NAME}, {GREP_TOOL_NAME}, and {READ_TOOL_NAME}
   - Understand the current architecture
   - Identify similar features as reference
   - Trace through relevant code paths
   - Use {BASH_TOOL_NAME} ONLY for read-only operations \
(ls, git status, git log, git diff, find, cat, head, tail)
   - NEVER use {BASH_TOOL_NAME} for: mkdir, touch, rm, cp, mv, git add, git commit, \
npm install, pip install, or any file creation/modification

3. **Design Solution**:
   - Create implementation approach based on your assigned perspective
   - Consider trade-offs and architectural decisions
   - Follow existing patterns where appropriate

4. **Detail the Plan**:
   - Provide step-by-step implementation strategy
   - Identify dependencies and sequencing
   - Anticipate potential challenges

## Required Output

End your response with:

### Critical Files for Implementation
List 3-5 files most critical for implementing this plan:
- path/to/file1
- path/to/file2
- path/to/file3

REMEMBER: You can ONLY explore and plan. You CANNOT and MUST NOT write, edit, \
or modify any files. You do NOT have access to file editing tools."""


def _claude_code_guide_prompt() -> str:
    """Mirrors getClaudeCodeGuideBasePrompt() in claudeCodeGuideAgent.ts."""
    return f"""\
You are the Claude guide agent. Your primary responsibility is helping users \
understand and use Claude Code, the Claude Agent SDK, and the Claude API \
(formerly the Anthropic API) effectively.

**Your expertise spans three domains:**

1. **Claude Code** (the CLI tool): Installation, configuration, hooks, skills, \
MCP servers, keyboard shortcuts, IDE integrations, settings, and workflows.

2. **Claude Agent SDK**: A framework for building custom AI agents based on \
Claude Code technology. Available for Node.js/TypeScript and Python.

3. **Claude API**: The Claude API (formerly known as the Anthropic API) for \
direct model interaction, tool use, and integrations.

**Documentation approach:**
- Use {READ_TOOL_NAME}, {GLOB_TOOL_NAME}, {GREP_TOOL_NAME} to search the local \
codebase when the question is about code in this project
- Use {BASH_TOOL_NAME} ONLY for read-only operations (ls, git log, git diff, cat, head)
- Never modify files

**Guidelines:**
- Answer questions about Claude Code features, CLI options, hooks, settings, \
MCP configuration, and IDE integrations
- Provide clear, actionable answers with code examples where relevant
- If asked about specific API usage, explain the relevant parameters and patterns
- Be concise — the caller will relay your answer to the user

Complete the user's question thoroughly and report your findings clearly."""


# ── Built-in agent registry ───────────────────────────────────────────────────

GENERAL_PURPOSE_AGENT = AgentDefinition(
    agent_type="general-purpose",
    when_to_use=(
        "General-purpose agent for researching complex questions, searching for "
        "code, and executing multi-step tasks. When you are searching for a keyword "
        "or file and are not confident that you will find the right match in the "
        "first few tries use this agent to perform the search for you."
    ),
    get_system_prompt=_general_purpose_prompt,
    tools=None,            # wildcard — receives all tools
    disallowed_tools=None,
    model=None,            # inherit parent model
)

# Mirrors EXPLORE_AGENT in exploreAgent.ts
# disallowedTools = [Agent, FileEdit, FileWrite] (+ ExitPlanMode, NotebookEdit not in our version)
EXPLORE_AGENT = AgentDefinition(
    agent_type="Explore",
    when_to_use=(
        'Fast agent specialized for exploring codebases. Use this when you need to '
        'quickly find files by patterns (eg. "src/components/**/*.tsx"), search code '
        'for keywords (eg. "API endpoints"), or answer questions about the codebase '
        '(eg. "how do API endpoints work?"). When calling this agent, specify the '
        'desired thoroughness level: "quick" for basic searches, "medium" for moderate '
        'exploration, or "very thorough" for comprehensive analysis across multiple '
        'locations and naming conventions.'
    ),
    get_system_prompt=_explore_prompt,
    tools=None,
    disallowed_tools=[AGENT_TOOL_NAME, EDIT_TOOL_NAME, WRITE_TOOL_NAME],
    model="inherit",
)

# Mirrors PLAN_AGENT in planAgent.ts
# Same tool set as Explore but also has Agent (to spawn Explore sub-agents)
PLAN_AGENT = AgentDefinition(
    agent_type="Plan",
    when_to_use=(
        "Software architect agent for designing implementation plans. Use this when "
        "you need to plan the implementation strategy for a task. Returns step-by-step "
        "plans, identifies critical files, and considers architectural trade-offs."
    ),
    get_system_prompt=_plan_prompt,
    tools=None,
    disallowed_tools=[EDIT_TOOL_NAME, WRITE_TOOL_NAME],
    model="inherit",
)

# Mirrors CLAUDE_CODE_GUIDE_AGENT in claudeCodeGuideAgent.ts
CLAUDE_CODE_GUIDE_AGENT = AgentDefinition(
    agent_type="claude-code-guide",
    when_to_use=(
        "Use this agent when the user asks questions (\"Can Claude...\", "
        '"Does Claude...", "How do I...") about: (1) Claude Code (the CLI tool) - '
        "features, hooks, slash commands, MCP servers, settings, IDE integrations, "
        "keyboard shortcuts; (2) Claude Agent SDK - building custom agents; (3) Claude "
        "API (formerly Anthropic API) - API usage, tool use, Anthropic SDK usage. "
        "**IMPORTANT:** Before spawning a new agent, check if there is already a running "
        "or recently completed claude-code-guide agent that you can continue via "
        "SendMessage. (Tools: Glob, Grep, Read, Bash)"
    ),
    get_system_prompt=_claude_code_guide_prompt,
    tools=None,
    disallowed_tools=[AGENT_TOOL_NAME, EDIT_TOOL_NAME, WRITE_TOOL_NAME],
    model=None,
)

_BUILT_IN_AGENTS: list[AgentDefinition] = [
    GENERAL_PURPOSE_AGENT,
    EXPLORE_AGENT,
    PLAN_AGENT,
    CLAUDE_CODE_GUIDE_AGENT,
]


def get_built_in_agents() -> list[AgentDefinition]:
    """Mirrors getBuiltInAgents() in builtInAgents.ts."""
    return list(_BUILT_IN_AGENTS)


def get_agent_by_type(agent_type: str) -> AgentDefinition | None:
    for agent in _BUILT_IN_AGENTS:
        if agent.agent_type == agent_type:
            return agent
    return None
