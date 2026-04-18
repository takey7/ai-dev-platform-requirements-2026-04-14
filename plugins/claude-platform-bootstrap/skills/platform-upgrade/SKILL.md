---
name: platform-upgrade
description: Use when the user wants to sync an existing repository to a newer version of the shared platform baseline without rebuilding the repository from scratch.
---

# Platform Upgrade

Always call the shared CLI:

1. Prefer `platform upgrade --to <version>`.
2. Otherwise, use `python3 scripts/platform.py upgrade --to <version>`.
3. After upgrading, run `platform doctor` or its repo-local equivalent.

Do not hand-edit wrapper workflows or hook files unless the shared CLI cannot be used.
