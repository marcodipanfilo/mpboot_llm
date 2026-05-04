#!/usr/bin/env bash

set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_bootstrap_common.sh"

require_command python3
require_command curl
install_uv

echo "Creating virtual environment in ${VENV_DIR}"
uv venv --clear "${VENV_DIR}"

echo "Installing project dependencies"
uv pip install --python "${VENV_DIR}/bin/python" -r "${ROOT_DIR}/requirements.txt" requests psycopg2-binary
