from __future__ import annotations

import os
import subprocess
from pathlib import Path

from evaluation.common import EvaluationRunConfig


def ensure_postgres_container_running() -> None:
    container_name = os.environ.get("MPBOOT_PG_CONTAINER", "mpboot-postgres")
    inspect = subprocess.run(
        ["docker", "container", "inspect", container_name],
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if inspect.returncode != 0:
        raise RuntimeError(
            f"PostgreSQL container '{container_name}' not found. Run scripts/bootstrap_postgres.sh first."
        )

    running = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", container_name],
        text=True,
        capture_output=True,
        check=True,
    )
    if running.stdout.strip() != "true":
        print(f"[DB] Starting PostgreSQL container {container_name} ...")
        subprocess.run(["docker", "start", container_name], check=True, stdout=subprocess.DEVNULL)

    print("[DB] Waiting for PostgreSQL container readiness...")
    while True:
        ready = subprocess.run(
            [
                "docker",
                "exec",
                container_name,
                "pg_isready",
                "-U",
                os.environ.get("MPBOOT_DB_USER", "postgres"),
                "-d",
                "postgres",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if ready.returncode == 0:
            break


def _psql_base_cmd(cfg: EvaluationRunConfig, *, db_name: str) -> list[str]:
    return [
        str(cfg.db_cmd),
        "-v",
        "ON_ERROR_STOP=1",
        "-h",
        cfg.db_host,
        "-p",
        str(cfg.db_port),
        "-U",
        cfg.db_user,
        "-d",
        db_name,
    ]


def _run_psql(cfg: EvaluationRunConfig, *, db_name: str, sql: str | None = None, file_path: Path | None = None) -> None:
    cmd = _psql_base_cmd(cfg, db_name=db_name)
    if sql is not None:
        cmd.extend(["-c", sql])
    elif file_path is not None:
        cmd.extend(["-f", str(file_path)])
    else:
        raise ValueError("Either sql or file_path must be provided.")

    env = os.environ.copy()
    env["PGPASSWORD"] = cfg.db_password
    print("[CMD]", " ".join(cmd))
    result = subprocess.run(cmd, text=True, env=env)
    if result.returncode != 0:
        raise RuntimeError(f"psql command failed with exit code {result.returncode}")


def ensure_database_exists(cfg: EvaluationRunConfig) -> None:
    check_cmd = _psql_base_cmd(cfg, db_name="postgres") + [
        "-tAc",
        f"SELECT 1 FROM pg_database WHERE datname = '{cfg.db_name}'",
    ]
    env = os.environ.copy()
    env["PGPASSWORD"] = cfg.db_password
    result = subprocess.run(check_cmd, text=True, capture_output=True, env=env)
    if result.returncode != 0:
        raise RuntimeError(f"Failed to check database existence: {' '.join(check_cmd)}")

    if result.stdout.strip() == "1":
        return

    _run_psql(cfg, db_name="postgres", sql=f'CREATE DATABASE "{cfg.db_name}"')


def database_exists(cfg: EvaluationRunConfig) -> bool:
    check_cmd = _psql_base_cmd(cfg, db_name="postgres") + [
        "-tAc",
        f"SELECT 1 FROM pg_database WHERE datname = '{cfg.db_name}'",
    ]
    env = os.environ.copy()
    env["PGPASSWORD"] = cfg.db_password
    result = subprocess.run(check_cmd, text=True, capture_output=True, env=env)
    if result.returncode != 0:
        raise RuntimeError(f"Failed to check database existence: {' '.join(check_cmd)}")
    return result.stdout.strip() == "1"


def drop_dataset_schema(cfg: EvaluationRunConfig) -> None:
    _run_psql(cfg, db_name=cfg.db_name, sql=f'DROP SCHEMA IF EXISTS "{cfg.dataset_name}" CASCADE')


def load_dataset_dump(cfg: EvaluationRunConfig) -> None:
    if not cfg.dump_file.exists():
        raise FileNotFoundError(f"Dataset dump not found: {cfg.dump_file}")
    _run_psql(cfg, db_name=cfg.db_name, file_path=cfg.dump_file)


def prepare_database_from_dump(cfg: EvaluationRunConfig) -> None:
    ensure_postgres_container_running()
    print("[DB] Ensuring PostgreSQL database exists...")
    ensure_database_exists(cfg)
    print(f"[DB] Dropping existing schema for dataset '{cfg.dataset_name}'...")
    drop_dataset_schema(cfg)
    print(f"[DB] Loading dump from {cfg.dump_file} ...")
    load_dataset_dump(cfg)
    print("[DB] Database prepared from archived dump.")


def ensure_dataset_database_ready(cfg: EvaluationRunConfig) -> None:
    ensure_postgres_container_running()
    ensure_database_exists(cfg)
