#!/usr/bin/env python3
import argparse
import hashlib
import os
import re
import io
import csv
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from sanitizer import normalize_identifier

SQL_KEY_RE = re.compile(r"^\s*sql\s*=", re.IGNORECASE)
TOP_LEVEL_KEY_RE = re.compile(r"^\s*[A-Za-z][A-Za-z0-9_]*\s*=")

DEFAULT_WORKDIR = Path(__file__).resolve().parent / "outputs" / "regression_artifacts"


@dataclass
class PgConfig:
    host: Optional[str]
    port: Optional[int]
    user: Optional[str]
    password: Optional[str]
    maintenance_db: str = "postgres"


@dataclass
class PgRunner:
    mode: str
    pg: PgConfig
    container: Optional[str] = None

    def _env(self):
        env = os.environ.copy()
        if self.pg.password:
            env["PGPASSWORD"] = self.pg.password
        return env

    def _local_psql_base(self, dbname: str):
        cmd = ["psql", "-X", "-v", "ON_ERROR_STOP=1", "-d", dbname]
        if self.pg.host:
            cmd += ["-h", self.pg.host]
        if self.pg.port:
            cmd += ["-p", str(self.pg.port)]
        if self.pg.user:
            cmd += ["-U", self.pg.user]
        return cmd

    def _docker_psql_base(self, dbname: str):
        if not self.container:
            raise ValueError("Docker mode requires --container")
        cmd = ["docker", "exec", "-i"]
        if self.pg.password:
            cmd += ["-e", f"PGPASSWORD={self.pg.password}"]
        cmd += [self.container, "psql", "-X", "-v", "ON_ERROR_STOP=1", "-d", dbname]
        if self.pg.host:
            cmd += ["-h", self.pg.host]
        if self.pg.port:
            cmd += ["-p", str(self.pg.port)]
        if self.pg.user:
            cmd += ["-U", self.pg.user]
        return cmd

    def psql_base(self, dbname: str):
        if self.mode == "local":
            return self._local_psql_base(dbname)
        if self.mode == "docker":
            return self._docker_psql_base(dbname)
        raise ValueError(f"Unsupported mode: {self.mode}")

    def run_query(self, dbname: str, sql: str, at: bool = False, fieldsep: str = "\t") -> str:
        cmd = self.psql_base(dbname)
        if at:
            cmd += ["-At", "-F", fieldsep]
        cmd += ["-c", sql]

        result = subprocess.run(
            cmd,
            env=self._env(),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Query failed on database '{dbname}'.\n"
                f"SQL:\n{sql}\n\n"
                f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
            )
        return result.stdout

    def run_file(self, dbname: str, sql_file: Path):
        if self.mode == "local":
            cmd = self.psql_base(dbname) + ["-f", str(sql_file)]
            result = subprocess.run(
                cmd,
                env=self._env(),
                capture_output=True,
                text=True,
                check=False,
            )
        else:
            cmd = self.psql_base(dbname)
            with open(sql_file, "rb") as f:
                result = subprocess.run(
                    cmd,
                    env=self._env(),
                    stdin=f,
                    capture_output=True,
                    check=False,
                )

        if result.returncode != 0:
            stdout = result.stdout.decode() if isinstance(result.stdout, bytes) else result.stdout
            stderr = result.stderr.decode() if isinstance(result.stderr, bytes) else result.stderr
            raise RuntimeError(
                f"Failed loading SQL file into database '{dbname}': {sql_file}\n"
                f"STDOUT:\n{stdout}\nSTDERR:\n{stderr}"
            )

    def createdb(self, dbname: str):
        sql = f'CREATE DATABASE "{dbname.replace(chr(34), chr(34) * 2)}";'
        self.run_query(self.pg.maintenance_db, sql)

    def dropdb(self, dbname: str):
        sql = f'DROP DATABASE IF EXISTS "{dbname.replace(chr(34), chr(34) * 2)}";'
        self.run_query(self.pg.maintenance_db, sql)


def has_order_by(sql: str) -> bool:
    """
    Lightweight heuristic: checks whether the SQL contains ORDER BY.
    Good enough for these qpair benchmark queries.
    """
    return re.search(r"\border\s+by\b", sql, flags=re.IGNORECASE) is not None


def parse_csv_rows(text: str):
    text = text.strip()
    if not text:
        return []
    return list(csv.reader(io.StringIO(text)))


def normalize_result_rows(csv_text: str, preserve_order: bool):
    rows = parse_csv_rows(csv_text)
    if not preserve_order:
        rows = sorted(rows)
    return rows


