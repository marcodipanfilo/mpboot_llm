from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path

import psycopg2

from evaluation.common import EvaluationRunConfig
from evaluation.query_artifacts import archive_query_artifacts
from evaluation.database import ensure_dataset_database_ready
from parsers.dump_split import discover_schemas


JAVA_CP = "target/classes:target/dependency/*:lib/*"
MAIN_CLASS = "com.fluidops.rdb2rdfbench.Main"
_RODI_JVM_FLAGS = [
    "-Djava.util.Arrays.useLegacyMergeSort=true",
]
_RODI_DATATYPE_PATCHES = [
    ("xsd:decimal", "xsd:double"),
    ("http://www.w3.org/2001/XMLSchema#decimal", "http://www.w3.org/2001/XMLSchema#double"),
    ("xsd:numeric", "xsd:double"),
    ("http://www.w3.org/2001/XMLSchema#numeric", "http://www.w3.org/2001/XMLSchema#double"),
    ("xsd:anyURI", "xsd:string"),
    ("http://www.w3.org/2001/XMLSchema#anyURI", "http://www.w3.org/2001/XMLSchema#string"),
    ("xsd:nonNegativeInteger", "xsd:integer"),
    ("http://www.w3.org/2001/XMLSchema#nonNegativeInteger", "http://www.w3.org/2001/XMLSchema#integer"),
    ("xsd:positiveInteger", "xsd:integer"),
    ("http://www.w3.org/2001/XMLSchema#positiveInteger", "http://www.w3.org/2001/XMLSchema#integer"),
    ("xsd:unsignedInt", "xsd:integer"),
    ("http://www.w3.org/2001/XMLSchema#unsignedInt", "http://www.w3.org/2001/XMLSchema#integer"),
    ("xsd:unsignedLong", "xsd:integer"),
    ("http://www.w3.org/2001/XMLSchema#unsignedLong", "http://www.w3.org/2001/XMLSchema#integer"),
]


def _table_schema_map_from_dump(dump_file: Path) -> dict[str, str]:
    lines = dump_file.read_text(encoding="utf-8", errors="replace").splitlines()
    current_schema: str | None = None
    table_to_schema: dict[str, str] = {}

    set_search_path_re = re.compile(r'^\s*SET\s+search_path\s*=\s*("?)([A-Za-z_][A-Za-z0-9_]*)\1\b', re.IGNORECASE)
    create_schema_table_re = re.compile(
        r'^\s*CREATE\s+TABLE\s+(?:"([^"]+)"|([A-Za-z_][A-Za-z0-9_]*))\.(?:"([^"]+)"|([A-Za-z_][A-Za-z0-9_]*))',
        re.IGNORECASE,
    )
    create_table_re = re.compile(r'^\s*CREATE\s+TABLE\s+(?:"([^"]+)"|([A-Za-z_][A-Za-z0-9_]*))', re.IGNORECASE)

    for line in lines:
        search_path_match = set_search_path_re.match(line)
        if search_path_match:
            current_schema = search_path_match.group(2)
            continue

        qualified_match = create_schema_table_re.match(line)
        if qualified_match:
            schema = qualified_match.group(1) or qualified_match.group(2)
            table = qualified_match.group(3) or qualified_match.group(4)
            table_to_schema[table] = schema
            table_to_schema.setdefault(table.lower(), schema)
            continue

        create_match = create_table_re.match(line)
        if create_match and current_schema:
            table = create_match.group(1) or create_match.group(2)
            table_to_schema[table] = current_schema
            table_to_schema.setdefault(table.lower(), current_schema)

    return table_to_schema


