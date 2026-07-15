# Codex CLI Reference

## Overview

Codex is OpenAI's command-line coding agent. It runs in the terminal, reads and
edits files in your working directory, and can execute commands to implement and
verify changes. This page is an original, paraphrased summary of the official
Codex CLI documentation, written for use as a grounded demo corpus.

## CLI flags

The --model flag selects the model Codex uses for code generation. Run
'codex --help' for the full list of flags. Common options control the working
directory, the approval mode for commands, and whether the session runs in a
sandbox. Flags passed on the command line override values set in the config file.

## Authentication

Codex reads its credentials from the OPENAI_API_KEY environment variable, or from
a stored login created by signing in through the CLI. Keep the key out of source
control; prefer an environment variable or the CLI's own credential store over
pasting the key into scripts.

## Approval modes

Codex supports different approval modes that trade autonomy for safety. A
read-only mode answers questions without changing files; a mode that asks before
running commands keeps a human in the loop; and a fuller-auto mode lets the agent
run commands within a sandbox. Choose the least-privileged mode that still lets the
task finish.

## Sandbox

When sandboxing is enabled, commands run with restricted file-system and network
access so a mistaken or unsafe command cannot damage the wider system. The sandbox
is the safety net that makes higher autonomy modes reasonable; disabling it removes
those guarantees, so do so only when you understand the risk.

## Errors

A "rate limit exceeded" error means the OPENAI_API_KEY environment variable is set
and valid but the account is over its quota; wait and retry, or raise the account
limit. An authentication error instead means the key is missing or invalid. A
sandbox-denied error means a command tried to touch a resource the sandbox blocks.

## Configuration

Persistent settings live in a config file so you do not repeat flags every run.
The file can pin a default model, an approval mode, and sandbox behavior.
Command-line flags always take precedence over the config file for a single
invocation.
