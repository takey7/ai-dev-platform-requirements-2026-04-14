# Codex External Agent Session Import

Codex CLI `0.128.0+` can detect recent external agent session files, including Claude Code sessions under `~/.claude/projects/**/*.jsonl`, and import them into Codex history.

This platform treats that feature as optional user assistance, not as orchestrator state.

## What It Is For
- Reopen useful Claude Code context from Codex history.
- Let a human carry recent local investigation context into an interactive Codex session.
- Improve UX when switching between Claude Code and Codex manually.

## What It Is Not For
- It does not replace orchestrator `contracts`, `messages`, `decisions`, `attempts`, or Jira sticky comments.
- It is not used as the source of truth for automated baton pass.
- The worker does not auto-import sessions, because that would blur repo scope and audit boundaries.

## Doctor Checks
Run:

```bash
./bin/platform toolchain doctor
./bin/platform doctor --target /path/to/consumer-repo
```

`toolchain doctor` reports `external_agent_session_import_available: true` only when the resolved Codex CLI is `0.128.0+`.

`doctor --target` detects recent Claude Code session files whose recorded `cwd` matches the target repo and prints an optional import hint.

## Scope And Secrets
- Import only sessions that belong to the same repo you are currently working in.
- Do not import sessions containing secrets or credentials unless you have reviewed the session contents.
- Do not use imported session history to override `.platform/platform.yaml`, Jira project scope, or orchestrator decisions.

## Recommended Policy
- Worker minimum Codex CLI stays `0.125.0+` for v0.2.x.
- Human-facing recommendation is `0.128.0+` if you want external agent session import.
- Orchestrator automation remains structured-contract based even when import is available.