def _qualify_sql_tables(sql: str, table_to_schema: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        keyword = match.group("keyword")
        quote = match.group("quote") or ""
        table = match.group("table")
        schema = table_to_schema.get(table) or table_to_schema.get(table.lower())
        if not schema:
            return match.group(0)

        after = match.group("after")
        if after and after.lstrip().startswith("."):
            return match.group(0)

        if quote:
            qualified = f'{keyword} "{schema}"."{table}"'
        else:
            qualified = f"{keyword} {schema}.{table}"
        return qualified + (after or "")

    pattern = re.compile(
        r'(?P<keyword>\bFROM|\bJOIN|\bUPDATE|\bINTO)\s+'
        r'(?P<quote>"?)(?P<table>[A-Za-z_][A-Za-z0-9_]*)'
        r'(?P=quote)(?P<after>\s*\.)?',
        re.IGNORECASE,
    )
    return pattern.sub(replace, sql)


def _qualify_rodi_mapping_sql(content: str, cfg: EvaluationRunConfig) -> tuple[str, list[str]]:
    if cfg.dataset_name != "mondial_rel":
        return content, []

    schemas = discover_schemas(cfg.dump_file.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True))
    table_to_schema = _table_schema_map_from_dump(cfg.dump_file)
    if not table_to_schema:
        return content, []

    if schemas == [cfg.dataset_name]:
        return content, []

    replacements: list[str] = []

    def replace_sql_query(match: re.Match[str]) -> str:
        sql = match.group("sql")
        updated = _qualify_sql_tables(sql, table_to_schema)
        if updated != sql:
            replacements.append(f"rr:sqlQuery {sql!r} -> {updated!r}")
        return match.group("prefix") + updated + match.group("suffix")

    def replace_table_name(match: re.Match[str]) -> str:
        table = match.group("table")
        schema = table_to_schema.get(table) or table_to_schema.get(table.lower())
        if not schema:
            return match.group(0)
        replacements.append(f'rr:tableName "{table}" -> rr:sqlQuery SELECT * FROM "{schema}"."{table}"')
        return (
            f'{match.group("indent")}rr:logicalTable '
            f'[ rr:sqlQuery \'\'\'SELECT * FROM "{schema}"."{table}"\'\'\' ] ;'
            f'  # RODI patch: schema-qualified for mondial_rel because RODI forces SEARCH_PATH to the scenario name'
        )

    content = re.sub(
        r'(?P<prefix>rr:sqlQuery\s+("""|\'\'\'))(?P<sql>.*?)(?P<suffix>\2)',
        replace_sql_query,
        content,
        flags=re.DOTALL,
    )
    content = re.sub(
        r'(?P<indent>^[ \t]*)rr:logicalTable\s+\[\s*rr:tableName\s+"(?P<table>[^"]+)"\s*\]\s*;',
        replace_table_name,
        content,
        flags=re.MULTILINE,
    )
    return content, replacements


def _primary_schema_from_dump(cfg: EvaluationRunConfig) -> str:
    schemas = discover_schemas(cfg.dump_file.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True))
    if schemas:
        return schemas[0]
    return cfg.dataset_name


def _table_name_from_logical_table(logical_table: str) -> str | None:
    table_match = re.search(r'rr:tableName\s+"([^"]+)"', logical_table)
    if table_match:
        return table_match.group(1)

    sql_match = re.search(r'rr:sqlQuery\s+(?:"""|\'\'\')\s*SELECT\s+\*\s+FROM\s+"([^"]+)"(?:\."([^"]+)")?', logical_table, re.IGNORECASE)
    if sql_match:
        if sql_match.group(2):
            return sql_match.group(2)
        return sql_match.group(1)
    return None


