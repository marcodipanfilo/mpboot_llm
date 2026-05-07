#!/usr/bin/env bash

set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

ensure_repo_root

bash "${ROOT_DIR}/scripts/generate_rodi_f1_site.sh" "$@"
bash "${ROOT_DIR}/scripts/generate_summary_table_site.sh" "$@"
run_python src/evaluation/generate_summary_portal.py "$@"
