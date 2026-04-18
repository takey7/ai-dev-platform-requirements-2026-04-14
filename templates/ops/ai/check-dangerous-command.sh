#!/usr/bin/env bash
set -euo pipefail

# Skeleton only.
# Read hook payload format from Claude Code docs and adapt this script.
# Intent:
# - deny dangerous commands such as:
#   rm -rf /
#   git push --force
#   terraform apply (prod)
#   kubectl delete on production
#   direct release/deploy commands from local shell

echo "[check-dangerous-command] adapt this script to your hook payload schema"
exit 0
