#!/usr/bin/env bash

set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_bootstrap_common.sh"

require_command git

rm -rf "${DATASETS_DIR}"
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
    git sparse-checkout set "${RODI_DATASETS[@]/#/data/}"
  )
  source_rodi_dir="${tmp_dir}/rodi"
fi

for dataset in "${RODI_DATASETS[@]}"; do
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
