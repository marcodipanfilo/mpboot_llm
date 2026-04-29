#!/usr/bin/env bash

set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_bootstrap_common.sh"

require_command curl
mkdir -p "${ONTOP_DIR}/jdbc"

echo "Installing PostgreSQL JDBC driver ${JDBC_VERSION}"
jdbc_jar="postgresql-${JDBC_VERSION}.jar"
curl -LsSf -o "${ONTOP_DIR}/jdbc/${jdbc_jar}" \
  "https://repo1.maven.org/maven2/org/postgresql/postgresql/${JDBC_VERSION}/${jdbc_jar}"
