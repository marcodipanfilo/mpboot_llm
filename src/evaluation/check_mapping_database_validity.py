from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple


ROOT_DIR = Path(__file__).resolve().parents[2]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

try:
    import psycopg2  # type: ignore
except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard
    raise SystemExit(
        "Missing Python dependency 'psycopg2'. Run scripts/bootstrap.sh first so the evaluation environment is ready."
    ) from exc

from evaluation.common import EvaluationRunConfig
from evaluation.database import prepare_database_from_dump
from parsers.dump_split import discover_schemas

REPORT_FILE_NAME = "check_mapping_database_validity__report.txt"


@dataclass
class TriplesMapBlock:
    iri: str
    line_no: int
    block_start: int
    block_end: int
    block_lines: List[str]
    logical_kind: str
    logical_value: str
    local_columns: Set[str]
    parent_links: List[Tuple[str, str]]


@dataclass
class MapIssue:
    reason: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate R2RML logical tables and referenced columns against the archived dataset dump database."
    )
    parser.add_argument(
        "run_path",
        type=Path,
        help="Dataset archive directory or batch directory under outputs/<model>/<batch_timestamp>",
    )
    parser.add_argument("--dataset", action="append", default=[], help="Only process a specific dataset name")
    parser.add_argument("--remove", action="store_true", help="Comment out invalid triples maps in place")
    parser.add_argument(
        "--output",
        type=Path,
        help="Write the sanitized TTL to a different file instead of updating in place. Only valid for a single dataset.",
    )
    parser.add_argument("--db-host", default=os.environ.get("MPBOOT_DB_HOST", "localhost"))
    parser.add_argument("--db-port", type=int, default=int(os.environ.get("MPBOOT_DB_PORT", "5433")))
    parser.add_argument("--db-name", default=os.environ.get("MPBOOT_DB_NAME", "postgres"))
    parser.add_argument("--db-user", default=os.environ.get("MPBOOT_DB_USER", "postgres"))
    parser.add_argument("--db-password", default=os.environ.get("MPBOOT_DB_PASSWORD", "postgres"))
    parser.add_argument(
        "--db-cmd",
        type=Path,
        default=Path(os.environ.get("MPBOOT_DB_CMD", ".tools/bin/psql_docker.sh")),
    )
    parser.add_argument(
        "--db-setup",
        choices=["dump", "none"],
        default="dump",
        help="Prepare the database from the archived dump before validating",
    )
    return parser.parse_args()


def _is_dataset_run_dir(path: Path) -> bool:
    return (path / "run_metadata.json").exists() and (path / "mappings_r2rml.ttl").exists()


def _select_dataset_dirs(run_path: Path, selected_names: Iterable[str]) -> List[Path]:
    run_path = run_path.resolve()
    if _is_dataset_run_dir(run_path):
        dataset_dirs = [run_path]
    else:
        dataset_dirs = sorted(path for path in run_path.iterdir() if path.is_dir() and _is_dataset_run_dir(path))

    if not dataset_dirs:
        raise FileNotFoundError(
            f"No dataset run directories found under {run_path}. Expected run_metadata.json and mappings_r2rml.ttl."
        )

    if not selected_names:
        return dataset_dirs

    selected = set(selected_names)
    filtered = [path for path in dataset_dirs if path.name in selected]
    missing = sorted(selected - {path.name for path in filtered})
    if missing:
        raise FileNotFoundError(f"Dataset(s) not found under {run_path}: {', '.join(missing)}")
    return filtered


