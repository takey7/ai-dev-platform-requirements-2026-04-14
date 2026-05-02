# Onboarding Quickstart

## 1. Publish the source repo
1. Push this repository to GitHub personal/org.
2. Mark it as a template repository.
3. Create an initial semver release such as `v0.1.0`.

## 2. Sign in locally
```bash
claude auth login --claudeai
codex login
```

Use login-based access for both tools. Do not make `OPENAI_API_KEY` part of the default local setup.

## 3. Save local defaults once
```bash
./bin/platform configure \
  --github-owner takey7 \
  --projects-root ~/workspaces \
  --jira-site-url https://<site>.atlassian.net \
  --jira-admin-email you@example.com
```

For local macOS operation, store the Jira admin token in Keychain:
```bash
security add-generic-password \
  -a "$USER" \
  -s ai-dev-platform.atlassian-api-token \
  -w "<jira-admin-token>" \
  -U
```

For one-off or Linux host operation, exporting the token is also supported:
```bash
export ATLASSIAN_API_TOKEN=<jira-admin-token>
```

## 4. Greenfield: create everything in one step
```bash
./bin/platform create-project "Billing API"
```

This creates a GitHub private repo, bootstraps the baseline, provisions a Jira Software Kanban project, and starts `claude` / `codex` in tmux.

## 5. Existing repo: provision GitHub/Jira and bootstrap in one step

Use this when an engineer is already inside the target repo and the repo needs the same environment.

```bash
./bin/platform setup-repo \
  --target /path/to/target-repo \
  --github-owner takey7 \
  --repo-name billing-api \
  --project-name "Billing API" \
  --jira-key BILL \
  --jira-name "Billing API" \
  --confluence-space BILL \
  --adapter node-ts \
  --launch-mode none
```

This creates or attaches a GitHub private repo, creates a Jira Software Kanban project, bootstraps the baseline, commits/pushes the setup, and registers the repo with the polling orchestrator.

For copy-paste instructions for another engineer, use [engineer-existing-repo-handoff.md](engineer-existing-repo-handoff.md).

## 6. Bootstrap a consuming repo directly
```bash
git clone <platform-source-repo>
cd ai-dev-platform-requirements-2026-04-14

./bin/platform bootstrap \
  --target /path/to/consumer-repo \
  --adapter node-ts \
  --issue-project-key PROJ \
  --confluence-space SPACE \
  --source-repo takey7/ai-dev-platform-requirements-2026-04-14 \
  --version <latest-tag>
```

`bootstrap` remains the canonical platform primitive. `create-project` is the greenfield convenience wrapper around the same baseline logic.

`bootstrap` only applies files. It does not create GitHub repos, Jira projects, setup commits, or orchestrator registrations.

## 7. Validate the result
```bash
./bin/platform doctor --target /path/to/consumer-repo
```

## 8. Register the resident orchestrator

```bash
./bin/platform orchestrator register --target /path/to/consumer-repo

./bin/platform orchestrator configure \
  --codex-model "" \
  --codex-binary auto \
  --codex-ignore-user-config \
  --claude-model default \
  --claude-effort ""

./bin/platform toolchain doctor
```

This updates `~/.config/ai-dev-platform/orchestrator.json`, creates or reuses the Jira control issue, and registers the repo for polling-first orchestration.

The worker uses `~/.config/ai-dev-platform/toolchain.json` to select a compatible Codex CLI by absolute path. Empty `--codex-model` means the Codex CLI built-in current default, not a personal model pin in the OS user's config.

Webhook mode is optional. Only use it when you intentionally want Jira Automation callbacks:

```bash
./bin/platform orchestrator register \
  --target /path/to/consumer-repo \
  --webhook \
  --public-base-url https://orchestrator.<domain>
```

## 8. Run the worker
```bash
./bin/platform orchestrator run --poll-only
```

The worker keeps a SQLite WAL state DB under `~/.local/state/ai-dev-platform/orchestrator/`, polls Jira issues/comments and GitHub checks/reviews, mirrors progress to Jira, runs the Claude-Codex mediated baton before implementation, moves active jobs to `In Progress` best-effort, and enables GitHub auto-merge / merge queue when PRs reach `ready_for_merge`. Jira moves to `Done` only after the PR is merged.

For always-on hosting, use the worker host layout in [orchestrator-host.md](orchestrator-host.md). A public URL is required only for optional webhook mode.

On a local Mac, install a user LaunchAgent when you want the polling worker to restart after login:
```bash
./bin/platform orchestrator install-agent
./bin/platform orchestrator agent-status
```

If the worker is not running and a job is waiting on GitHub, refresh state manually:
```bash
./bin/platform orchestrator poll --issue PROJ-123
./bin/platform orchestrator status --issue PROJ-123
```

For a guided first run, use [first-project-walkthrough.md](first-project-walkthrough.md).

For Codex GitHub review setup, use [codex-github-review.md](codex-github-review.md).
For mediated baton and parallel batch operation, use [parallel-batch-orchestrator.md](parallel-batch-orchestrator.md).

To run multiple independent Jira issues in parallel:
```bash
./bin/platform orchestrator batch create \
  --project PROJ \
  --jql 'project = PROJ AND labels = "ai:auto" AND status in ("To Do", "Selected for Development")' \
  --max-parallel 3

./bin/platform orchestrator batch status
```

## 9. Start day-to-day flow
```bash
./bin/platform new-spec PROJ-123 --target /path/to/consumer-repo --title "add queue health alerts"
```

Then:
- implement in a dedicated worktree/branch
- let local hooks run
- open a PR
- let required checks drive merge readiness
- or add the `ai:auto` label in Jira and let the worker drive the same flow automatically
- use Jira comment controls when needed:
  - `/ai pause`
  - `/ai resume`
  - `/ai cancel`
  - `/ai retry`
  - `/ai status`

## 10. Enable GitHub-side Codex reviews
- connect ChatGPT/Codex to GitHub
- enable automatic Codex reviews on each repo
- the worker waits for a Codex review artifact and only falls back to `@codex review` if nothing arrives
- posting `@codex review` is not completion; the worker waits for a GitHub review from `codex` / `codex[bot]` or a `Codex Review:` comment from `chatgpt-codex-connector`
- the shared `ai-gate` workflow is informational and does not require `OPENAI_API_KEY`

## 11. Jira issue creation from Claude
- Jira issue creation is allowed only when the user explicitly asks for it
- Claude must create the issue in the repo's fixed Jira project key only
- default issue type is `Task`

## 12. Upgrade later
```bash
./bin/platform upgrade --target /path/to/consumer-repo --to <new-tag>
```