def _sanitize_empty_string_datatypes(content: str, cfg: EvaluationRunConfig) -> tuple[str, list[str]]:
    schema = _primary_schema_from_dump(cfg)
    conn = psycopg2.connect(
        dbname=cfg.db_name,
        user=cfg.db_user,
        password=cfg.db_password,
        host=cfg.db_host,
        port=str(cfg.db_port),
    )
    empty_string_cache: dict[tuple[str, str], bool] = {}
    notes: list[str] = []

    triples_map_pattern = re.compile(
        r"(?P<block><[^>]+>\s*a\s+rr:TriplesMap\s*;\s*(?P<body>.*?)(?=^\s*<[^>]+>\s*a\s+rr:TriplesMap\b|\Z))",
        re.DOTALL | re.MULTILINE,
    )
    logical_table_pattern = re.compile(r"rr:logicalTable\s+\[(?P<logical>.*?)\]\s*;", re.DOTALL)
    pom_pattern = re.compile(
        r"(?P<fullblock>rr:predicateObjectMap\s*\[\s*(?P<body>.*?)\s*\]\s*;)",
        re.DOTALL,
    )

    def column_has_empty_string(table: str, column: str) -> bool:
        key = (table, column)
        if key in empty_string_cache:
            return empty_string_cache[key]

        with conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{schema}", public;')
            cur.execute(
                f'SELECT EXISTS (SELECT 1 FROM "{table}" WHERE "{column}" IS NOT NULL AND BTRIM("{column}"::text) = \'\')'
            )
            result = bool(cur.fetchone()[0])
        empty_string_cache[key] = result
        return result

    def replace_triples_map(match: re.Match[str]) -> str:
        block = match.group("block")
        body = match.group("body")
        logical_match = logical_table_pattern.search(body)
        if not logical_match:
            return block

        table = _table_name_from_logical_table(logical_match.group("logical"))
        if not table:
            return block

        def replace_pom(pom_match: re.Match[str]) -> str:
            pom_body = pom_match.group("body")
            if "rr:parentTriplesMap" in pom_body or "rr:template" in pom_body:
                return pom_match.group("fullblock")

            pred_match = re.search(r"rr:predicate\s+([^\s;]+)\s*;", pom_body)
            column_match = re.search(r'rr:column\s+"([^"]+)"\s*;', pom_body)
            dtype_match = re.search(
                r"(?P<indent>\s*)rr:datatype\s+(?P<dtype>xsd:[A-Za-z_][A-Za-z0-9_\-]*|http://www\.w3\.org/2001/XMLSchema#[A-Za-z_][A-Za-z0-9_\-]*|<http://www\.w3\.org/2001/XMLSchema#[A-Za-z_][A-Za-z0-9_\-]*>)\s*;",
                pom_body,
            )
            if not column_match or not dtype_match:
                return pom_match.group("fullblock")

            dtype = dtype_match.group("dtype").strip("<>")
            if dtype in _RODI_STRING_SAFE_DATATYPES:
                return pom_match.group("fullblock")

            column = column_match.group(1)
            try:
                if not column_has_empty_string(table, column):
                    return pom_match.group("fullblock")
            except Exception:
                return pom_match.group("fullblock")

            predicate = pred_match.group(1) if pred_match else "(unknown predicate)"
            notes.append(f'Removed RODI-only datatype for {table}.{column} on {predicate} because empty strings are present.')
            new_body = re.sub(
                r"\n?\s*rr:datatype\s+(?:xsd:[A-Za-z_][A-Za-z0-9_\-]*|<http://www\.w3\.org/2001/XMLSchema#[A-Za-z_][A-Za-z0-9_\-]*>|http://www\.w3\.org/2001/XMLSchema#[A-Za-z_][A-Za-z0-9_\-]*)\s*;",
                "",
                pom_body,
                count=1,
            )
            return pom_match.group("fullblock").replace(pom_body, new_body)

        return block.replace(body, pom_pattern.sub(replace_pom, body))

    try:
        patched = triples_map_pattern.sub(replace_triples_map, content)
    finally:
        conn.close()

    return patched, notes


def _ensure_rodi_layout(cfg: EvaluationRunConfig) -> None:
    if not cfg.rodi_root.exists():
        raise FileNotFoundError(f"RODI root not found: {cfg.rodi_root}")
    if not (cfg.rodi_root / "target").exists():
        raise FileNotFoundError(
            f"RODI build output not found under {cfg.rodi_root / 'target'}. Build the RODI project first."
        )
    if not cfg.qpair_dir.exists():
        raise FileNotFoundError(f"RODI qpair folder not found: {cfg.qpair_dir}")


def _prepare_rodi_scenario_dump(cfg: EvaluationRunConfig) -> Path:
    if not cfg.dump_file.exists():
        raise FileNotFoundError(f"Evaluation dump not found: {cfg.dump_file}")

    scenario_dump = cfg.rodi_root / "data" / cfg.dataset_name / "dump.sql"
    if not scenario_dump.parent.exists():
        raise FileNotFoundError(f"RODI scenario directory not found: {scenario_dump.parent}")

    shutil.copy2(cfg.dump_file, scenario_dump)
    return scenario_dump


