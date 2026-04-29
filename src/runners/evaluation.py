"""Evaluate archived mapping runs with RODI and/or Ontop."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Iterable, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evaluation.common import EvaluationRunConfig


class TeeStream:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data: str) -> int:
        for stream in self.streams:
            stream.write(data)
        return len(data)

    def flush(self) -> None:
        for stream in self.streams:
            stream.flush()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate archived mapping outputs.")
    parser.add_argument(
        "run_path",
        type=Path,
        help="Dataset archive directory or batch directory under outputs/<model>/<batch_timestamp>",
    )
    parser.add_argument("--dataset", action="append", default=[], help="Only evaluate a specific dataset name")
    parser.add_argument(
        "--method",
        choices=["all", "rodi", "ontop"],
        default="all",
        help="Which evaluation method to run",
    )
    parser.add_argument("--rodi-root", type=Path, default=Path(os.environ.get("MPBOOT_RODI_ROOT", ".tools/rodi")))
    parser.add_argument(
        "--ontop-dir",
        type=Path,
        default=Path(os.environ.get("MPBOOT_ONTOP_DIR", ".tools/ontop")),
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
    parser.add_argument("--reasoning", default=os.environ.get("MPBOOT_RODI_REASONING", "structural"))
    parser.add_argument("--ontop-port", type=int, default=int(os.environ.get("MPBOOT_ONTOP_PORT", "8089")))
    parser.add_argument(
        "--db-setup",
        choices=["auto", "rodi", "dump", "none"],
        default=os.environ.get("MPBOOT_DB_SETUP", "auto"),
        help="How to prepare the database before evaluation",
    )
    parser.add_argument("--compare-tabular", action="store_true", help="Compare Ontop and RODI tabular reports")
    parser.add_argument("--keep-going", action="store_true", help="Continue with later datasets after a failure")
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
            f"No evaluation dump found in {dataset_dir}. Checked metadata source_dump_file, "
            "inputs/dump_pg_compatible.sql, workspace/inputs/database/dump_new.sql, "
            "inputs/dump.sql, and workspace/inputs/database/dump.sql."
        )

    db_cmd = args.db_cmd
    if not db_cmd.is_absolute():
        resolved = shutil.which(str(db_cmd))
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
        rodi_root=args.rodi_root.resolve(),
        ontop_dir=args.ontop_dir.resolve(),
        db_host=args.db_host,
        db_port=args.db_port,
        db_name=args.db_name,
        db_user=args.db_user,
        db_password=args.db_password,
        db_cmd=db_cmd,
        reasoning=args.reasoning,
        ontop_port=args.ontop_port,
    )


def _run_compare(cfg: EvaluationRunConfig) -> None:
    from evaluation.utils_compare import build_tabular_diff

    diff_text = build_tabular_diff(cfg.eval_ontop_tabular_file, cfg.eval_rodi_tabular_file)
    if diff_text:
        cfg.comparison_diff_file.write_text(diff_text + "\n", encoding="utf-8")
        raise RuntimeError(f"Tabular reports differ. Diff saved to: {cfg.comparison_diff_file}")
    if cfg.comparison_diff_file.exists():
        cfg.comparison_diff_file.unlink()
    print("[CHECK] Tabular files are identical.")


def _resolve_db_setup(cfg: EvaluationRunConfig, args: argparse.Namespace) -> str:
    if args.db_setup != "auto":
        return args.db_setup
    return "rodi" if cfg.qpair_dir.exists() else "dump"


def _run_dataset(cfg: EvaluationRunConfig, args: argparse.Namespace) -> None:
    from evaluation.ontop_like_llm4vkg import evaluate_with_ontop_like_llm4vkg
    from evaluation.rodi import run_rodi, run_rodi_setup
    from evaluation.database import ensure_dataset_database_ready, prepare_database_from_dump

    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    log_file = cfg.output_dir / "evaluation.log"
    db_setup = _resolve_db_setup(cfg, args)

    with open(log_file, "w", encoding="utf-8") as fh:
        tee_out = TeeStream(sys.stdout, fh)
        tee_err = TeeStream(sys.stderr, fh)
        with contextlib.redirect_stdout(tee_out), contextlib.redirect_stderr(tee_err):
            print(f"\n{'=' * 72}")
            print(f"Dataset   : {cfg.dataset_name}")
            print(f"Run dir   : {cfg.dataset_dir}")
            print(f"Method    : {args.method}")
            print(f"DB setup  : {db_setup}")
            print(f"RODI root : {cfg.rodi_root}")
            print(f"Ontop dir : {cfg.ontop_dir}")
            print(f"DB        : {cfg.db_user}@{cfg.db_host}:{cfg.db_port}/{cfg.db_name}")
            print(f"Log file  : {log_file}")
            print(f"{'=' * 72}")

            if args.method == "rodi":
                run_rodi(cfg, include_setup=True)
            else:
                if db_setup == "rodi":
                    run_rodi_setup(cfg)
                elif db_setup == "dump":
                    prepare_database_from_dump(cfg)
                elif db_setup == "none":
                    ensure_dataset_database_ready(cfg)
                elif db_setup != "none":
                    raise ValueError(f"Unsupported db setup mode: {db_setup}")

            if args.method in {"all", "ontop"}:
                evaluate_with_ontop_like_llm4vkg(cfg)
            if args.method == "all":
                if db_setup == "rodi":
                    run_rodi(cfg, include_setup=False)
                else:
                    run_rodi(cfg, include_setup=True)
            if args.compare_tabular and args.method == "all":
                _run_compare(cfg)

            print(f"\nSaved evaluation artifacts under: {cfg.output_dir}\n")


def main() -> None:
    args = parse_args()
    dataset_dirs = _select_dataset_dirs(args.run_path, args.dataset)

    overall_ok = True
    for dataset_dir in dataset_dirs:
        try:
            _run_dataset(_build_config(dataset_dir, args), args)
        except Exception as exc:
            overall_ok = False
            print(f"\n[ERROR] Evaluation failed for {dataset_dir.name}: {exc}\n", file=sys.stderr)
            if not args.keep_going:
                break

    print(f"\n{'=' * 72}")
    if overall_ok:
        print("All evaluations finished.")
    else:
        print("Evaluation finished with failures.")
    print(f"Evaluated datasets under: {args.run_path.resolve()}")
    print(f"{'=' * 72}\n")
    sys.exit(0 if overall_ok else 1)


if __name__ == "__main__":
    main()
