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

CURRENT_SCHEMA="$("${VENV_DIR}/bin/python" - <<'PY' "${MONDIAL_DUMP}"
from pathlib import Path
import re
import sys

dump_path = Path(sys.argv[1])
text = dump_path.read_text(encoding="utf-8", errors="replace")
schemas = []
for line in text.splitlines():
    if "SET search_path =" not in line:
        continue
    match = re.search(r"SET search_path = ([^,\s;]+)", line)
    if not match:
        continue
    schema = match.group(1).strip('"')
    if schema not in schemas:
        schemas.append(schema)

if "mondial_rdf2sql_standard" in schemas:
    print("mondial_rdf2sql_standard")
elif "mondial_rel" in schemas:
    print("mondial_rel")
PY
)"

if [[ "${CURRENT_SCHEMA}" == "mondial_rel" ]]; then
  echo "mondial_rel dump already prepared; skipping schema rewrite"
  exit 0
fi

echo "Preparing mondial_rel dump: keep only schema mondial_rdf2sql_standard"
"${VENV_DIR}/bin/python" "${ROOT_DIR}/src/parsers/dump_split.py" \
  "${MONDIAL_DUMP}" \
  --keep-schema mondial_rdf2sql_standard

echo "Rewriting kept mondial_rel dump schema from mondial_rdf2sql_standard to mondial_rel"
python3 - <<'PY' "${MONDIAL_DUMP}"
from pathlib import Path
import sys

dump_path = Path(sys.argv[1])
text = dump_path.read_text(encoding="utf-8")
text = text.replace("mondial_rdf2sql_standard", "mondial_rel")
dump_path.write_text(text, encoding="utf-8")
PY