def _prepare_rodi_scenario_queries(cfg: EvaluationRunConfig) -> Path | None:
    source_queries_dir = cfg.dump_file.parent / "queries"
    if not source_queries_dir.exists():
        return None

    scenario_queries_dir = cfg.rodi_root / "data" / cfg.dataset_name / "queries"
    if not scenario_queries_dir.parent.exists():
        raise FileNotFoundError(f"RODI scenario directory not found: {scenario_queries_dir.parent}")

    scenario_queries_dir.mkdir(parents=True, exist_ok=True)
    for existing in scenario_queries_dir.glob("*.qpair"):
        existing.unlink()

    copied = 0
    for source_query in sorted(source_queries_dir.glob("*.qpair")):
        destination_name = source_query.name.replace("_pg_compatible", "")
        shutil.copy2(source_query, scenario_queries_dir / destination_name)
        copied += 1

    if copied == 0:
        return None

    return scenario_queries_dir


def _archive_query_artifacts_from_dir(cfg: EvaluationRunConfig, queries_dir: Path | None) -> None:
    if queries_dir is None or not queries_dir.exists():
        return
    qpair_files = sorted(path for path in queries_dir.iterdir() if path.is_file() and path.suffix == ".qpair")
    if not qpair_files:
        return
    output_dir = archive_query_artifacts(cfg, qpair_files)
    print(f"  -> archived query artifacts to: {output_dir}")


def write_rodi_config(cfg: EvaluationRunConfig) -> Path:
    cfg.rodi_config_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.rodi_r2rml_dir.mkdir(parents=True, exist_ok=True)
    db_cmd = cfg.db_cmd.resolve().as_posix() if cfg.db_cmd.is_absolute() else str(cfg.db_cmd)

    content = f"""# Host name of the PostgreSQL server. Default: localhost
dbHost={cfg.db_host}

# Port number of the PostgreSQL server. Default: 5432
dbPort={cfg.db_port}

# PostgreSQL database name to connect to. Default: postgres
postgresDb={cfg.db_name}

# user name for connecting to PostgreSQL. Default: postgres
dbUser={cfg.db_user}

# Password for connecting to PostgreSQL. Default: postgres
dbPassword={cfg.db_password}

# Path to the PostgreSQL command line utility psql. Default: /usr/bin/psql
dbCmd={db_cmd}

# Names/identifiers of scenarios to run (automatic/batch mode, only).
scenario={cfg.dataset_name}

# The location of R2RML mappings, relative to the current working directory.
r2rmlPath={cfg.rodi_r2rml_dir.resolve().as_posix()}/

# Reasoning mode during evaluation.
reasoning={cfg.reasoning}
"""
    cfg.rodi_config_file.write_text(content, encoding="utf-8")
    return cfg.rodi_config_file


def prepare_rodi_mapping(cfg: EvaluationRunConfig) -> Path:
    if not cfg.mapping_file.exists():
        raise FileNotFoundError(f"Generated mapping file not found: {cfg.mapping_file}")

    cfg.rodi_r2rml_dir.mkdir(parents=True, exist_ok=True)
    cfg.evaluation_dir.mkdir(parents=True, exist_ok=True)
    cfg.rodi_output_dir.mkdir(parents=True, exist_ok=True)

    mapping_dst = cfg.rodi_r2rml_dir / cfg.mapping_file.name

    print(f"  -> source mapping: {cfg.mapping_file}")
    content = cfg.mapping_file.read_text(encoding="utf-8")

    content, sql_replacements = _qualify_rodi_mapping_sql(content, cfg)

    patched_lines: list[str] = []
    replacements = []
    total_replacements = 0

    for original_line in content.splitlines():
        line = original_line
        line_notes: list[str] = []
        for source, target in _RODI_DATATYPE_PATCHES:
            count = line.count(source)
            if count > 0:
                line = line.replace(source, target)
                replacements.append(f"{source} -> {target} ({count} occurrences)")
                total_replacements += count
                line_notes.append(f"{source} -> {target} for RODI DB2Triples compatibility")

        if line_notes:
            line = f"{line}  # RODI patch: {'; '.join(line_notes)}"
        patched_lines.append(line)

    patched_content = "\n".join(patched_lines)
    if content.endswith("\n"):
        patched_content += "\n"

    if patched_content != content or sql_replacements:
        header_lines = [
            "# ------------------------------------------------------------",
            "# RODI PATCH APPLIED",
            "# Applied temporary RODI-only compatibility fixes to the mapping copy.",
            "# Each changed line is annotated inline with the reason when possible.",
        ]
        if sql_replacements:
            header_lines.append(
                f"# Schema-qualified logical tables/queries: {len(sql_replacements)} "
                f"(mondial_rel uses schema mondial_rdf2sql_standard while RODI forces SEARCH_PATH to the scenario name)."
            )
        header_lines.append(f"# Total datatype replacements: {total_replacements}")
        header_lines.append("# ------------------------------------------------------------")
        patched_content = "\n".join(header_lines) + "\n\n" + patched_content
        cfg.rodi_patch_file.write_text(patched_content, encoding="utf-8")
        mapping_dst.write_text(patched_content, encoding="utf-8")
        print("  -> applied RODI compatibility patch")
        print(f"  -> patched mapping saved to: {cfg.rodi_patch_file}")
    else:
        shutil.copy2(cfg.mapping_file, mapping_dst)
        print("  -> no RODI patch needed")
    return mapping_dst


