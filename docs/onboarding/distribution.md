# Distribution Model

## Formal entrypoint
- The formal entrypoint is the CLI in `scripts/platform.py`.
- `bin/platform` is the convenience wrapper.
- `bootstrap` is the canonical repo-level primitive.
- `create-project` is the greenfield orchestration wrapper around the same baseline logic.
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
- repo manifest values such as Jira project key and Confluence space

## Atlassian planes
- Runtime plane:
  - MCP + OAuth
  - used by Claude/Codex during normal repo work
- Provisioning plane:
  - REST + API token
  - used only by `create-project` to create a Jira Software Kanban project
- Resident orchestrator plane:
  - polling-first Jira REST + GitHub polling
  - used by `platform orchestrator run`
  - consumes repo-local manifests but keeps its SQLite state outside tracked repos
  - uses repo project keys to keep multiple projects isolated
- Generated repos must not store Jira admin credentials
- The worker config lives in `~/.config/ai-dev-platform/orchestrator.json`
- The worker DB lives in `~/.local/state/ai-dev-platform/orchestrator/orchestrator.db`

## Orchestrator rollout checklist
- register each consuming repo with `platform orchestrator register --target <repo>`
- run `platform orchestrator run --poll-only`
- run the worker under a dedicated account already logged into `gh`, `claude`, and `codex`
- use Jira label `ai:auto` as the start gate and comment commands for pause/resume/cancel/status
- treat `ready_for_merge` as the worker stop state in v1; do not auto-merge by default
- enable automatic Codex review on each repo; the worker only falls back to `@codex review` if no Codex review artifact arrives
- use `platform orchestrator poll` or `status --refresh` when the worker was stopped and GitHub check/review state needs to be reconciled manually
- use webhook mode only when low-latency Jira events are worth maintaining a public callback URL

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
- monitor recent PRs for a Codex review artifact, not just a request comment

## Claude plugin publish checklist
- keep `.claude-plugin/marketplace.json` at the repository root
- validate the plugin manifest before publishing
- add the marketplace with `claude plugin marketplace add <repo-or-url>`
- install the plugin with project or user scope
- recommend Claude Code `2.1.109` or newer to pick up the April 14-15, 2026 plugin and security fixes

## Jira issue creation policy
- use Claude + MCP as the standard runtime path
- allow issue creation only on explicit user instruction
- keep issue creation inside the repo's fixed Jira project key
- do not add a second CLI path for normal Jira issue creation
