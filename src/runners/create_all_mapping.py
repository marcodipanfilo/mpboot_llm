"""
Stage one or more datasets, run the mapping pipeline, and archive each run.
"""

import argparse
import os
import sys
from pathlib import Path
from typing import List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runners.common import (
    ARCHIVE_OUTPUTS_DIR,
    archive_run_artifacts,
    clear_workspace,
    ensure_workspace_dirs,
    list_dataset_dirs,
    resolve_dataset_files,
    selected_model_name,
    stage_dataset,
    timestamp_id,
)
from runners.create_mapping_single_dataset import run_pipeline


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create mappings for all datasets under a root directory.")
    parser.add_argument("dataset_root", type=Path, help="Root directory containing dataset subdirectories")
    parser.add_argument("--dataset", action="append", default=[], help="Only run a specific dataset name")
    parser.add_argument("--from", dest="from_id", default=None, help="Resume single-dataset pipeline from a step")
    parser.add_argument("--only", dest="only_id", default=None, help="Run a single step for each dataset")
    parser.add_argument("--skip", dest="skip_ids", action="append", default=[], help="Skip a step for each dataset")
    parser.add_argument("--dry-run", action="store_true", help="Print the intended actions without executing steps")
    parser.add_argument("--keep-going", action="store_true", help="Continue with later datasets after a failure")
    return parser.parse_args()


def select_dataset_dirs(root: Path, selected_names: List[str]) -> List[Path]:
    dataset_dirs = list_dataset_dirs(root)
    if not selected_names:
        return dataset_dirs

    names = set(selected_names)
    filtered = [path for path in dataset_dirs if path.name in names]
    missing = sorted(names - {path.name for path in filtered})
    if missing:
        raise FileNotFoundError(f"Dataset(s) not found under {root}: {', '.join(missing)}")
    return filtered


def main() -> None:
    args = parse_args()
    ensure_workspace_dirs()

    dataset_dirs = select_dataset_dirs(args.dataset_root.resolve(), args.dataset)
    model_name = selected_model_name()
    batch_run_id = timestamp_id()
    batch_output_dir = ARCHIVE_OUTPUTS_DIR / model_name / batch_run_id

    overall_ok = True

    for dataset_dir in dataset_dirs:
        dataset = resolve_dataset_files(dataset_dir)
        run_dir = ARCHIVE_OUTPUTS_DIR / model_name / batch_run_id / dataset.dataset_name

        print(f"\n{'#' * 72}")
        print(f"Dataset : {dataset.dataset_name}")
        print(f"Archive : {run_dir}")
        print(f"Dump    : {dataset.dump_file}")
        print(f"Ontology: {dataset.ontology_file}")
        print(f"{'#' * 72}")

        log_file = run_dir / "run.log"
        print(f"Log     : {log_file}")

        if args.dry_run:
            clear_workspace(confirm=False)
            staged_paths = {
                "dump": dataset.dump_file,
                "ontology": dataset.ontology_file,
            }
        else:
            clear_workspace(confirm=True)
            staged_paths = stage_dataset(dataset)

        exit_code = run_pipeline(
            {
                "from_id": args.from_id,
                "only_id": args.only_id,
                "skip_ids": args.skip_ids,
                "dry_run": args.dry_run,
            },
            log_file=None if args.dry_run else log_file,
        )

        if not args.dry_run:
            archive_run_artifacts(
                run_dir=run_dir,
                dataset=dataset,
                staged_paths=staged_paths,
                run_config={
                    "batch_run_id": batch_run_id,
                    "pipeline_exit_code": exit_code,
                    "dry_run": args.dry_run,
                    "from_id": args.from_id,
                    "only_id": args.only_id,
                    "skip_ids": args.skip_ids,
                },
            )

        if exit_code != 0:
            overall_ok = False
            if not args.keep_going:
                break

    print(f"\n{'=' * 72}")
    if overall_ok:
        print("All dataset runs finished.")
    else:
        print("Batch run finished with failures.")
    print(f"Outputs saved under: {batch_output_dir}")
    if args.dry_run:
        print("Dry run only: no archive files were written.")
    else:
        print("Each dataset archive contains run_metadata.json, mappings_r2rml.ttl, and run.log at the dataset root.")
    print(f"{'=' * 72}\n")

    sys.exit(0 if overall_ok else 1)


if __name__ == "__main__":
    main()
