#!/usr/bin/env bash

set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_bootstrap_common.sh"

HOST="${ANTHROPIC_MOCK_HOST:-127.0.0.1}"
PORT="${ANTHROPIC_MOCK_PORT:-8000}"
MODE="${ANTHROPIC_MOCK_MODE:-cache-first}"
DB_PATH="${ANTHROPIC_MOCK_DB:-anthropic_mock_server/cache.sqlite3}"
REAL_BASE_URL="${ANTHROPIC_REAL_BASE_URL:-https://api.anthropic.com}"
LOG_LEVEL="${ANTHROPIC_MOCK_LOG_LEVEL:-INFO}"

usage() {
  cat <<EOF
Usage: bash scripts/start_anthropic_mock_server.sh [options]

Options:
  --host <host>           Bind host (default: ${HOST})
  --port <port>           Bind port (default: ${PORT})
  --mode <mode>           cache-first | record | replay | mock-only | passthrough
  --db <path>             SQLite cache path (default: ${DB_PATH})
  --real-base-url <url>   Upstream Anthropic base URL (default: ${REAL_BASE_URL})
  --log-level <level>     DEBUG | INFO | WARNING | ERROR (default: ${LOG_LEVEL})
EOF
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --host)
        HOST="$2"
        shift 2
        ;;
      --port)
        PORT="$2"
        shift 2
        ;;
      --mode)
        MODE="$2"
        shift 2
        ;;
      --db)
        DB_PATH="$2"
        shift 2
        ;;
      --real-base-url)
        REAL_BASE_URL="$2"
        shift 2
        ;;
      --log-level)
        LOG_LEVEL="$2"
        shift 2
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        echo "Unknown option: $1" >&2
        usage >&2
        exit 1
        ;;
    esac
  done
}

main() {
  parse_args "$@"
  ensure_repo_root
  ensure_venv

  exec "${VENV_DIR}/bin/python" "${ROOT_DIR}/anthropic_mock_server/server.py" \
    --host "${HOST}" \
    --port "${PORT}" \
    --mode "${MODE}" \
    --db "${DB_PATH}" \
    --real-base-url "${REAL_BASE_URL}" \
    --log-level "${LOG_LEVEL}"
}

main "$@"
