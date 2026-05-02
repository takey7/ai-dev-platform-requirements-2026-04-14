# Orchestrator Host

## Target shape
- one Linux VM
- one dedicated OS user: `platform-orchestrator`
- one resident worker process
- optional public HTTPS endpoint only if webhook mode is enabled
- local macOS always-on runs use a user LaunchAgent instead of systemd

## Required runtime on the host
- `gh`
- `claude`
- `codex`
- `python3`
- `git`
- Atlassian MCP configured for the worker account

The worker account must already be signed in to:
- GitHub CLI
- Claude Code
- Codex CLI

On Linux hosts, provide provisioning secrets through `/etc/platform-orchestrator.env` only:
```bash
ATLASSIAN_API_TOKEN=<jira-admin-token>
```

Local macOS runs may use Keychain service `ai-dev-platform.atlassian-api-token`, but permanent Linux worker hosts should use the systemd environment file with locked-down permissions.

Do not write Jira admin credentials into consuming repos.

## Install layout
- source repo: `/opt/ai-dev-platform-source`
- workspaces root: `/srv/workspaces`
- worker config: `/home/platform-orchestrator/.config/ai-dev-platform/orchestrator.json`
- worker state DB: `/home/platform-orchestrator/.local/state/ai-dev-platform/orchestrator/orchestrator.db`

## systemd
1. Copy `deploy/orchestrator/platform-orchestrator.service` to `/etc/systemd/system/`.
2. Adjust `WorkingDirectory`, `ExecStart`, and `ReadWritePaths` if your paths differ.
3. Reload and enable:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now platform-orchestrator
```

The bundled unit uses `Restart=always`. On restart, the worker reopens the SQLite WAL state DB and requeues inflight `planning` / `coding` / `reviewing` / `pr_open` jobs so polling can resume from the last stored checkpoint.

## macOS LaunchAgent
Use this only for a logged-in local Mac worker. This is the correct local mode because `gh`, `claude`, `codex`, Keychain, and MCP auth are user-session scoped.

```bash
./bin/platform orchestrator install-agent
./bin/platform orchestrator agent-status
```

This installs `~/Library/LaunchAgents/com.ai-dev-platform.orchestrator.plist` with `RunAtLoad=true` and `KeepAlive=true`.

Logs:
- `~/Library/Logs/ai-dev-platform/orchestrator.out.log`
- `~/Library/Logs/ai-dev-platform/orchestrator.err.log`

To inspect without installing:

```bash
./bin/platform orchestrator install-agent --dry-run
```

To remove:

```bash
./bin/platform orchestrator uninstall-agent
```

## Optional Caddy
Polling mode does not need a reverse proxy or public URL. Use Caddy only if you opt in to webhook mode.

## Recommended worker config
Use the CLI to keep polling mode explicit:

```bash
./bin/platform orchestrator configure \
  --event-mode polling \
  --bind-host 127.0.0.1 \
  --bind-port 8787 \
  --codex-model "" \
  --codex-binary auto \
  --codex-ignore-user-config \
  --claude-model default \
  --claude-effort ""
