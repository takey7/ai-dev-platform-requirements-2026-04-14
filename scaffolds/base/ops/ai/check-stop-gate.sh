#!/usr/bin/env bash
set -euo pipefail

python3 ./ops/platform/checks.py hook-stop --manifest .platform/platform.yaml
