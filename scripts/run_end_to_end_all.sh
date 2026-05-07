#!/usr/bin/env bash

set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_bootstrap_common.sh"

EVAL_METHOD="all"
SKIP_EVALUATION=0
SKIP_SUMMARY=0

CACHE_SERVER_PID=""
CACHE_SERVER_REUSED=0

usage() {
  cat <<EOF
Usage: bash scripts/run_end_to_end_all.sh [options]

Runs the full batch workflow:
  1. bootstrap missing tools
  2. download the selected RODI datasets if needed
  3. prepare mondial_rel dump and qpair files
  4. build the PostgreSQL-compatible dataset tree
  5. generate OWL/XML files where needed
  6. start the Anthropic cache server and run mapping generation
  7. stop the cache server
  8. run batch evaluation
  9. regenerate the shared summary webpages

Options:
  --method {all,rodi}        Evaluation method to run (default: all)
  --skip-evaluation          Stop after archived mapping generation
  --skip-summary             Do not regenerate the summary webpages
EOF
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --method)
        if [[ $# -lt 2 ]]; then
          echo "Missing value for --method" >&2
          exit 1
        fi
        EVAL_METHOD="$2"
        shift 2
        ;;
      --skip-evaluation)
        SKIP_EVALUATION=1
        shift
        ;;
      --skip-summary)
        SKIP_SUMMARY=1
        shift
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

  case "${EVAL_METHOD}" in
    all|rodi) ;;
    *)
      echo "Invalid --method: ${EVAL_METHOD}" >&2
      exit 1
      ;;
  esac
}

cleanup() {
  if [[ -n "${CACHE_SERVER_PID}" && "${CACHE_SERVER_REUSED}" -eq 0 ]]; then
    kill "${CACHE_SERVER_PID}" >/dev/null 2>&1 || true
    wait "${CACHE_SERVER_PID}" 2>/dev/null || true
  fi
}

ensure_bootstrap() {
  local need_bootstrap=0

  if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    need_bootstrap=1
  fi
  if [[ ! -x "${ROBOT_BIN}" ]]; then
    need_bootstrap=1
  fi
  if [[ ! -d "${RODI_DIR}" ]]; then
    need_bootstrap=1
  fi
  if [[ ! -x "${PSQL_WRAPPER}" ]]; then
    need_bootstrap=1
  fi

  if [[ "${need_bootstrap}" -eq 1 ]]; then
    echo "Bootstrapping toolchain"
    bash "${ROOT_DIR}/scripts/bootstrap.sh"
  fi
}

ensure_datasets_downloaded() {
  local dataset
  for dataset in "${RODI_DATASETS[@]}"; do
    if [[ ! -d "${DATASETS_DIR}/${dataset}" ]]; then
      echo "Downloading selected RODI datasets"
      bash "${ROOT_DIR}/scripts/bootstrap.sh" --download-rodi
      return 0
    fi
  done
}

ensure_mondial_prepared() {
  if [[ -d "${DATASETS_DIR}/mondial_rel" ]]; then
    echo "Preparing mondial_rel dump"
    bash "${ROOT_DIR}/scripts/bootstrap_prepare_rodi_dumps.sh"
  fi
}

cache_health_url() {
  printf 'http://%s:%s/healthz' "${ANTHROPIC_MOCK_HOST:-127.0.0.1}" "${ANTHROPIC_MOCK_PORT:-8000}"
}

cache_messages_url() {
  printf 'http://%s:%s/v1/messages' "${ANTHROPIC_MOCK_HOST:-127.0.0.1}" "${ANTHROPIC_MOCK_PORT:-8000}"
}

