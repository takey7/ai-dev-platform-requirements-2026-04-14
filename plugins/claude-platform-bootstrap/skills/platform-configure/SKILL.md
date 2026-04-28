---
name: platform-configure
description: Use when setting or updating user-level defaults for the shared platform CLI, especially before first-time use of create-project.
---

# Platform Configure

## Workflow

1. Prefer `platform configure ...`.
2. If `platform` is not on `PATH`, use `python3 scripts/platform.py configure ...` from the platform source repo.
3. Configure only non-secret defaults here.
4. Keep Jira admin credentials out of repo files and out of the saved config.

## Defaults

- Use `~/workspaces` as the default projects root unless the user asks for another root.
- Keep the launch mode at `tmux` unless the user explicitly opts out.
- Store `jira.site_url` and `jira.admin_email`, but require `ATLASSIAN_API_TOKEN` to come from the shell environment.