def _build_config(dataset_dir: Path, args: argparse.Namespace) -> EvaluationRunConfig:
    metadata_path = dataset_dir / "run_metadata.json"
    dump_candidates = []
    if metadata_path.exists():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        source_dump = metadata.get("source_dump_file")
        if source_dump:
            dump_candidates.append(Path(source_dump))
    dump_candidates.extend(
        [
            dataset_dir / "inputs" / "dump_pg_compatible.sql",
            dataset_dir / "workspace" / "inputs" / "database" / "dump_new.sql",
            dataset_dir / "inputs" / "dump.sql",
            dataset_dir / "workspace" / "inputs" / "database" / "dump.sql",
        ]
    )
    dump_file = next((path.resolve() for path in dump_candidates if path.exists()), None)
    if dump_file is None:
        raise FileNotFoundError(
            f"No validation dump found in {dataset_dir}. Checked metadata source_dump_file, "
            "inputs/dump_pg_compatible.sql, workspace/inputs/database/dump_new.sql, "
            "inputs/dump.sql, and workspace/inputs/database/dump.sql."
        )

    db_cmd = args.db_cmd
    if not db_cmd.is_absolute():
        resolved = shutil.which(str(db_cmd))  # type: ignore[name-defined]
        if resolved is not None:
            db_cmd = Path(resolved)
            if not db_cmd.is_absolute():
                db_cmd = (Path.cwd() / db_cmd).resolve()
        else:
            db_cmd = (Path.cwd() / db_cmd).resolve()

    return EvaluationRunConfig(
        dataset_dir=dataset_dir,
        dataset_name=dataset_dir.name,
        mapping_file=dataset_dir / "mappings_r2rml.ttl",
        ontology_file=dataset_dir / "inputs" / "ontology.owl",
        dump_file=dump_file,
        output_dir=dataset_dir / "evaluation",
        rodi_root=(ROOT_DIR / ".tools" / "rodi").resolve(),
        ontop_dir=(ROOT_DIR / ".tools" / "ontop").resolve(),
        db_host=args.db_host,
        db_port=args.db_port,
        db_name=args.db_name,
        db_user=args.db_user,
        db_password=args.db_password,
        db_cmd=db_cmd,
    )


def _schema_search_path_from_dump(dump_file: Path) -> str:
    lines = dump_file.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    schemas = discover_schemas(lines)
    if not schemas:
        return "public"
    return ", ".join(schemas + ["public"])


def _connect(cfg: EvaluationRunConfig):
    conn = psycopg2.connect(
        host=cfg.db_host,
        port=cfg.db_port,
        dbname=cfg.db_name,
        user=cfg.db_user,
        password=cfg.db_password,
    )
    conn.autocommit = True
    return conn


def _split_quoted_template_vars(template: str) -> Set[str]:
    return set(re.findall(r"\{([^{}]+)\}", template))


def _extract_block_info(block_lines: Sequence[str], iri: str, line_no: int, start: int, end: int) -> TriplesMapBlock:
    block_text = "\n".join(block_lines)
    sql_match = re.search(r"rr:sqlQuery\s+'''(.*?)'''", block_text, flags=re.DOTALL)
    table_match = re.search(r'rr:tableName\s+"([^"]+)"', block_text)
    if sql_match:
        logical_kind = "sqlQuery"
        logical_value = sql_match.group(1).strip()
    elif table_match:
        logical_kind = "tableName"
        logical_value = table_match.group(1)
    else:
        logical_kind = "unknown"
        logical_value = ""

    local_columns: Set[str] = set(re.findall(r'rr:column\s+"([^"]+)"', block_text))
    local_columns.update(_split_quoted_template_vars(block_text))
    local_columns.update(re.findall(r'rr:child\s+"([^"]+)"', block_text))

    parent_links: List[Tuple[str, str]] = []
    for match in re.finditer(
        r"rr:parentTriplesMap\s+<([^>]+)>.*?rr:joinCondition\s*\[\s*.*?rr:parent\s+\"([^\"]+)\"",
        block_text,
        flags=re.DOTALL,
    ):
        parent_links.append((match.group(1), match.group(2)))

    return TriplesMapBlock(
        iri=iri,
        line_no=line_no,
        block_start=start,
        block_end=end,
        block_lines=list(block_lines),
        logical_kind=logical_kind,
        logical_value=logical_value,
        local_columns=local_columns,
        parent_links=parent_links,
    )


def _parse_triples_maps(text: str) -> List[TriplesMapBlock]:
    lines = text.splitlines()
    maps: List[TriplesMapBlock] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        start_match = re.match(r"\s*<([^>]+)>\s*$", line)
        if not start_match:
            i += 1
            continue
        iri = start_match.group(1)
        if i + 1 >= len(lines) or "a rr:TriplesMap" not in lines[i + 1]:
            i += 1
            continue

        start = i
        block_lines = [lines[i]]
        i += 1
        while i < len(lines):
            block_lines.append(lines[i])
            if lines[i].strip().endswith("."):
                break
            i += 1

        end = i
        maps.append(_extract_block_info(block_lines, iri, start + 1, start, end))
        i += 1

    return maps


