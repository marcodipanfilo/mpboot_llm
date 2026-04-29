#!/usr/bin/env bash

set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_bootstrap_common.sh"

DOWNLOAD_RODI=0

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

run_step() {
  local script_name
  script_name="$1"
  echo
  echo "==> ${script_name}"
  bash "${ROOT_DIR}/scripts/${script_name}"
}

main() {
  parse_args "$@"

  run_step "bootstrap_python_env.sh"
  run_step "bootstrap_robot.sh"
  run_step "bootstrap_ontop.sh"
  run_step "bootstrap_jdbc.sh"
  run_step "bootstrap_rodi.sh"
  if [[ "${DOWNLOAD_RODI}" -eq 1 ]]; then
    run_step "bootstrap_download_rodi_datasets.sh"
  fi
  run_step "bootstrap_postgres.sh"
  run_step "bootstrap_psql_wrapper.sh"

  cat <<EOF

Bootstrap complete.

Virtual environment:
  ${VENV_DIR}

Activate it with:
  source "${VENV_DIR}/bin/activate"

Repo-local tools:
  ROBOT  : ${ROBOT_BIN}
  RODI   : ${RODI_DIR}
  Ontop  : ${ONTOP_DIR}
  JDBC   : ${ONTOP_DIR}/jdbc/postgresql-${JDBC_VERSION}.jar
  psql   : ${PSQL_WRAPPER}

Docker PostgreSQL:
  Container : ${POSTGRES_CONTAINER}
  Host      : localhost
  Port      : ${POSTGRES_PORT}
  Database  : ${POSTGRES_DB}
  User      : ${POSTGRES_USER}

Selected RODI datasets destination:
  ${DATASETS_DIR}

Example commands:
  bash scripts/bootstrap.sh --download-rodi
  bash scripts/create_pg_compatible_dataset.sh datasets/rodi
  bash scripts/generate_owlxml_ontologies.sh pg_compatible/outputs/data_pg_compatible
  bash scripts/create_all_mapping.sh pg_compatible/outputs/data_pg_compatible --dry-run
  bash scripts/evaluation.sh outputs/<model>/<batch_timestamp> --dataset mondial_rel --method all
EOF
}

main "$@"
