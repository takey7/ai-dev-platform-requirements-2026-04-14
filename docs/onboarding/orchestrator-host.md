# Orchestrator Host

## Target shape
- one Linux VM
- one dedicated OS user: `platform-orchestrator`
- one public HTTPS endpoint: `https://orchestrator.<domain>`
- one resident worker process
- one reverse proxy in front of the worker

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

Export provisioning secrets through `/etc/platform-orchestrator.env` only:
```bash
ATLASSIAN_API_TOKEN=<jira-admin-token>
```

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

## Caddy
1. Copy `deploy/orchestrator/Caddyfile` into your Caddy config.
2. Replace `orchestrator.example.com` with the real public hostname.
3. Point the reverse proxy to the worker bind address from `orchestrator.json`.

## Recommended worker config
```json
{
  "version": 2,
  "bind_host": "127.0.0.1",
  "bind_port": 8787,
  "public_base_url": "https://orchestrator.example.com",
  "projects_roots": ["/srv/workspaces"],
  "poll_intervals": {
    "reconcile_seconds": 300,
    "github_seconds": 30,
    "loop_seconds": 5
  },
  "github_mode": "polling",
  "github": {
    "codex_review_authors": ["codex", "codex[bot]"],
    "auto_review_grace_seconds": 180,
    "fallback_review_grace_seconds": 300
  },
  "jira_site_url": "https://<site>.atlassian.net",
  "jira_admin_email": "you@example.com"
}
```

## Multi-project safety rules
- one repo maps to one Jira project key
- each registered Jira project gets its own Automation endpoint path
- each registered Jira project gets its own webhook secret
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
- creates or reuses project-scoped Automation rules when `public_base_url` is set
- exports blueprint JSON when live Automation registration is unavailable

## Validation checklist
1. `curl https://orchestrator.<domain>/healthz`
2. register two repos with different Jira project keys
3. verify each project gets a different `/jira/events/<PROJECT_KEY>` target
4. add `ai:auto` to one issue in each project
5. confirm jobs, PRs, and Jira summary comments do not cross projects
6. confirm Codex review arrives as a real GitHub review or the worker marks the issue blocked after fallback timeout