def _logical_query(block: TriplesMapBlock) -> Optional[str]:
    if block.logical_kind == "sqlQuery":
        return block.logical_value
    if block.logical_kind == "tableName":
        return f'SELECT * FROM "{block.logical_value}"'
    return None


def _describe_query_columns(conn, search_path: str, block: TriplesMapBlock) -> Tuple[Optional[Set[str]], Optional[str]]:
    sql = _logical_query(block)
    if not sql:
        return None, "missing rr:logicalTable definition"

    wrapped = f"SELECT * FROM ({sql}) AS __mpboot_validate LIMIT 0"
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {search_path}")
            cur.execute(wrapped)
            cols = {desc.name for desc in cur.description or []}
            return cols, None
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        return None, f"invalid logical table/query: {exc}"


def _validate_triples_maps(conn, search_path: str, blocks: List[TriplesMapBlock]) -> Dict[str, List[MapIssue]]:
    columns_by_map: Dict[str, Set[str]] = {}
    issues: Dict[str, List[MapIssue]] = {}

    for block in blocks:
        cols, error = _describe_query_columns(conn, search_path, block)
        if error is not None:
            issues.setdefault(block.iri, []).append(MapIssue(error))
            continue
        assert cols is not None
        columns_by_map[block.iri] = cols

        missing_local = sorted(col for col in block.local_columns if col not in cols)
        if missing_local:
            issues.setdefault(block.iri, []).append(
                MapIssue(f"referenced column(s) not returned by logical table/query: {', '.join(missing_local)}")
            )

    for block in blocks:
        if block.iri in issues:
            continue
        for parent_iri, parent_col in block.parent_links:
            parent_cols = columns_by_map.get(parent_iri)
            if parent_cols is None:
                issues.setdefault(block.iri, []).append(
                    MapIssue(f"parent triples map <{parent_iri}> is already invalid or missing")
                )
                continue
            if parent_col not in parent_cols:
                issues.setdefault(block.iri, []).append(
                    MapIssue(
                        f'join parent column "{parent_col}" not returned by parent triples map <{parent_iri}>'
                    )
                )

    return issues


def _comment_out_invalid_maps(text: str, blocks: List[TriplesMapBlock], issues: Dict[str, List[MapIssue]]) -> str:
    lines = text.splitlines()
    invalid_blocks = [block for block in blocks if block.iri in issues]
    note_lines = [
        "# ------------------------------------------------------------",
        "# AUTO-REMOVED INVALID TRIPLES MAPS",
        "# The blocks commented out below failed mapping/database validation.",
        "# Reasons include invalid logical table queries, missing tables, and",
        "# references to columns that are not returned by the logical table.",
        f"# Full report: evaluation/{REPORT_FILE_NAME}",
        "# ------------------------------------------------------------",
        "",
    ]

    for block in sorted(invalid_blocks, key=lambda item: item.block_start, reverse=True):
        indent = re.match(r"\s*", lines[block.block_start]).group(0)
        commented = [
            f"{indent}# {line[len(indent):] if line.startswith(indent) else line}"
            for line in lines[block.block_start : block.block_end + 1]
        ]
        reason_comment_lines: List[str] = []
        for issue in issues[block.iri]:
            issue_lines = issue.reason.splitlines() or [issue.reason]
            for idx, issue_line in enumerate(issue_lines):
                prefix = "Reason: " if idx == 0 else "        "
                reason_comment_lines.append(f"{indent}# {prefix}{issue_line}")
        reason_lines = [
            f"{indent}# Auto-removed invalid triples map <{block.iri}>",
            *reason_comment_lines,
            *commented,
        ]
        lines[block.block_start : block.block_end + 1] = reason_lines

    return "\n".join(note_lines + lines) + "\n"


