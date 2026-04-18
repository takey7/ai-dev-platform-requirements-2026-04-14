---
name: platform-bootstrap
description: Use when setting up a new repository or retrofitting an existing repository to the shared AI development platform. Always call the shared platform CLI instead of creating baseline files manually.
---

# Platform Bootstrap

## Workflow

1. Prefer the shared CLI over manual file edits.
2. If `platform` is on `PATH`, run `platform bootstrap ...`.
3. Otherwise, if the current repository already contains `scripts/platform.py`, run `python3 scripts/platform.py bootstrap ...`.
4. Otherwise, if the plugin bin command `platform-bootstrap` is available, run `platform-bootstrap bootstrap ...`.
5. Only if none of the above exist, tell the user to clone the platform source repository and run the CLI from there.

## Defaults

- Use `--adapter node-ts` for v1 unless the user explicitly says the repo is not Node/TypeScript.
- Keep `--version main` until the central repo is released with a pinned tag.
- Preserve existing package files unless the repository is empty.
