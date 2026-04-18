#!/usr/bin/env bash
set -euo pipefail

PAYLOAD="$(cat || true)"
printf '%s' "${PAYLOAD}" | python3 ./ops/platform/checks.py hook-dangerous
