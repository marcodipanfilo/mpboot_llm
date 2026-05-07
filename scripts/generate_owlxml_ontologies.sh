#!/usr/bin/env bash

set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

ensure_robot
run_python src/runners/generate_owlxml_ontologies.py "$@"
