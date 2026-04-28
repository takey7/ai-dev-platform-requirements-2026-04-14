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

Export the Jira admin token only for provisioning:
```bash
export ATLASSIAN_API_TOKEN=<jira-admin-token>
```

## 4. Greenfield: create everything in one step
```bash
./bin/platform create-project "Billing API"
```

This creates a GitHub private repo, bootstraps the baseline, provisions a Jira Software Kanban project, and starts `claude` / `codex` in tmux.

## 5. Bootstrap a consuming repo directly
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

## 6. Validate the result
```bash
./bin/platform doctor --target /path/to/consumer-repo
```

## 7. Register the resident orchestrator
```bash
./bin/platform orchestrator register --target /path/to/consumer-repo
```

This updates `~/.config/ai-dev-platform/orchestrator.json`, creates or reuses the Jira control issue, and attempts to create two project-scoped Automation rules:
- lifecycle -> `POST /jira/events/<PROJECT_KEY>`
- comment control -> `POST /jira/events/<PROJECT_KEY>`

Each registered project gets its own webhook secret. Re-registering one project must not rotate secrets or break other projects.

If live rule creation is not available, the rule payload blueprints are written to `.platform/orchestrator/automation-rules/`.

## 8. Run the worker
```bash
./bin/platform orchestrator run
```

The worker keeps a SQLite WAL state DB under `~/.local/state/ai-dev-platform/orchestrator/`, mirrors progress to Jira, and stops at `ready_for_merge`.

For permanent hosting, use the fixed-URL host layout in [orchestrator-host.md](orchestrator-host.md).

If the worker is not running and a job is waiting on GitHub, refresh state manually:
```bash
./bin/platform orchestrator poll --issue PROJ-123
./bin/platform orchestrator status --issue PROJ-123
```

For a guided first run, use [first-project-walkthrough.md](first-project-walkthrough.md).

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
- the worker waits for a real Codex review artifact and only falls back to `@codex review` if nothing arrives
- posting `@codex review` is not completion; the worker waits for an actual GitHub review from `codex` or `codex[bot]`
- the shared `ai-gate` workflow is informational and does not require `OPENAI_API_KEY`

## 11. Jira issue creation from Claude
- Jira issue creation is allowed only when the user explicitly asks for it
- Claude must create the issue in the repo's fixed Jira project key only
- default issue type is `Task`

## 12. Upgrade later
```bash
./bin/platform upgrade --target /path/to/consumer-repo --to <new-tag>
```
