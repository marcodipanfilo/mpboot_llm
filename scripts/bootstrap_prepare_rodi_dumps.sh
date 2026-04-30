#!/usr/bin/env bash

set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_bootstrap_common.sh"

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
  echo "Virtual environment not found at ${VENV_DIR}." >&2
  echo "Run scripts/bootstrap.sh first." >&2
  exit 1
fi

MONDIAL_DUMP="${DATASETS_DIR}/mondial_rel/dump.sql"

if [[ ! -f "${MONDIAL_DUMP}" ]]; then
  echo "SKIP mondial_rel dump preparation: ${MONDIAL_DUMP} not found"
  exit 0
fi

echo "Preparing mondial_rel dump: keep only schema mondial_rdf2sql_standard"
"${VENV_DIR}/bin/python" "${ROOT_DIR}/src/parsers/dump_split.py" \
  "${MONDIAL_DUMP}" \
  --keep-schema mondial_rdf2sql_standard
