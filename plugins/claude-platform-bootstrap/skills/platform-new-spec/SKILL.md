---
name: platform-new-spec
description: Use when the user wants a new issue spec under docs/specs using the shared platform template and naming conventions.
---

# Platform New Spec

Create specs through the shared CLI:

1. Prefer `platform new-spec <ISSUE_KEY> --title ...`.
2. If the global CLI is missing, fall back to `python3 scripts/platform.py new-spec ...` when available.
3. Preserve the template structure and fill only the placeholders the CLI supports.
