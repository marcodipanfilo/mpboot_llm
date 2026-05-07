#!/usr/bin/env bash

set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_bootstrap_common.sh"

DOWNLOAD_RODI=0
SELECTED_DATASETS=()
SELECTED_DATASET_COUNT=0

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --download-rodi)
        DOWNLOAD_RODI=1
        shift
        ;;
      --dataset)
        if [[ $# -lt 2 ]]; then
          echo "Missing value for --dataset" >&2
          exit 1
        fi
        SELECTED_DATASETS+=("$2")
        SELECTED_DATASET_COUNT=$((SELECTED_DATASET_COUNT + 1))
        shift 2
        ;;
      *)
        echo "Unknown option: $1" >&2
        echo "Usage: bash scripts/bootstrap.sh [--download-rodi] [--dataset <name>]..." >&2
        exit 1
        ;;
    esac
  done

  if [[ "${SELECTED_DATASET_COUNT}" -gt 0 && "${DOWNLOAD_RODI}" -ne 1 ]]; then
    echo "--dataset can only be used together with --download-rodi" >&2
    exit 1
  fi
}

main() {
  parse_args "$@"
  ensure_repo_root

  bash "${ROOT_DIR}/scripts/bootstrap_python_env.sh"
  bash "${ROOT_DIR}/scripts/bootstrap_robot.sh"
  bash "${ROOT_DIR}/scripts/bootstrap_ontop.sh"
  bash "${ROOT_DIR}/scripts/bootstrap_jdbc.sh"
  bash "${ROOT_DIR}/scripts/bootstrap_rodi.sh"
  bash "${ROOT_DIR}/scripts/bootstrap_postgres.sh"
  bash "${ROOT_DIR}/scripts/bootstrap_psql_wrapper.sh"

  if [[ "${DOWNLOAD_RODI}" -eq 1 ]]; then
    if [[ "${SELECTED_DATASET_COUNT}" -gt 0 ]]; then
      dataset_args=()
      for dataset in "${SELECTED_DATASETS[@]}"; do
        dataset_args+=("--dataset" "${dataset}")
      done
      bash "${ROOT_DIR}/scripts/bootstrap_download_rodi_datasets.sh" "${dataset_args[@]}"
    else
      bash "${ROOT_DIR}/scripts/bootstrap_download_rodi_datasets.sh"
    fi
  fi

  if [[ -d "${DATASETS_DIR}/mondial_rel" ]]; then
    bash "${ROOT_DIR}/scripts/bootstrap_prepare_rodi_dumps.sh"
  fi

  cat <<EOF

Bootstrap complete.

Virtual environment:
  ${VENV_DIR}

Activate it with:
  source "${VENV_DIR}/bin/activate"

Installed tools:
  ROBOT  : ${ROBOT_BIN}
  Ontop  : ${ONTOP_DIR}/ontop
  RODI   : ${RODI_DIR}
  psql   : ${PSQL_WRAPPER}

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
