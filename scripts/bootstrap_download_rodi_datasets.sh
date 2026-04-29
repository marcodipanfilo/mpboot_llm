#!/usr/bin/env bash

set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_bootstrap_common.sh"

require_command git

tmp_dir="$(mktemp -d)"
rm -rf "${DATASETS_DIR}"
mkdir -p "${DATASETS_DIR}"

echo "Downloading selected RODI datasets into ${DATASETS_DIR}"
git clone --depth 1 --filter=blob:none --sparse "${RODI_REPO_URL}" "${tmp_dir}/rodi"
(
  cd "${tmp_dir}/rodi"
  git sparse-checkout set "${RODI_DATASETS[@]/#/data/}"
)

for dataset in "${RODI_DATASETS[@]}"; do
  rm -rf "${DATASETS_DIR:?}/${dataset}"
  cp -R "${tmp_dir}/rodi/data/${dataset}" "${DATASETS_DIR}/${dataset}"
done

rm -rf "${tmp_dir}"

