# Claude Code Reference

## Overview

Claude Code is Anthropic's agentic coding tool for working on a codebase in plain
language — it reads and edits files in your project, runs commands, and answers
questions about the code. It is available in several forms that share the same
agent: a command-line tool in your terminal, a desktop app, a web app, and
extensions for popular IDEs such as VS Code and JetBrains. It is powered by
Claude, a large language model (LLM), so Claude Code is an LLM-based tool. This
page is an original, paraphrased summary of the official Claude Code
documentation, written for use as a grounded demo corpus.

## Installation

The recommended way to install Claude Code is the native installer: on macOS, Linux
or WSL run 'curl -fsSL https://claude.ai/install.sh | bash', and on Windows
PowerShell run 'irm https://claude.ai/install.ps1 | iex'. Package managers also
work — 'brew install --cask claude-code' on macOS and
'winget install Anthropic.ClaudeCode' on Windows. It can also be installed from
npm with 'npm install -g @anthropic-ai/claude-code', which requires Node.js v22 or
later; do not install it with sudo. Supported platforms are macOS 13.0+, Windows 10
1809+, Ubuntu 20.04+, Debian 10+ and Alpine 3.19+, on x64 or ARM64 with at least
4 GB of RAM, and an internet connection is required.

Confirm the install with 'claude --version', and run 'claude doctor' for read-only
diagnostics of install health and settings. Native installs auto-update in the
background by default and can also be updated on demand with 'claude update'; set
DISABLE_AUTOUPDATER=1 to turn that off. Homebrew, WinGet and Linux
package-manager installs do not auto-update — refresh those with
'brew upgrade claude-code' or 'winget upgrade Anthropic.ClaudeCode'. An npm global
install does auto-update, because it installs the same native binary; refresh it
on demand with 'npm install -g @anthropic-ai/claude-code@latest'.

## First run and sign-in

Run 'claude' to start it; the first launch opens a browser to sign in. If the browser
does not open, press 'c' to copy the login URL and open it yourself. If the browser
shows a login code instead of redirecting back — common over SSH, in WSL2 and in
containers, where it cannot reach the local callback server — paste that code into
the terminal.

Sign-in works with a Claude Pro or Max subscription, with Claude for Teams or Enterprise, or with a
Claude Console account. The free Claude.ai plan does not include Claude Code. As an
alternative to signing in, set the ANTHROPIC_API_KEY environment variable, or run
'claude setup-token' to mint a long-lived token for CI and scripts. Use '/logout' to
sign out.

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