```

Verify the worker Codex binary before starting the worker:

```bash
./bin/platform toolchain doctor
```

Empty `--codex-model` tracks the Codex CLI built-in current default. `--codex-binary auto` resolves a compatible Codex CLI into `~/.config/ai-dev-platform/toolchain.json`, so worker subprocesses do not depend on tmux, LaunchAgent, systemd, or shell PATH order. Worker Codex subprocesses ignore `~/.codex/config.toml` by default so a personal model pin such as `model = "gpt-5.2"` cannot silently change production worker behavior. Use `--codex-use-user-config` only for a dedicated worker account where that inheritance is intentional.

Use `platform toolchain pin-codex --binary <path>` only when the worker intentionally pins a specific Codex binary.

Use `--claude-model best --claude-effort xhigh` only for a dedicated worker where the project explicitly wants the most capable available Claude model.

```json
{
  "version": 2,
  "bind_host": "127.0.0.1",
  "bind_port": 8787,
  "event_mode": "polling",
  "public_base_url": "",
  "projects_roots": ["/srv/workspaces"],
  "poll_intervals": {
    "reconcile_seconds": 300,
    "github_seconds": 30,
    "loop_seconds": 5
  },
  "github_mode": "polling",
  "github": {
    "codex_review_authors": ["codex", "codex[bot]", "chatgpt-codex-connector"],
    "auto_review_grace_seconds": 180,
    "fallback_review_grace_seconds": 300,
    "merge_policy": "merge_queue"
  },
  "scheduler": {
    "max_parallel_per_repo": 3,
    "max_parallel_per_project": 5,
    "contract_handshake": "required",
    "max_baton_rounds": 2,
    "control_flag_ttl_seconds": 28800,
    "lease_ttl_seconds": 1800,
    "waiting_dependency_warn_seconds": 86400,
    "project_backoff_seconds": 300
  },
  "timeouts": {
    "claude_seconds": 600,
    "codex_exec_seconds": 900,
    "codex_review_seconds": 180,
    "github_checks_seconds": 2700,
    "merge_queue_seconds": 7200
  },
  "failure": {
    "max_attempts": 2,
    "backlog_statuses": ["To Do", "Backlog"]
  },
  "ai": {
    "codex_model": "",
    "codex_binary": "auto",
    "codex_ignore_user_config": true,
    "claude_model": "default",
    "claude_effort": ""
  },
  "jira_site_url": "https://<site>.atlassian.net",
  "jira_admin_email": "you@example.com"
}
```

## Multi-project safety rules
- one repo maps to one Jira project key
- polling JQL and comment checks are scoped to each repo's Jira project key
- duplicate Jira project keys across repos must fail registration
- all sticky comments, jobs, worktrees, and temporary files stay namespaced by project key
- within one repo, independent issues may run in parallel up to `scheduler.max_parallel_per_repo`
- conflict group and dependency leases prevent concurrent edits to the same shared surface

## Registration flow
For each consuming repo:
```bash
platform orchestrator register --target /srv/workspaces/<repo>
```

That command:
- appends the repo root to `projects_roots[]`
- creates or reuses the control issue
- skips Jira Automation in polling mode
- disables previously tracked webhook-mode Automation rules when switching an existing registration back to polling

To opt in to webhook mode:

```bash
platform orchestrator register \
  --target /srv/workspaces/<repo> \
  --webhook \
  --public-base-url https://orchestrator.<domain>
```

## Validation checklist
1. register two repos with different Jira project keys
2. run `platform orchestrator run --poll-only`
3. add `ai:auto` to one issue in each project
4. confirm jobs, PRs, and Jira summary comments do not cross projects
5. confirm `/ai pause` or `/ai status` comments are picked up by polling
6. confirm Codex review arrives as a real GitHub review or the worker marks only that issue `gate_waiting_human` after fallback timeout
7. confirm Jira moves to `In Progress` / `進行中` / `作業中` after the job starts
8. confirm Jira does not move to `Done` / `完了` at `ready_for_merge`; it moves only after the PR is merged
9. confirm independent issues can run in parallel while shared conflict groups serialize
10. confirm failed issues move back to `To Do` / `Backlog` and the scheduler continues with the next executable issue

## Health and stop-scope triage

Use `health` before restarting the worker blindly:

```bash
platform orchestrator health
platform orchestrator health --project PROJ
platform doctor --target /srv/workspaces/<repo>
```

Read the states by scope:

- `service_health.degraded`: Jira/GitHub/Claude/Codex/toolchain is temporarily unavailable. The queue is preserved and unrelated projects keep running.
- `gate_waiting_human`: one issue is waiting for human approval, pending checks timeout, Codex review, or merge queue progress.
- `gate_failed`: one issue failed a required validation/security/spec gate.
- `waiting_dependency`: one issue is blocked by another issue, but does not consume a parallel slot.
- `blocked`: tool, permission, or contract problem where automatic continuation would be unsafe.

Default control flags expire:

```bash
platform orchestrator pause --project PROJ --ttl 8h
platform orchestrator drain --project PROJ --ttl 8h
platform orchestrator undrain --project PROJ
platform orchestrator resume --project PROJ
```

Use `--no-expire` only for explicit maintenance windows. A permanent global pause should be treated as a high-severity operational condition and cleared with:

```bash
platform orchestrator resume --global
```

If a batch has dependency deadlock or conflict groups that are too broad:

```bash
platform orchestrator batch status --batch PROJ-YYYYMMDDHHMMSS
platform orchestrator batch replan --batch PROJ-YYYYMMDDHHMMSS
```

`batch status` reports runnable, waiting, gate, blocked, done, and conflict-blocked counts so one intentionally stopped PR does not hide independent runnable work.
