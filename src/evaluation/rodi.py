from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from evaluation.common import EvaluationRunConfig
from evaluation.database import ensure_dataset_database_ready


JAVA_CP = "target/classes:target/dependency/*:lib/*"
MAIN_CLASS = "com.fluidops.rdb2rdfbench.Main"


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
    cfg.output_dir.mkdir(parents=True, exist_ok=True)

    mapping_dst = cfg.rodi_r2rml_dir / cfg.mapping_file.name

    print(f"  -> source mapping: {cfg.mapping_file}")
    content = cfg.mapping_file.read_text(encoding="utf-8")
    patched_content = content

    replacements = []
    total_replacements = 0

    count_short = patched_content.count("xsd:anyURI")
    if count_short > 0:
        patched_content = patched_content.replace("xsd:anyURI", "xsd:string")
        replacements.append(f"xsd:anyURI -> xsd:string ({count_short} occurrences)")
        total_replacements += count_short

    count_long = patched_content.count("http://www.w3.org/2001/XMLSchema#anyURI")
    if count_long > 0:
        patched_content = patched_content.replace(
            "http://www.w3.org/2001/XMLSchema#anyURI",
            "http://www.w3.org/2001/XMLSchema#string",
        )
        replacements.append(
            "http://www.w3.org/2001/XMLSchema#anyURI -> "
            f"http://www.w3.org/2001/XMLSchema#string ({count_long} occurrences)"
        )
        total_replacements += count_long

    if patched_content != content:
        print("  -> RODI patch applied: xsd:anyURI -> xsd:string")
        for replacement in replacements:
            print(f"     {replacement}")
        header = f"""# ------------------------------------------------------------
# RODI PATCH APPLIED
# Replaced xsd:anyURI -> xsd:string for DB2Triples compatibility.
# Total replacements: {total_replacements}
# ------------------------------------------------------------

"""
        patched_content = header + patched_content
        cfg.rodi_patch_file.write_text(patched_content, encoding="utf-8")
        mapping_dst.write_text(patched_content, encoding="utf-8")
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

    print("[STEP] Running RODI setup...")
    run_cmd(_base_cmd(cfg) + ["--setup"], cwd=cfg.rodi_root)
    print("  -> setup completed")


def run_rodi(cfg: EvaluationRunConfig, *, include_setup: bool = True) -> None:
    _ensure_rodi_layout(cfg)
    ensure_dataset_database_ready(cfg)

    print("\n====================")
    print("RODI EVALUATION START")
    print("====================\n")

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

        print("[STEP 2] Running SETUP...")
        run_cmd(_base_cmd(cfg) + ["--setup"], cwd=cfg.rodi_root)
        print("  -> setup completed\n")
    else:
        print("[STEP 1] Reusing existing RODI setup...\n")

    print("[STEP 2] Preparing mapping..." if not include_setup else "[STEP 3] Preparing mapping...")
    copied_mapping = prepare_rodi_mapping(cfg)
    print(f"  -> mapping copied to: {copied_mapping}\n")

    print("[STEP 3] Running R2RML evaluation..." if not include_setup else "[STEP 4] Running R2RML evaluation...")
    run_cmd(_base_cmd(cfg) + ["--eval-r2rml"], cwd=cfg.rodi_root, quiet=True)
    print("  -> r2rml evaluation done\n")

    print("[STEP 4] Running reasoning..." if not include_setup else "[STEP 5] Running reasoning...")
    run_cmd(_base_cmd(cfg) + ["--eval-reasoning"], cwd=cfg.rodi_root)
    print("  -> reasoning completed\n")

    print("[STEP 5] Running queries..." if not include_setup else "[STEP 6] Running queries...")
    run_cmd(_base_cmd(cfg) + ["--eval-queries"], cwd=cfg.rodi_root)
    print("  -> queries completed\n")

    print("[STEP 6] Collecting report..." if not include_setup else "[STEP 7] Collecting report...")
    report_src, tabular_report_src = get_rodi_report_paths(cfg)
    print(f"  -> expected report at: {report_src}")
    print(f"  -> expected tabular report at: {tabular_report_src}")

    if not report_src.exists():
        raise FileNotFoundError(f"RODI report not found: {report_src}")
    if not tabular_report_src.exists():
        raise FileNotFoundError(f"RODI tabular report not found: {tabular_report_src}")

    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(report_src, cfg.eval_rodi_report_file)
    shutil.copy2(tabular_report_src, cfg.eval_rodi_tabular_file)
    print(f"  -> report copied to: {cfg.eval_rodi_report_file}")
    print(f"  -> tabular report copied to: {cfg.eval_rodi_tabular_file}")

    print("====================")
    print("RODI EVALUATION END")
    print("====================\n")
