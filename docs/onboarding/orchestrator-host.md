# Orchestrator Host

## Target shape
- one Linux VM
- one dedicated OS user: `platform-orchestrator`
- one resident worker process
- optional public HTTPS endpoint only if webhook mode is enabled

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
  --codex-ignore-user-config \
  --claude-model default \
  --claude-effort ""
```

Empty `--codex-model` tracks the Codex CLI built-in current default. Worker Codex subprocesses ignore `~/.codex/config.toml` by default so a personal model pin such as `model = "gpt-5.2"` cannot silently change production worker behavior. Use `--codex-use-user-config` only for a dedicated worker account where that inheritance is intentional.

Use `--codex-model gpt-5.5 --codex-ignore-user-config` only when the worker intentionally pins Codex.

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
    "fallback_review_grace_seconds": 300
  },
  "ai": {
    "codex_model": "",
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
6. confirm Codex review arrives as a real GitHub review or the worker marks the issue blocked after fallback timeout
