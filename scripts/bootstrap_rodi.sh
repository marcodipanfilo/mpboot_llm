#!/usr/bin/env bash

set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_bootstrap_common.sh"

require_command git
require_command java

clone_fresh_repo "${RODI_REPO_URL}" "${RODI_DIR}"
echo "Building RODI in ${RODI_DIR}"
build_with_maven "${RODI_DIR}"

mkdir -p "${RODI_DIR}/lib"
if [[ -f "${ONTOP_DIR}/jdbc/postgresql-${JDBC_VERSION}.jar" ]]; then
  cp "${ONTOP_DIR}/jdbc/postgresql-${JDBC_VERSION}.jar" "${RODI_DIR}/lib/"
fi
