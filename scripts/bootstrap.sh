#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${ROOT_DIR}/.venv"
TOOLS_DIR="${ROOT_DIR}/.tools/robot"
ROBOT_JAR="${TOOLS_DIR}/robot.jar"
ROBOT_BIN="${TOOLS_DIR}/robot"
DATASETS_DIR="${ROOT_DIR}/datasets/rodi"
DOWNLOAD_RODI=0

RODI_DATASETS=(
  "cmt_denormalized"
  "cmt_renamed"
  "cmt_structured"
  "conference_nofks"
  "conference_renamed"
  "conference_structured"
  "mondial_rel"
  "npd_atomic_tests"
  "sigkdd_mixed"
  "sigkdd_renamed"
  "sigkdd_structured"
)

require_command() {
  if command -v "$1" >/dev/null 2>&1; then
    return 0
  fi

  echo "Missing required command: $1" >&2
  exit 1
}

install_uv() {
  if command -v uv >/dev/null 2>&1; then
    echo "uv already installed: $(command -v uv)"
    return 0
  fi

  echo "uv not found. Installing..."

  if command -v curl >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- https://astral.sh/uv/install.sh | sh
  else
    echo "Neither curl nor wget is available to install uv." >&2
    echo "Install uv manually: https://docs.astral.sh/uv/getting-started/installation/" >&2
    exit 1
  fi

  export PATH="${HOME}/.local/bin:${PATH}"

  if ! command -v uv >/dev/null 2>&1; then
    echo "uv installation completed, but uv is not on PATH." >&2
    echo "Open a new shell or add \$HOME/.local/bin to PATH, then rerun this script." >&2
    exit 1
  fi
}

install_robot() {
  require_command java
  mkdir -p "${TOOLS_DIR}"

  echo "Installing ROBOT in ${TOOLS_DIR}"
  curl -LsSf -o "${ROBOT_JAR}" "https://github.com/ontodev/robot/releases/latest/download/robot.jar"
  curl -LsSf -o "${ROBOT_BIN}" "https://raw.githubusercontent.com/ontodev/robot/master/bin/robot"
  chmod u+x "${ROBOT_BIN}"

  if ! "${ROBOT_BIN}" --version >/dev/null 2>&1; then
    echo "ROBOT installation failed." >&2
    exit 1
  fi
}

download_rodi_datasets() {
  require_command git
  local tmp_dir
  tmp_dir="$(mktemp -d)"
  rm -rf "${DATASETS_DIR}"
  mkdir -p "${DATASETS_DIR}"

  echo "Downloading selected RODI datasets into ${DATASETS_DIR}"
  git clone --depth 1 --filter=blob:none --sparse https://github.com/chrpin/rodi.git "${tmp_dir}/rodi"
  (
    cd "${tmp_dir}/rodi"
    git sparse-checkout set "${RODI_DATASETS[@]/#/data/}"
  )

  for dataset in "${RODI_DATASETS[@]}"; do
    rm -rf "${DATASETS_DIR}/${dataset}"
    cp -R "${tmp_dir}/rodi/data/${dataset}" "${DATASETS_DIR}/${dataset}"
  done

  rm -rf "${tmp_dir}"
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --download-rodi)
        DOWNLOAD_RODI=1
        shift
        ;;
      *)
        echo "Unknown option: $1" >&2
        echo "Usage: bash scripts/bootstrap.sh [--download-rodi]" >&2
        exit 1
        ;;
    esac
  done
}

main() {
  parse_args "$@"
  require_command python3
  require_command curl
  install_uv

  echo "Creating virtual environment in ${VENV_DIR}"
  uv venv "${VENV_DIR}"

  echo "Installing project dependencies"
  uv pip install --python "${VENV_DIR}/bin/python" -r "${ROOT_DIR}/requirements.txt" requests

  install_robot
  if [[ "${DOWNLOAD_RODI}" -eq 1 ]]; then
    download_rodi_datasets
  fi

  cat <<EOF

Bootstrap complete.

Virtual environment:
  ${VENV_DIR}

Activate it with:
  source "${VENV_DIR}/bin/activate"

ROBOT binary:
  ${ROBOT_BIN}

Selected RODI datasets destination:
  ${DATASETS_DIR}

Example commands:
  bash scripts/bootstrap.sh --download-rodi
  bash scripts/create_pg_compatible_dataset.sh datasets/rodi
  bash scripts/generate_owlxml_ontologies.sh pg_compatible/outputs/data_pg_compatible
  bash scripts/create_all_mapping.sh pg_compatible/outputs/data_pg_compatible --dry-run
EOF
}

main "$@"
