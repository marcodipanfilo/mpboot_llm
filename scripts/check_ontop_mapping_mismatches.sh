#!/usr/bin/env bash

set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

run_python src/evaluation/check_ontop_mapping_mismatches.py "$@"