def run_cmd(cmd: list[str], cwd: Path, *, quiet: bool = False) -> None:
    print("[CMD]", " ".join(cmd))
    kwargs = {"cwd": cwd, "text": True}
    if quiet:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.STDOUT

    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        if quiet and result.stdout:
            print(result.stdout)
        raise RuntimeError(f"Command failed with exit code {result.returncode}: {' '.join(cmd)}")


def get_rodi_report_paths(cfg: EvaluationRunConfig) -> tuple[Path, Path]:
    reports_dir = cfg.rodi_root / "reports"
    report_name = f"{cfg.dataset_name}_{cfg.dataset_name}_reasoning_{cfg.reasoning}.txt"
    tabular_report_name = f"tabular_{cfg.dataset_name}_{cfg.dataset_name}_reasoning_{cfg.reasoning}.txt"
    return reports_dir / report_name, reports_dir / tabular_report_name


def _base_cmd(cfg: EvaluationRunConfig) -> list[str]:
    return [
        "java",
        *_RODI_JVM_FLAGS,
        "-cp",
        JAVA_CP,
        MAIN_CLASS,
        f"--scenario={cfg.dataset_name}",
        f"--title={cfg.dataset_name}",
    ]


def run_rodi_setup(cfg: EvaluationRunConfig) -> None:
    _ensure_rodi_layout(cfg)
    ensure_dataset_database_ready(cfg)

    print("[STEP] Writing RODI config...")
    config_path = write_rodi_config(cfg)
    print(f"  -> config written at: {config_path}")

    scenario_dump = _prepare_rodi_scenario_dump(cfg)
    print(f"  -> scenario dump refreshed from: {cfg.dump_file}")
    print(f"  -> scenario dump written to: {scenario_dump}")
    scenario_queries = _prepare_rodi_scenario_queries(cfg)
    if scenario_queries is not None:
        print(f"  -> scenario queries refreshed from: {cfg.dump_file.parent / 'queries'}")
        print(f"  -> scenario queries written to: {scenario_queries}")
    _archive_query_artifacts_from_dir(cfg, scenario_queries)

    print("[STEP] Running RODI setup...")
    run_cmd(_base_cmd(cfg) + ["--setup"], cwd=cfg.rodi_root)
    print("  -> setup completed")


