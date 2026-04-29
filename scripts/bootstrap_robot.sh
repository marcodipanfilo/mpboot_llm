#!/usr/bin/env bash

set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_bootstrap_common.sh"

require_command java
require_command curl
mkdir -p "${ROBOT_DIR}"

echo "Installing ROBOT in ${ROBOT_DIR}"
curl -LsSf -o "${ROBOT_JAR}" "https://github.com/ontodev/robot/releases/latest/download/robot.jar"
curl -LsSf -o "${ROBOT_BIN}" "https://raw.githubusercontent.com/ontodev/robot/master/bin/robot"
chmod u+x "${ROBOT_BIN}"

if ! "${ROBOT_BIN}" --version >/dev/null 2>&1; then
  echo "ROBOT installation failed." >&2
  exit 1
fi

