#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${ROOT_DIR}/.venv"
ROBOT_BIN="${ROOT_DIR}/.tools/robot/robot"

ensure_repo_root() {
  cd "${ROOT_DIR}"
}

ensure_uv() {
  if command -v uv >/dev/null 2>&1; then
    return 0
  fi

  echo "uv is not installed. Run scripts/bootstrap.sh first." >&2
  exit 1
}

ensure_venv() {
  if [[ -x "${VENV_DIR}/bin/python" ]]; then
    return 0
  fi

  echo "Virtual environment not found at ${VENV_DIR}." >&2
  echo "Run scripts/bootstrap.sh first." >&2
  exit 1
}

run_python() {
  ensure_repo_root
  ensure_uv
  ensure_venv
  uv run --python "${VENV_DIR}/bin/python" python "$@"
}

ensure_robot() {
  if [[ -x "${ROBOT_BIN}" ]]; then
    return 0
  fi

  echo "ROBOT not found at ${ROBOT_BIN}." >&2
  echo "Run scripts/bootstrap.sh first." >&2
  exit 1
}
