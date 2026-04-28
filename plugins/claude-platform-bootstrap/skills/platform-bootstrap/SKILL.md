---
name: platform-bootstrap
description: Use when setting up a new repository or retrofitting an existing repository to the shared AI development platform. Always call the shared platform CLI instead of creating baseline files manually.
---

# Platform Bootstrap

## Workflow

1. Prefer the shared CLI over manual file edits.
2. Use `platform create-project ...` for brand-new repositories when the user wants GitHub + Jira + local startup in one flow.
3. Use `platform bootstrap ...` when the repo already exists or the user only wants the baseline applied.
4. If `platform` is on `PATH`, run the matching command directly.
5. Otherwise, if the current repository already contains `scripts/platform.py`, run `python3 scripts/platform.py <command> ...`.
6. Otherwise, if the plugin bin command `platform-bootstrap` is available, run `platform-bootstrap <command> ...`.
7. Only if none of the above exist, tell the user to clone the platform source repository and run the CLI from there.

## Defaults

- Use `--adapter node-ts` for v1 unless the user explicitly says the repo is not Node/TypeScript.
- Prefer the latest released tag when available; fall back to `main` only if no release can be resolved.
- Preserve existing package files unless the repository is empty.
- Jira issue creation is not part of bootstrap. If the user explicitly asks Claude to create a Jira issue later, keep it in the repo's fixed Jira project key only.
