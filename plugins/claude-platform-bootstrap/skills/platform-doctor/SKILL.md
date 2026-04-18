---
name: platform-doctor
description: Use when validating whether a repository conforms to the shared AI development platform, including manifest shape, required files, workflow refs, and tool availability.
---

# Platform Doctor

Run the shared CLI instead of inspecting files one by one:

1. If `platform` exists, run `platform doctor`.
2. Else if `scripts/platform.py` exists in the current repo, run `python3 scripts/platform.py doctor`.
3. Else if `platform-bootstrap` exists, run `platform-bootstrap doctor`.

Report the errors and warnings from the command directly. Do not recreate the doctor logic manually.
