#!/usr/bin/env bash

set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_bootstrap_common.sh"

require_command java
require_command curl
require_command unzip

tmp_dir="$(mktemp -d)"
zip_file="${tmp_dir}/ontop.zip"
extract_dir="${tmp_dir}/extract"

echo "Installing Ontop CLI ${ONTOP_VERSION} in ${ONTOP_DIR}"
rm -rf "${ONTOP_DIR}"
mkdir -p "${TOOLS_DIR}" "${extract_dir}" "${ONTOP_DIR}"

curl -LsSf -o "${zip_file}" \
  "https://github.com/ontop/ontop/releases/download/ontop-${ONTOP_VERSION}/ontop-cli-${ONTOP_VERSION}.zip"
unzip -q "${zip_file}" -d "${extract_dir}"

ontop_bin="$(find "${extract_dir}" -type f -name 'ontop' | head -n 1)"
if [[ -z "${ontop_bin}" ]]; then
  echo "Failed to unpack Ontop CLI archive: no 'ontop' executable found." >&2
  exit 1
fi

extracted_dir="$(dirname "${ontop_bin}")"
if [[ "$(basename "${extracted_dir}")" == "bin" ]]; then
  extracted_dir="$(cd "${extracted_dir}/.." && pwd)"
fi

cp -R "${extracted_dir}/." "${ONTOP_DIR}/"
chmod u+x "${ONTOP_DIR}/ontop"
rm -rf "${tmp_dir}"
