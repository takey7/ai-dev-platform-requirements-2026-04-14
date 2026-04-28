# Latest Official Updates Checked

Checked on 2026-04-29.

## OpenAI / Codex
- Official latest-model guide now targets `gpt-5.5`.
- `gpt-5.5` is recommended for coding, tool-heavy agents, long-running workflows, grounded assistants, and product-spec-to-plan workflows.
- Migration guidance says to update the model slug to `gpt-5.5`, use the Responses API for reasoning/tool-calling/multi-turn use cases, and tune `reasoning.effort`.
- Impact on this repo:
  - resident orchestrator defaults Codex execution/review stages to the Codex CLI built-in current default
  - worker Codex subprocesses ignore `~/.codex/config.toml` by default so local personal model pins cannot silently change production worker behavior
  - use `codex_model=gpt-5.5` only when a worker intentionally wants an explicit model pin
  - Codex remains login-based through ChatGPT/GitHub Connector
  - `OPENAI_API_KEY` remains outside the standard local/GitHub review path
  - local validation on Codex CLI `0.125.0` confirmed `gpt-5.5`, `gpt-5.4`, and `gpt-5.3-codex` work with ChatGPT login; `gpt-5.2-codex` was rejected for ChatGPT-login Codex CLI

## Claude Code
- Local verified version: `2.1.121`.
- Official Claude Code docs recommend model aliases instead of hard-coded model names:
  - `default` uses the recommended model for the account type
  - `best` uses the most capable available model
  - `opus` and `sonnet` track the latest available Opus/Sonnet model for the provider
- Claude Code installation docs say native installs auto-update, Homebrew stable lags latest, and `claude-code@latest` tracks the latest channel.
- Impact on this repo:
  - recommend upgrading local Claude Code to `2.1.121+`
  - resident orchestrator supports `claude_model` and `claude_effort` worker settings
  - default worker setting is `claude_model=default`
  - use `claude_model=best` and `claude_effort=xhigh` only when a project explicitly wants the most capable Claude path

## GitHub
- Template repositories are still created after the repository exists; this repo should be published first and then marked as a template repository.
- Reusable workflows from a private repository still require explicit Actions access configuration before other private repositories can consume them.
- GitHub Actions added a rerun limit of 50 on April 10, 2026.
- Impact on this repo:
  - added onboarding guidance for private workflow-sharing access
  - no workflow retry loop changes were required because this baseline does not auto-rerun failed workflows
