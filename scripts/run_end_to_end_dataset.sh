#!/usr/bin/env bash

set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_bootstrap_common.sh"

DATASET=""
EVAL_METHOD="all"
SKIP_EVALUATION=0
SKIP_SUMMARY=0

CACHE_SERVER_PID=""
CACHE_SERVER_REUSED=0

usage() {
  cat <<EOF
Usage: bash scripts/run_end_to_end_dataset.sh <dataset_name> [options]

Runs the full dataset-scoped workflow:
  1. bootstrap missing tools
  2. download the selected RODI dataset if needed
  3. build the PostgreSQL-compatible dataset copy
  4. generate OWL/XML if needed
  5. start the Anthropic cache server and run mapping generation
  6. stop the cache server
  7. run evaluation
  8. regenerate the shared summary webpages

Options:
  --method {all,rodi}        Evaluation method to run (default: all)
  --skip-evaluation          Stop after archived mapping generation
  --skip-summary             Do not regenerate the summary webpages
EOF
}

parse_args() {
  if [[ $# -lt 1 ]]; then
    usage >&2
    exit 1
  fi

  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
  esac

  DATASET="$1"
  shift

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

ensure_dataset_downloaded() {
  if [[ -d "${DATASETS_DIR}/${DATASET}" ]]; then
    return 0
  fi

  echo "Downloading dataset ${DATASET}"
  bash "${ROOT_DIR}/scripts/bootstrap.sh" --download-rodi --dataset "${DATASET}"
}

ensure_mondial_prepared() {
  if [[ "${DATASET}" == "mondial_rel" && -d "${DATASETS_DIR}/mondial_rel" ]]; then
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
  log_file="${log_dir}/anthropic_mock_server_${DATASET}.log"

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

pg_dataset_dir() {
  printf '%s/%s\n' "$(pg_dataset_root)" "${DATASET}"
}

build_pg_compatible_dataset() {
  local target_dir
  target_dir="$(pg_dataset_dir)"
  rm -rf "${target_dir}"
  echo "Building PostgreSQL-compatible dataset at ${target_dir}"
  bash "${ROOT_DIR}/scripts/create_pg_compatible_dataset.sh" "${DATASETS_DIR}/${DATASET}" "${target_dir}"
}

generate_owlxml() {
  echo "Generating OWL/XML for ${DATASET}"
  bash "${ROOT_DIR}/scripts/generate_owlxml_ontologies.sh" "$(pg_dataset_root)" --dataset "${DATASET}" --overwrite
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
  echo "Running mapping pipeline for ${DATASET}"
  bash "${ROOT_DIR}/scripts/create_all_mapping.sh" "$(pg_dataset_root)" --dataset "${DATASET}"
}

run_evaluation() {
  local batch_dir="$1"
  echo "Running ${EVAL_METHOD} evaluation for ${DATASET}"
  bash "${ROOT_DIR}/scripts/evaluation.sh" "${batch_dir}" --dataset "${DATASET}" --method "${EVAL_METHOD}"
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
  ensure_dataset_downloaded
  ensure_mondial_prepared
  build_pg_compatible_dataset
  generate_owlxml

  model_name="$(selected_model_name_for_run)"
  start_cache_server
  run_mapping
  cleanup
  CACHE_SERVER_PID=""

  batch_dir="$(latest_batch_dir "${model_name}")"
  if [[ -z "${batch_dir}" || ! -f "${batch_dir}/${DATASET}/run_metadata.json" ]]; then
    echo "Could not resolve the latest batch output for dataset ${DATASET}" >&2
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

Dataset: ${DATASET}
Batch  : ${batch_dir}

Archived dataset output:
  ${batch_dir}/${DATASET}

Shared summary portal:
  ${ROOT_DIR}/outputs/summary/index.html
EOF
}

main "$@"
