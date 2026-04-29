#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${ROOT_DIR}/.venv"
TOOLS_DIR="${ROOT_DIR}/.tools"

if [[ -f "${ROOT_DIR}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${ROOT_DIR}/.env"
  set +a
fi

ROBOT_DIR="${TOOLS_DIR}/robot"
ROBOT_JAR="${ROBOT_DIR}/robot.jar"
ROBOT_BIN="${ROBOT_DIR}/robot"

RODI_DIR="${TOOLS_DIR}/rodi"
ONTOP_DIR="${TOOLS_DIR}/ontop"
BIN_DIR="${TOOLS_DIR}/bin"
PSQL_WRAPPER="${BIN_DIR}/psql_docker.sh"

DATASETS_DIR="${ROOT_DIR}/datasets/rodi"

POSTGRES_CONTAINER="${MPBOOT_PG_CONTAINER:-mpboot-postgres}"
POSTGRES_IMAGE="${MPBOOT_PG_IMAGE:-postgres:11}"
POSTGRES_PORT="${MPBOOT_DB_PORT:-5433}"
POSTGRES_DB="${MPBOOT_DB_NAME:-postgres}"
POSTGRES_USER="${MPBOOT_DB_USER:-postgres}"
POSTGRES_PASSWORD="${MPBOOT_DB_PASSWORD:-postgres}"

ONTOP_VERSION="${MPBOOT_ONTOP_VERSION:-5.4.0}"
JDBC_VERSION="${MPBOOT_PGJDBC_VERSION:-42.7.7}"

RODI_REPO_URL="${MPBOOT_RODI_REPO_URL:-https://github.com/chrpin/rodi.git}"

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

ensure_repo_root() {
  cd "${ROOT_DIR}"
}

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

clone_fresh_repo() {
  local repo_url target_dir
  repo_url="$1"
  target_dir="$2"

  rm -rf "${target_dir}"
  echo "Cloning ${repo_url} into ${target_dir}"
  git clone --depth 1 "${repo_url}" "${target_dir}"
}

build_with_maven() {
  local workdir
  workdir="$1"

  if command -v mvn >/dev/null 2>&1; then
    (
      cd "${workdir}"
      mvn -q -DskipTests package dependency:copy-dependencies
    )
    return 0
  fi

  require_command docker
  docker run --rm \
    -v "${workdir}:/work" \
    -w /work \
    maven:3.9.9-eclipse-temurin-17 \
    mvn -q -DskipTests package dependency:copy-dependencies
}
