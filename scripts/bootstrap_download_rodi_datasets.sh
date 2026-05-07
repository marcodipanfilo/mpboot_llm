#!/usr/bin/env bash

set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_bootstrap_common.sh"

require_command git

SELECTED_DATASETS=()
SELECTED_DATASET_COUNT=0
DATASET_LIST=()

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
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
        echo "Usage: bash scripts/bootstrap_download_rodi_datasets.sh [--dataset <name>]..." >&2
        exit 1
        ;;
    esac
  done
}

validate_selected_datasets() {
  local dataset known
  if [[ "${SELECTED_DATASET_COUNT}" -eq 0 ]]; then
    return 0
  fi
  for dataset in "${SELECTED_DATASETS[@]-}"; do
    known=0
    for allowed in "${RODI_DATASETS[@]}"; do
      if [[ "${dataset}" == "${allowed}" ]]; then
        known=1
        break
      fi
    done
    if [[ "${known}" -ne 1 ]]; then
      echo "Unsupported dataset: ${dataset}" >&2
      echo "Allowed datasets: ${RODI_DATASETS[*]}" >&2
      exit 1
    fi
  done
}

selected_downloads() {
  if [[ "${SELECTED_DATASET_COUNT}" -gt 0 ]]; then
    DATASET_LIST=("${SELECTED_DATASETS[@]-}")
  else
    DATASET_LIST=("${RODI_DATASETS[@]}")
  fi
}

parse_args "$@"
validate_selected_datasets
selected_downloads

if [[ "${SELECTED_DATASET_COUNT}" -eq 0 ]]; then
  rm -rf "${DATASETS_DIR}"
fi
mkdir -p "${DATASETS_DIR}"

echo "Downloading selected RODI datasets into ${DATASETS_DIR}"

source_rodi_dir=""
tmp_dir=""

if [[ -d "${RODI_DIR}/data" ]]; then
  echo "Reusing datasets from existing local RODI checkout at ${RODI_DIR}"
  source_rodi_dir="${RODI_DIR}"
else
  tmp_dir="$(mktemp -d)"
  echo "Local RODI checkout not found under ${RODI_DIR}; cloning a temporary copy for dataset download"
  git clone --depth 1 --filter=blob:none "${RODI_REPO_URL}" "${tmp_dir}/rodi"
  (
    cd "${tmp_dir}/rodi"
    git sparse-checkout init --cone
    git sparse-checkout set "${DATASET_LIST[@]/#/data/}"
  )
  source_rodi_dir="${tmp_dir}/rodi"
fi

for dataset in "${DATASET_LIST[@]}"; do
  rm -rf "${DATASETS_DIR:?}/${dataset}"
  if [[ ! -d "${source_rodi_dir}/data/${dataset}" ]]; then
    echo "Missing dataset ${dataset} under ${source_rodi_dir}/data" >&2
    exit 1
  fi
  cp -R "${source_rodi_dir}/data/${dataset}" "${DATASETS_DIR}/${dataset}"
done

python3 "${ROOT_DIR}/src/evaluation/normalize_rodi_qpair_names.py" "${DATASETS_DIR}"

if [[ -n "${tmp_dir}" ]]; then
  rm -rf "${tmp_dir}"
fi
