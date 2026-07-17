# Claude Code Reference

## Overview

Claude Code is Anthropic's agentic coding tool that runs in the terminal, reads
and edits files in your project, runs commands, and answers questions about the
codebase. It is powered by Claude, a large language model (LLM), so Claude Code is
an LLM-based tool. This page is an original, paraphrased summary of the official Claude
Code documentation, written for use as a grounded demo corpus.

## Permissions

Claude Code permissions are configured in the project's settings file. Use the
allow and deny lists to gate which tools and commands the assistant may run. By
default, actions that modify files or execute shell commands prompt for approval;
you can pre-approve safe, repetitive commands so you are not asked every time.
Denied entries always win over allowed entries.

## Slash commands

Claude Code responds to built-in slash commands typed at the prompt, including
/help to list commands, /clear to reset the conversation, and /compact to
summarize and shrink the context when it grows long. Projects can define custom
slash commands as prompt files so a team shares the same shortcuts.

## Memory and CLAUDE.md

Claude Code automatically loads a CLAUDE.md file from the project root as project
instructions. Use it to record build and test commands, conventions, and rules the
assistant must follow. Because it is loaded every session, keep it concise and put
the single source of truth there rather than duplicating guidance across files.

## Hooks

Hooks are shell commands the tool runs automatically at defined points in its
lifecycle, such as before or after a tool call. Because the harness executes hooks
rather than the model, hooks are the right mechanism for deterministic, always-on
behavior like formatting on save or blocking a commit that fails a check.

## MCP servers

Claude Code can connect to Model Context Protocol (MCP) servers to gain extra
tools and data sources. Configure a server once and its tools become available to
the assistant. This is how Claude Code integrates with issue trackers, browsers,
and internal services without hard-coding each integration.

## Configuration

Settings are layered: user-level settings apply everywhere, and project-level
settings override them for a specific repository. Prefer project settings for
anything team-specific so the configuration travels with the code and every
contributor gets the same behavior.

## Cost and billing

Claude Code has no separate license fee of its own — what you pay for is the Claude
usage it consumes, billed through your Anthropic account. That works in one of two
ways, chosen when you authenticate: pay-as-you-go API token usage billed through the
Anthropic Console, or an eligible Claude subscription plan whose allowance covers
Claude Code usage. The two are alternatives rather than a single combined bill — a
subscription pauses when its allowance is reached rather than silently spilling over
into metered API charges, and work that needs more than a plan provides can use API
billing instead. Because cost tracks token usage either way, keeping the working
context focused is part of what keeps spending down.