def run_rodi(cfg: EvaluationRunConfig, *, include_setup: bool = True) -> None:
    _ensure_rodi_layout(cfg)
    ensure_dataset_database_ready(cfg)

    print("\n====================")
    print("RODI EVALUATION START")
    print("====================\n")

    total_started_at = time.perf_counter()
    setup_elapsed_seconds: float | None = None
    r2rml_elapsed_seconds: float | None = None
    reasoning_elapsed_seconds: float | None = None
    queries_elapsed_seconds: float | None = None

    if include_setup:
        print("[STEP 1] Writing RODI config...")
        config_path = write_rodi_config(cfg)
        print(f"  -> config written at: {config_path}\n")

        scenario_dump = _prepare_rodi_scenario_dump(cfg)
        print(f"  -> scenario dump refreshed from: {cfg.dump_file}")
        print(f"  -> scenario dump written to: {scenario_dump}\n")
        scenario_queries = _prepare_rodi_scenario_queries(cfg)
        if scenario_queries is not None:
            print(f"  -> scenario queries refreshed from: {cfg.dump_file.parent / 'queries'}")
            print(f"  -> scenario queries written to: {scenario_queries}\n")
        _archive_query_artifacts_from_dir(cfg, scenario_queries)

        print("[STEP 2] Running SETUP...")
        setup_started_at = time.perf_counter()
        run_cmd(_base_cmd(cfg) + ["--setup"], cwd=cfg.rodi_root)
        setup_elapsed_seconds = time.perf_counter() - setup_started_at
        print("  -> setup completed\n")
    else:
        print("[STEP 1] Reusing existing RODI setup...\n")

    print("[STEP 2] Preparing mapping..." if not include_setup else "[STEP 3] Preparing mapping...")
    copied_mapping = prepare_rodi_mapping(cfg)
    print(f"  -> mapping copied to: {copied_mapping}\n")

    print("[STEP 3] Running R2RML evaluation..." if not include_setup else "[STEP 4] Running R2RML evaluation...")
    r2rml_started_at = time.perf_counter()
    run_cmd(_base_cmd(cfg) + ["--eval-r2rml"], cwd=cfg.rodi_root, quiet=True)
    r2rml_elapsed_seconds = time.perf_counter() - r2rml_started_at
    print("  -> r2rml evaluation done\n")

    print("[STEP 4] Running reasoning..." if not include_setup else "[STEP 5] Running reasoning...")
    reasoning_started_at = time.perf_counter()
    run_cmd(_base_cmd(cfg) + ["--eval-reasoning"], cwd=cfg.rodi_root)
    reasoning_elapsed_seconds = time.perf_counter() - reasoning_started_at
    print("  -> reasoning completed\n")

    print("[STEP 5] Running queries..." if not include_setup else "[STEP 6] Running queries...")
    queries_started_at = time.perf_counter()
    run_cmd(_base_cmd(cfg) + ["--eval-queries"], cwd=cfg.rodi_root)
    queries_elapsed_seconds = time.perf_counter() - queries_started_at
    print("  -> queries completed\n")

    print("[STEP 6] Collecting report..." if not include_setup else "[STEP 7] Collecting report...")
    report_src, tabular_report_src = get_rodi_report_paths(cfg)
    print(f"  -> expected report at: {report_src}")
    print(f"  -> expected tabular report at: {tabular_report_src}")

    if not report_src.exists():
        raise FileNotFoundError(f"RODI report not found: {report_src}")
    if not tabular_report_src.exists():
        raise FileNotFoundError(f"RODI tabular report not found: {tabular_report_src}")

    cfg.evaluation_dir.mkdir(parents=True, exist_ok=True)
    cfg.rodi_output_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(report_src, cfg.eval_rodi_report_file)
    shutil.copy2(tabular_report_src, cfg.eval_rodi_tabular_file)
    print(f"  -> report copied to: {cfg.eval_rodi_report_file}")
    print(f"  -> tabular report copied to: {cfg.eval_rodi_tabular_file}")

    total_elapsed_seconds = time.perf_counter() - total_started_at
    timings = {
        "scenario": cfg.dataset_name,
        "setup_seconds": setup_elapsed_seconds,
        "r2rml_seconds": r2rml_elapsed_seconds,
        "reasoning_seconds": reasoning_elapsed_seconds,
        "queries_seconds": queries_elapsed_seconds,
        "total_seconds": total_elapsed_seconds,
        "include_setup": include_setup,
    }
    cfg.eval_rodi_timings_file.write_text(
        json.dumps(timings, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"  -> timings written to: {cfg.eval_rodi_timings_file}")

    print("====================")
    print("RODI EVALUATION END")
    print("====================\n")
