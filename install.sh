#!/usr/bin/env bash
set -euo pipefail

SOURCE_REPO="${PLATFORM_SOURCE_REPO:-takey7/ai-dev-platform-requirements-2026-04-14}"
SOURCE_REF="${PLATFORM_VERSION:-main}"
TARGET="${PWD}"
PLATFORM_DIR="${PLATFORM_DIR:-${HOME}/.cache/ai-dev-platform/source}"

ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --source-repo)
      SOURCE_REPO="$2"
      shift 2
      ;;
    --version|--source-ref)
      SOURCE_REF="$2"
      shift 2
      ;;
    --target)
      TARGET="$2"
      shift 2
      ;;
    --platform-dir)
      PLATFORM_DIR="$2"
      shift 2
      ;;
    *)
      ARGS+=("$1")
      shift
      ;;
  esac
done

SOURCE_URL="https://github.com/${SOURCE_REPO}.git"
mkdir -p "$(dirname "${PLATFORM_DIR}")"

if [[ ! -d "${PLATFORM_DIR}/.git" ]]; then
  git clone "${SOURCE_URL}" "${PLATFORM_DIR}"
fi

git -C "${PLATFORM_DIR}" fetch --tags origin
git -C "${PLATFORM_DIR}" checkout "${SOURCE_REF}"

exec "${PLATFORM_DIR}/bin/platform" setup-repo \
  --target "${TARGET}" \
  --source-repo "${SOURCE_REPO}" \
  --version "${SOURCE_REF}" \
  "${ARGS[@]}"
