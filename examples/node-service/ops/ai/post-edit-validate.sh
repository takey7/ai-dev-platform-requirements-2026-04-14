#!/usr/bin/env bash
set -euo pipefail

python3 ./ops/platform/checks.py hook-post-edit --manifest .platform/platform.yaml
