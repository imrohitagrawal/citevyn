"""The documentation pages the watcher fetches (ADR-0005 §5, Phase 7A).

A fixed, reviewed list: the watcher fetches exactly these URLs and nothing it
finds along the way. Each is the vendor's own Markdown version of a key page,
chosen from the vendor's ``llms.txt`` index where one exists. Checked on
2026-09-27: every URL returned 200 ``text/markdown``, robots.txt allowed it, and
two fetches seconds apart were byte-identical (so a diff means a real change).

Edit this list to change what is watched; the hosts allowlist follows from it.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class WatchedPage:
    product_area: str  # the corpus product area it belongs to
    title: str
    url: str


WATCHED_PAGES: tuple[WatchedPage, ...] = (
    # Claude API (docs.anthropic.com now redirects here)
    WatchedPage(
        "claude_api", "API overview", "https://platform.claude.com/docs/en/api/overview.md"
    ),
    WatchedPage(
        "claude_api",
        "Models overview",
        "https://platform.claude.com/docs/en/about-claude/models/overview.md",
    ),
    WatchedPage(
        "claude_api",
        "Model IDs and versions",
        "https://platform.claude.com/docs/en/about-claude/models/model-ids-and-versions.md",
    ),
    WatchedPage(
        "claude_api", "Rate limits", "https://platform.claude.com/docs/en/api/rate-limits.md"
    ),
    WatchedPage(
        "claude_api", "Pricing", "https://platform.claude.com/docs/en/about-claude/pricing.md"
    ),
    WatchedPage("claude_api", "Errors", "https://platform.claude.com/docs/en/api/errors.md"),
    # Claude Code
    WatchedPage("claude_code", "Overview", "https://code.claude.com/docs/en/overview.md"),
    WatchedPage("claude_code", "Settings", "https://code.claude.com/docs/en/settings.md"),
    WatchedPage("claude_code", "Permissions", "https://code.claude.com/docs/en/permissions.md"),
    WatchedPage("claude_code", "CLI reference", "https://code.claude.com/docs/en/cli-reference.md"),
    WatchedPage(
        "claude_code", "Model configuration", "https://code.claude.com/docs/en/model-config.md"
    ),
    WatchedPage(
        "claude_code", "Authentication", "https://code.claude.com/docs/en/authentication.md"
    ),
    WatchedPage("claude_code", "Costs", "https://code.claude.com/docs/en/costs.md"),
    # Codex (developers.openai.com/codex/llms.txt points here)
    WatchedPage("codex", "Authentication", "https://learn.chatgpt.com/docs/auth.md"),
    WatchedPage(
        "codex", "Settings reference", "https://learn.chatgpt.com/docs/reference/settings.md"
    ),
    WatchedPage(
        "codex", "Commands reference", "https://learn.chatgpt.com/docs/reference/commands.md"
    ),
    WatchedPage(
        "codex",
        "Approvals and security",
        "https://learn.chatgpt.com/docs/agent-approvals-security.md",
    ),
    WatchedPage(
        "codex", "AGENTS.md", "https://learn.chatgpt.com/docs/agent-configuration/agents-md.md"
    ),
    WatchedPage("codex", "Rules", "https://learn.chatgpt.com/docs/agent-configuration/rules.md"),
    # Gemini API (no llms.txt; each page has a .md.txt twin)
    WatchedPage("gemini_api", "Models", "https://ai.google.dev/gemini-api/docs/models.md.txt"),
    WatchedPage(
        "gemini_api", "Rate limits", "https://ai.google.dev/gemini-api/docs/rate-limits.md.txt"
    ),
    WatchedPage("gemini_api", "API keys", "https://ai.google.dev/gemini-api/docs/api-key.md.txt"),
    WatchedPage("gemini_api", "Pricing", "https://ai.google.dev/gemini-api/docs/pricing.md.txt"),
    WatchedPage(
        "gemini_api",
        "Text generation",
        "https://ai.google.dev/gemini-api/docs/text-generation.md.txt",
    ),
    WatchedPage(
        "gemini_api", "Quickstart", "https://ai.google.dev/gemini-api/docs/quickstart.md.txt"
    ),
)


def watched_hosts(pages: tuple[WatchedPage, ...] = WATCHED_PAGES) -> frozenset[str]:
    """The only hosts the watcher may contact (a redirect may not leave them)."""
    return frozenset(urlsplit(p.url).hostname or "" for p in pages)


__all__ = ["WATCHED_PAGES", "WatchedPage", "watched_hosts"]