def result_rows_to_text(rows) -> str:
    return "\n".join(",".join(row) for row in rows)

def normalize_schema_signature_text(text: str) -> str:
    lines = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) == 9:
            kind_, table_schema, table_name, ordinal_position, column_name, data_type, udt_name, is_nullable, extra = parts

            table_schema = normalize_identifier(table_schema)
            table_name = normalize_identifier(table_name)
            column_name = normalize_identifier(column_name)

            if kind_ == "FK":
                data_type = normalize_identifier(data_type) if data_type else data_type
                udt_name = normalize_identifier(udt_name) if udt_name else udt_name
                is_nullable = normalize_identifier(is_nullable) if is_nullable else is_nullable

            parts = [
                kind_,
                table_schema,
                table_name,
                ordinal_position,
                column_name,
                data_type,
                udt_name,
                is_nullable,
                extra,
            ]

        lines.append("\t".join(parts))

    lines.sort()
    return "\n".join(lines)


def detect_mode(requested_mode: str, container: Optional[str]) -> str:
    if requested_mode in {"local", "docker"}:
        return requested_mode

    if shutil.which("psql"):
        return "local"

    if container:
        result = subprocess.run(
            ["docker", "inspect", container],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return "docker"

    raise RuntimeError(
        "Could not determine PostgreSQL execution mode automatically. "
        "Either install local psql or pass --mode docker --container <name>."
    )


def decode_qpair_sql(sql: str) -> str:
    """
    Decode qpair-style SQL serialization such as:
      SELECT ... \n\
      FROM ...
    into real SQL with actual newlines.
    """
    sql = sql.replace("\\r\\n\\", "\n")
    sql = sql.replace("\\n\\", "\n")
    sql = sql.replace("\\n", "\n")
    sql = sql.replace("\\t", "\t")
    return sql.strip()


def cleanup_qpair_sql(sql: str) -> str:
    sql = sql.strip()
    if sql.endswith(";"):
        sql = sql[:-1].rstrip()
    return sql


def strip_sql_line_comments(sql: str) -> str:
    """
    Remove -- line comments while preserving quoted strings.
    """
    out = []
    i = 0
    n = len(sql)
    in_single = False
    in_double = False

    while i < n:
        ch = sql[i]

        if ch == "'" and not in_double:
            out.append(ch)
            if in_single:
                if i + 1 < n and sql[i + 1] == "'":
                    out.append(sql[i + 1])
                    i += 2
                    continue
                in_single = False
            else:
                in_single = True
            i += 1
            continue

        if ch == '"' and not in_single:
            out.append(ch)
            if in_double:
                if i + 1 < n and sql[i + 1] == '"':
                    out.append(sql[i + 1])
                    i += 2
                    continue
                in_double = False
            else:
                in_double = True
            i += 1
            continue

        if not in_single and not in_double and ch == "-" and i + 1 < n and sql[i + 1] == "-":
            while i < n and sql[i] != "\n":
                i += 1
            continue

        out.append(ch)
        i += 1

    return "".join(out)


def extract_sql_block_from_qpair(text: str) -> str:
    lines = text.splitlines(keepends=True)

    start = None
    prefix = None
    for i, line in enumerate(lines):
        if SQL_KEY_RE.match(line):
            start = i
            m = re.match(r"^(\s*sql\s*=)", line, re.IGNORECASE)
            if not m:
                raise ValueError("Could not parse sql= prefix")
            prefix = m.group(1)
            break

    if start is None or prefix is None:
        raise ValueError("No sql= block found")

    end = start + 1
    while end < len(lines):
        if TOP_LEVEL_KEY_RE.match(lines[end]):
            break
        end += 1

    block = "".join(lines[start:end])
    raw_sql = block[len(prefix):]

    decoded_sql = decode_qpair_sql(raw_sql)
    decoded_sql = strip_sql_line_comments(decoded_sql)
    return cleanup_qpair_sql(decoded_sql)


def schema_signature_sql() -> str:
    return r"""
WITH cols AS (
    SELECT
        c.table_schema,
        c.table_name,
        c.ordinal_position,
        c.column_name,
        c.data_type,
        c.udt_name,
        c.is_nullable,
        c.column_default
    FROM information_schema.columns c
    WHERE c.table_schema NOT IN ('pg_catalog', 'information_schema')
),
pks AS (
    SELECT
        tc.table_schema,
        tc.table_name,
        kcu.column_name,
        kcu.ordinal_position
    FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage kcu
      ON tc.constraint_name = kcu.constraint_name
     AND tc.table_schema = kcu.table_schema
     AND tc.table_name = kcu.table_name
    WHERE tc.constraint_type = 'PRIMARY KEY'
),
fks AS (
    SELECT
        tc.table_schema,
        tc.table_name,
        kcu.column_name,
        ccu.table_schema AS foreign_table_schema,
        ccu.table_name AS foreign_table_name,
        ccu.column_name AS foreign_column_name
    FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage kcu
      ON tc.constraint_name = kcu.constraint_name
     AND tc.table_schema = kcu.table_schema
     AND tc.table_name = kcu.table_name
    JOIN information_schema.constraint_column_usage ccu
      ON ccu.constraint_name = tc.constraint_name
     AND ccu.table_schema = tc.table_schema
    WHERE tc.constraint_type = 'FOREIGN KEY'
)
SELECT *
FROM (
    SELECT
        'COLUMN' AS kind,
        cols.table_schema,
        cols.table_name,
        cols.ordinal_position::text AS ordinal_position,
        cols.column_name,
        cols.data_type,
        cols.udt_name,
        cols.is_nullable,
        COALESCE(cols.column_default, '') AS extra
    FROM cols

    UNION ALL

    SELECT
        'PK' AS kind,
        pks.table_schema,
        pks.table_name,
        pks.ordinal_position::text AS ordinal_position,
        pks.column_name,
        '' AS data_type,
        '' AS udt_name,
        '' AS is_nullable,
        '' AS extra
    FROM pks

    UNION ALL

    SELECT
        'FK' AS kind,
        fks.table_schema,
        fks.table_name,
        '0' AS ordinal_position,
        fks.column_name,
        fks.foreign_table_schema AS data_type,
        fks.foreign_table_name AS udt_name,
        fks.foreign_column_name AS is_nullable,
        '' AS extra
    FROM fks
) s
ORDER BY
    kind,
    table_schema,
    table_name,
    ordinal_position,
    column_name,
    data_type,
    udt_name,
    is_nullable,
    extra;
"""


def list_dataset_dirs(root: Path) -> list[Path]:
    return sorted([p for p in root.iterdir() if p.is_dir()])


def find_dump_file(dataset_dir: Path) -> Optional[Path]:
    candidates = list(dataset_dir.rglob("dump.sql"))
    if not candidates:
        return None
    if len(candidates) > 1:
        raise RuntimeError(f"Multiple dump.sql files found under {dataset_dir}: {candidates}")
    return candidates[0]


def find_pg_dump_file(dataset_pg_dir: Path) -> Optional[Path]:
    candidates = list(dataset_pg_dir.rglob("dump_pg_compatible.sql"))
    if not candidates:
        return None
    if len(candidates) > 1:
        raise RuntimeError(f"Multiple dump_pg_compatible.sql files found under {dataset_pg_dir}: {candidates}")
    return candidates[0]


def list_qpairs(dataset_dir: Path) -> list[Path]:
    return sorted(dataset_dir.rglob("*.qpair"))


def map_qpair_to_pg(qpair_path: Path, orig_dataset_dir: Path, pg_dataset_dir: Path) -> Path:
    rel = qpair_path.relative_to(orig_dataset_dir)
    return pg_dataset_dir / rel.parent / f"{qpair_path.stem}_pg_compatible.qpair"


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def normalize_result_text(s: str) -> str:
    return s.replace("\r\n", "\n").strip()


def compare_schema(runner: PgRunner, db_orig: str, db_pg: str, dataset_name: str, workdir: Path):
    sql = schema_signature_sql()
    schema_orig_raw = runner.run_query(db_orig, sql, at=True)
    schema_pg_raw = runner.run_query(db_pg, sql, at=True)

    schema_orig = normalize_schema_signature_text(schema_orig_raw)
    schema_pg = normalize_schema_signature_text(schema_pg_raw)

    orig_file = workdir / f"{dataset_name}__schema_original.tsv"
    pg_file = workdir / f"{dataset_name}__schema_pg_compatible.tsv"
    orig_norm_file = workdir / f"{dataset_name}__schema_original_normalized.tsv"
    pg_norm_file = workdir / f"{dataset_name}__schema_pg_compatible_normalized.tsv"

    orig_file.write_text(schema_orig_raw, encoding="utf-8")
    pg_file.write_text(schema_pg_raw, encoding="utf-8")
    orig_norm_file.write_text(schema_orig, encoding="utf-8")
    pg_norm_file.write_text(schema_pg, encoding="utf-8")

    return normalize_result_text(schema_orig) == normalize_result_text(schema_pg), orig_norm_file, pg_norm_file


def quote_sql_literal(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def run_query_as_csv_text(runner: PgRunner, dbname: str, sql: str, dataset_schema: str) -> str:
    search_path = quote_ident(dataset_schema)
    full_sql = (
        f"SET search_path TO {search_path}; "
        "COPY ("
        + sql
        + ") TO STDOUT WITH (FORMAT CSV, HEADER FALSE, DELIMITER ',', QUOTE '\"', ESCAPE '\"', FORCE_QUOTE *);"
    )
    return runner.run_query(dbname, full_sql)

def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'

def is_disabled_qpair(text: str) -> bool:
    for line in text.splitlines():
        if line.strip().lower().startswith("disabled="):
            return True
    return False


def compare_qpair_results(
    runner: PgRunner,
    db_orig: str,
    db_pg: str,
    qpair_orig: Path,
    qpair_pg: Path,
    workdir: Path,
    dataset_schema: str,
):
    orig_text = qpair_orig.read_text(encoding="utf-8")
    pg_text = qpair_pg.read_text(encoding="utf-8")

    # 🔥 Skip disabled qpairs
    if is_disabled_qpair(orig_text) or is_disabled_qpair(pg_text):
        return "skipped_disabled", None

    sql_orig = extract_sql_block_from_qpair(orig_text)
    sql_pg = extract_sql_block_from_qpair(pg_text)

    result_orig = run_query_as_csv_text(runner, db_orig, sql_orig, dataset_schema)
    result_pg = run_query_as_csv_text(runner, db_pg, sql_pg, dataset_schema)

    rel_name = qpair_orig.stem
    out_orig = workdir / f"{rel_name}__original.csv"
    out_pg = workdir / f"{rel_name}__pg_compatible.csv"
    out_orig.write_text(result_orig, encoding="utf-8")
    out_pg.write_text(result_pg, encoding="utf-8")

    preserve_order = has_order_by(sql_orig) or has_order_by(sql_pg)

    norm_orig_rows = normalize_result_rows(result_orig, preserve_order=preserve_order)
    norm_pg_rows = normalize_result_rows(result_pg, preserve_order=preserve_order)

    norm_orig = result_rows_to_text(norm_orig_rows)
    norm_pg = result_rows_to_text(norm_pg_rows)

    norm_orig_file = workdir / f"{rel_name}__original_normalized.csv"
    norm_pg_file = workdir / f"{rel_name}__pg_compatible_normalized.csv"
    norm_orig_file.write_text(norm_orig, encoding="utf-8")
    norm_pg_file.write_text(norm_pg, encoding="utf-8")

    if norm_orig == norm_pg:
        return "ok", None

    msg = (
        f"Result mismatch for:\n"
        f"  original qpair: {qpair_orig}\n"
        f"  pg-compatible qpair: {qpair_pg}\n"
        f"  preserve_order: {preserve_order}\n"
        f"  original hash: {sha256_text(norm_orig)}\n"
        f"  pg-compatible hash: {sha256_text(norm_pg)}\n"
        f"  original result file: {out_orig}\n"
        f"  pg-compatible result file: {out_pg}\n"
        f"  original normalized file: {norm_orig_file}\n"
        f"  pg-compatible normalized file: {norm_pg_file}"
    )
    return "mismatch", msg


def test_dataset(
    runner: PgRunner,
    orig_dataset_dir: Path,
    pg_dataset_dir: Path,
    keep_dbs: bool,
    workdir: Path,
) -> bool:
    dataset_name = orig_dataset_dir.name
    print(f"\n=== DATASET: {dataset_name} ===")

    dump_orig = find_dump_file(orig_dataset_dir)
    dump_pg = find_pg_dump_file(pg_dataset_dir)

    if dump_orig is None:
        print(f"SKIP: no dump.sql found under {orig_dataset_dir}")
        return True
    if dump_pg is None:
        print(f"FAIL: no dump_pg_compatible.sql found under {pg_dataset_dir}")
        return False

    db_orig = f"tmp_orig_{dataset_name}_{uuid.uuid4().hex[:8]}".lower()
    db_pg = f"tmp_pgc_{dataset_name}_{uuid.uuid4().hex[:8]}".lower()

    try:
        print(f"Creating databases: {db_orig}, {db_pg}")
        runner.createdb(db_orig)
        runner.createdb(db_pg)

        print(f"Loading original dump: {dump_orig}")
        runner.run_file(db_orig, dump_orig)

        print(f"Loading pg-compatible dump: {dump_pg}")
        runner.run_file(db_pg, dump_pg)

        print("Comparing schemas...")
        schema_ok, schema_orig_file, schema_pg_file = compare_schema(runner, db_orig, db_pg, dataset_name, workdir)
        if not schema_ok:
            print("FAIL: schema mismatch")
            print(f"  original schema: {schema_orig_file}")
            print(f"  pg-compatible schema: {schema_pg_file}")
            return False
        print("OK: schema matches")

        qpairs_orig = list_qpairs(orig_dataset_dir)
        if not qpairs_orig:
            print("No qpair files found")
            return True

        print(f"Testing {len(qpairs_orig)} qpair files...")
        skipped_disabled = 0

        for q_orig in qpairs_orig:
            q_pg = map_qpair_to_pg(q_orig, orig_dataset_dir, pg_dataset_dir)
            if not q_pg.exists():
                print(f"FAIL: missing transformed qpair: {q_pg}")
                return False

            try:
                status, msg = compare_qpair_results(
                    runner, db_orig, db_pg, q_orig, q_pg, workdir, dataset_name
                )
            except Exception as e:
                print(
                    f"FAIL while running qpair:\n"
                    f"  original: {q_orig}\n"
                    f"  transformed: {q_pg}\n"
                    f"  error: {e}"
                )
                return False

            if status == "skipped_disabled":
                skipped_disabled += 1
                continue

            if status == "ok":
                continue

            if status == "mismatch":
                print("FAIL: qpair result mismatch")
                print(msg)
                return False

            print(f"FAIL: unexpected qpair status '{status}'")
            if msg:
                print(msg)
            return False

        if skipped_disabled:
            print(f"OK: all qpair results match ({skipped_disabled} disabled qpair(s) skipped)")
        else:
            print("OK: all qpair results match")
        return True

    finally:
        if keep_dbs:
            print(f"Keeping databases: {db_orig}, {db_pg}")
        else:
            try:
                runner.dropdb(db_orig)
            except Exception as e:
                print(f"Warning: failed to drop {db_orig}: {e}", file=sys.stderr)
            try:
                runner.dropdb(db_pg)
            except Exception as e:
                print(f"Warning: failed to drop {db_pg}: {e}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Regression test original data vs pg-compatible data.")
    parser.add_argument("original_root", type=Path, help="Original data root, e.g. rodi/data")
    parser.add_argument("pg_compatible_root", type=Path, help="Pg-compatible data root, e.g. data_pg_compatible")
    parser.add_argument("--mode", choices=["auto", "local", "docker"], default="auto")
    parser.add_argument("--container", default=None, help="Docker container name for postgres")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--user", default=None)
    parser.add_argument("--password", default=None)
    parser.add_argument("--maintenance-db", default="postgres")
    parser.add_argument("--dataset", default=None, help="Only test one dataset by directory name")
    parser.add_argument("--keep-dbs", action="store_true")
    parser.add_argument("--workdir", type=Path, default=DEFAULT_WORKDIR)
    args = parser.parse_args()

    mode = detect_mode(args.mode, args.container)
    print(f"Using PostgreSQL mode: {mode}")
    if mode == "docker":
        print(f"Using Docker container: {args.container}")

    runner = PgRunner(
        mode=mode,
        pg=PgConfig(
            host=args.host,
            port=args.port,
            user=args.user,
            password=args.password,
            maintenance_db=args.maintenance_db,
        ),
        container=args.container,
    )

    args.workdir.mkdir(parents=True, exist_ok=True)

    if args.dataset:
        orig_dirs = [args.original_root / args.dataset]
    else:
        orig_dirs = list_dataset_dirs(args.original_root)

    all_ok = True

    for orig_dir in orig_dirs:
        if not orig_dir.exists():
            print(f"FAIL: dataset directory not found: {orig_dir}")
            all_ok = False
            continue

        pg_dir = args.pg_compatible_root / orig_dir.name

        try:
            ok = test_dataset(runner, orig_dir, pg_dir, args.keep_dbs, args.workdir)
        except Exception as e:
            print(f"FAIL: dataset {orig_dir.name} crashed with an exception")
            print(f"Error: {e}")
            ok = False
        
        if not ok:
            all_ok = False

    if not all_ok:
        sys.exit(1)

    print("\nALL DATASETS PASSED")


if __name__ == "__main__":
    main()