def _write_issue_report(
    report_file: Path,
    dataset_name: str,
    mapping_file: Path,
    dump_file: Path,
    search_path: str,
    blocks: List[TriplesMapBlock],
    issues: Dict[str, List[MapIssue]],
) -> None:
    report_file.parent.mkdir(parents=True, exist_ok=True)
    block_lookup = {block.iri: block for block in blocks}
    chunks: List[str] = [
        "DATABASE VALIDATION REPORT",
        f"Dataset   : {dataset_name}",
        f"Mapping   : {mapping_file}",
        f"Dump      : {dump_file}",
        f"SearchPath: {search_path}",
        "",
    ]

    for iri, block_issues in issues.items():
        block = block_lookup[iri]
        chunks.append("=" * 72)
        chunks.append(f"TriplesMap: <{iri}>")
        chunks.append(f"Line      : {block.line_no}")
        chunks.append(f"Logical   : {block.logical_kind} = {block.logical_value}")
        chunks.append("Issues:")
        for idx, issue in enumerate(block_issues, start=1):
            chunks.append(f"  {idx}.")
            for issue_line in issue.reason.splitlines() or [issue.reason]:
                chunks.append(f"     {issue_line}")
        chunks.append("")
        chunks.append("Original block:")
        chunks.extend(block.block_lines)
        chunks.append("")

    report_file.write_text("\n".join(chunks) + "\n", encoding="utf-8")


def _broken_mapping_path(mapping_file: Path) -> Path:
    return mapping_file.with_name(f"{mapping_file.stem}_broken{mapping_file.suffix}")


def _process_dataset(dataset_dir: Path, args: argparse.Namespace, *, output_file: Optional[Path]) -> int:
    cfg = _build_config(dataset_dir, args)
    mapping_file = cfg.mapping_file.resolve()
    if not mapping_file.exists():
        raise FileNotFoundError(f"Mapping file not found: {mapping_file}")

    print(f"\nDataset: {dataset_dir.name}")
    if args.db_setup == "dump":
        print(f"  Preparing database from dump: {cfg.dump_file}")
        prepare_database_from_dump(cfg)

    search_path = _schema_search_path_from_dump(cfg.dump_file)
    print(f"  Using search_path: {search_path}")

    text = mapping_file.read_text(encoding="utf-8")
    blocks = _parse_triples_maps(text)
    if not blocks:
        print("  No triples maps found.")
        return 0

    with _connect(cfg) as conn:
        issues = _validate_triples_maps(conn, search_path, blocks)

    if not issues:
        print("  No database-level mapping issues found.")
        return 0

    print("  Found invalid triples maps:")
    block_lookup = {block.iri: block for block in blocks}
    for iri, block_issues in issues.items():
        line_no = block_lookup[iri].line_no
        print(f"    line {line_no}: <{iri}>")
        for issue in block_issues:
            print(f"      - {issue.reason}")

    report_file = cfg.output_dir / REPORT_FILE_NAME
    _write_issue_report(report_file, dataset_dir.name, mapping_file, cfg.dump_file, search_path, blocks, issues)
    print(f"  Wrote validation report to: {report_file}")

    if not args.remove:
        print("  Re-run with --remove to comment out invalid triples maps.")
        return 1

    sanitized = _comment_out_invalid_maps(text, blocks, issues)
    if output_file:
        destination = output_file.resolve()
    else:
        destination = (cfg.shared_output_dir / "mappings__r2rml_db_valid_sanitized.ttl").resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(sanitized, encoding="utf-8")
    print(f"  Wrote sanitized mapping to: {destination}")
    return 0


def main() -> int:
    args = parse_args()
    dataset_dirs = _select_dataset_dirs(args.run_path, args.dataset)

    if args.output and len(dataset_dirs) != 1:
        raise ValueError("--output can only be used when processing exactly one dataset.")

    overall_ok = True
    for dataset_dir in dataset_dirs:
        try:
            exit_code = _process_dataset(
                dataset_dir,
                args,
                output_file=args.output if len(dataset_dirs) == 1 else None,
            )
        except Exception as exc:
            print(f"\nDataset: {dataset_dir.name}")
            print(f"  Failed unexpectedly: {type(exc).__name__}: {exc}")
            exit_code = 1
        overall_ok = overall_ok and (exit_code == 0)

    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
