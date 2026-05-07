#!/usr/bin/env bash

set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_bootstrap_common.sh"

require_command docker

remove_existing_container() {
  if docker container inspect "${POSTGRES_CONTAINER}" >/dev/null 2>&1; then
    echo "Removing existing PostgreSQL container ${POSTGRES_CONTAINER}"
    docker rm -f "${POSTGRES_CONTAINER}" >/dev/null
  fi
}

wait_for_postgres() {
  echo "Waiting for PostgreSQL to accept connections..."
  until docker exec "${POSTGRES_CONTAINER}" pg_isready -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" >/dev/null 2>&1; do
    sleep 1
  done
}

main() {
  if ! docker image inspect "${POSTGRES_IMAGE}" >/dev/null 2>&1; then
    echo "Pulling ${POSTGRES_IMAGE}"
    docker pull "${POSTGRES_IMAGE}"
  fi

  remove_existing_container

  echo "Creating PostgreSQL container ${POSTGRES_CONTAINER}"
  docker run -d \
    --name "${POSTGRES_CONTAINER}" \
    -e POSTGRES_USER="${POSTGRES_USER}" \
    -e POSTGRES_PASSWORD="${POSTGRES_PASSWORD}" \
    -e POSTGRES_DB="${POSTGRES_DB}" \
    -p "${POSTGRES_PORT}:5432" \
    "${POSTGRES_IMAGE}" >/dev/null

  wait_for_postgres

  echo "Ensuring database ${POSTGRES_DB} exists in ${POSTGRES_CONTAINER}"
  docker exec -e PGPASSWORD="${POSTGRES_PASSWORD}" "${POSTGRES_CONTAINER}" \
    psql -U "${POSTGRES_USER}" -d postgres -tAc \
    "SELECT 1 FROM pg_database WHERE datname = '${POSTGRES_DB}'" | grep -q 1 || \
  docker exec -e PGPASSWORD="${POSTGRES_PASSWORD}" "${POSTGRES_CONTAINER}" \
    psql -U "${POSTGRES_USER}" -d postgres -v ON_ERROR_STOP=1 -c "CREATE DATABASE \"${POSTGRES_DB}\""
}

main