start_cache_server() {
  local health_url log_dir log_file
  health_url="$(cache_health_url)"

  if curl -fsS "${health_url}" >/dev/null 2>&1; then
    CACHE_SERVER_REUSED=1
    export ANTHROPIC_PROXY_URL
    ANTHROPIC_PROXY_URL="$(cache_messages_url)"
    echo "Reusing existing cache server at ${ANTHROPIC_PROXY_URL}"
    return 0
  fi

  log_dir="${ROOT_DIR}/.tools/logs"
  mkdir -p "${log_dir}"
  log_file="${log_dir}/anthropic_mock_server_all.log"

  export ANTHROPIC_PROXY_URL
  ANTHROPIC_PROXY_URL="$(cache_messages_url)"

  echo "Starting cache server at ${ANTHROPIC_PROXY_URL}"
  bash "${ROOT_DIR}/scripts/start_anthropic_mock_server.sh" \
    --host "${ANTHROPIC_MOCK_HOST:-127.0.0.1}" \
    --port "${ANTHROPIC_MOCK_PORT:-8000}" \
    --mode "${ANTHROPIC_MOCK_MODE:-cache-first}" \
    --db "${ANTHROPIC_MOCK_DB:-anthropic_mock_server/cache.sqlite3}" \
    --real-base-url "${ANTHROPIC_REAL_BASE_URL:-https://api.anthropic.com}" \
    --log-level "${ANTHROPIC_MOCK_LOG_LEVEL:-INFO}" \
    >"${log_file}" 2>&1 &

  CACHE_SERVER_PID="$!"

  for _ in $(seq 1 60); do
    if curl -fsS "${health_url}" >/dev/null 2>&1; then
      echo "Cache server ready"
      return 0
    fi
    sleep 1
  done

  echo "Cache server failed to start; see ${log_file}" >&2
  return 1
}

pg_dataset_root() {
  printf '%s\n' "${ROOT_DIR}/pg_compatible/outputs/data_pg_compatible"
}

build_pg_compatible_datasets() {
  echo "Building PostgreSQL-compatible dataset tree"
  bash "${ROOT_DIR}/scripts/create_pg_compatible_dataset.sh" "${DATASETS_DIR}"
}

generate_owlxml() {
  echo "Generating OWL/XML files"
  bash "${ROOT_DIR}/scripts/generate_owlxml_ontologies.sh" "$(pg_dataset_root)"
}

selected_model_name_for_run() {
  "${VENV_DIR}/bin/python" -c 'import sys; sys.path.insert(0, "src"); from runners.common import selected_model_name; print(selected_model_name())'
}

latest_batch_dir() {
  local model_dir
  model_dir="${ROOT_DIR}/outputs/$1"
  if [[ ! -d "${model_dir}" ]]; then
    return 1
  fi
  find "${model_dir}" -mindepth 1 -maxdepth 1 -type d | sort | tail -n 1
}

run_mapping() {
  echo "Running mapping pipeline for all datasets"
  bash "${ROOT_DIR}/scripts/create_all_mapping.sh" "$(pg_dataset_root)" --keep-going
}

run_evaluation() {
  local batch_dir="$1"
  echo "Running ${EVAL_METHOD} evaluation for all datasets"
  bash "${ROOT_DIR}/scripts/evaluation.sh" "${batch_dir}" --method "${EVAL_METHOD}" --keep-going
}

run_summary_generation() {
  local batch_dir="$1"
  echo "Regenerating summary webpages"
  bash "${ROOT_DIR}/scripts/generate_summary_portal.sh" "${batch_dir}"
}

main() {
  local model_name batch_dir

  parse_args "$@"
  ensure_repo_root
  trap cleanup EXIT

  ensure_bootstrap
  ensure_datasets_downloaded
  ensure_mondial_prepared
  build_pg_compatible_datasets
  generate_owlxml

  model_name="$(selected_model_name_for_run)"
  start_cache_server
  run_mapping
  cleanup
  CACHE_SERVER_PID=""

  batch_dir="$(latest_batch_dir "${model_name}")"
  if [[ -z "${batch_dir}" || ! -d "${batch_dir}" ]]; then
    echo "Could not resolve the latest batch output directory" >&2
    exit 1
  fi

  if [[ "${SKIP_EVALUATION}" -eq 0 ]]; then
    run_evaluation "${batch_dir}"
  fi

  if [[ "${SKIP_SUMMARY}" -eq 0 ]]; then
    run_summary_generation "${batch_dir}"
  fi

  cat <<EOF

Done.

Batch:
  ${batch_dir}

Shared summary portal:
  ${ROOT_DIR}/outputs/summary/index.html
EOF
}

main "$@"
