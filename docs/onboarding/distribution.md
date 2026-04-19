# Distribution Model

## Formal entrypoint
- The formal entrypoint is the CLI in `scripts/platform.py`.
- `bin/platform` is the convenience wrapper.
- Claude plugin support is supplementary and must not become the only installation path.
- Local authentication is login-based:
  - `claude auth login --claudeai`
  - `codex login`

## Why hybrid
- Central repo keeps contracts, workflows, actions, and docs in one place.
- Consuming repos receive a thin local copy of wrappers, hooks, and shared check helpers.
- `platform upgrade` becomes the controlled way to sync those local copies later.

## What stays centralized
- platform manifest schema
- reusable workflow definitions
- shared review/risk/audit conventions
- release bundle generation
- Claude marketplace catalog and plugin metadata

## What remains repo-local
- package manager lockfiles
- runtime-specific build and test files
- deploy targets and credentials
- stack-specific source layout
- local Claude/Codex login state

## GitHub publish checklist
- push the source repo
- configure it as a template repository
- create `v0.x` release tags
- enable Actions for reusable workflows
- ensure consuming repos can reference this repo by `owner/repo@ref`
- if the source repo is private, open `Settings -> Actions -> General -> Access` and allow reuse from repositories owned by the same user or organization
- keep the source repo name stable after publishing; reusable workflow references do not follow repository renames or redirects
- connect Codex to GitHub through ChatGPT when you want automatic PR reviews or `@codex` review requests
- do not require `OPENAI_API_KEY` for the default `ai-gate` path

## Claude plugin publish checklist
- keep `.claude-plugin/marketplace.json` at the repository root
- validate the plugin manifest before publishing
- add the marketplace with `claude plugin marketplace add <repo-or-url>`
- install the plugin with project or user scope
- recommend Claude Code `2.1.109` or newer to pick up the April 14-15, 2026 plugin and security fixes
