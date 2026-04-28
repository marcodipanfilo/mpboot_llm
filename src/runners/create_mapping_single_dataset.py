"""
Run the mapping pipeline for a single staged dataset.

Optionally stage a dataset directory into the workspace first.
"""

import argparse
import contextlib
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runners.common import ROOT_DIR, STEPS, ensure_workspace_dirs, resolve_dataset_files, stage_dataset

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
RESET = "\033[0m"
BOLD = "\033[1m"


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
    parser = argparse.ArgumentParser(description="Run the full mapping pipeline for a single dataset.")
    parser.add_argument("--from", dest="from_id", default=None, help="Resume from a specific step id")
    parser.add_argument("--only", dest="only_id", default=None, help="Run a single step id")
    parser.add_argument("--skip", dest="skip_ids", action="append", default=[], help="Skip one step id")
    parser.add_argument("--dry-run", action="store_true", help="Print what would run without executing")
    parser.add_argument(
        "--dataset-dir",
        type=Path,
        default=None,
        help="Optional dataset directory to stage into src/inputs before running",
    )
    return parser.parse_args()


def _die(msg: str) -> None:
    print(f"ERROR: {msg}")
    sys.exit(1)


def resolve_steps(cfg: Dict[str, object]) -> List[dict]:
    steps = STEPS[:]

    only_id = cfg.get("only_id")
    from_id = cfg.get("from_id")
    skip_ids = cfg.get("skip_ids", [])

    if only_id:
        matches = [step for step in steps if only_id in (step["id"], step["script"])]
        if not matches:
            _die(f"Unknown step id: '{only_id}'")
        return matches

    if from_id:
        ids = [step["id"] for step in steps]
        if from_id not in ids:
            _die(f"Unknown step id: '{from_id}'. Valid ids: {ids}")
        steps = steps[ids.index(from_id):]

    for step_id in skip_ids:
        before = len(steps)
        steps = [step for step in steps if step["id"] != step_id]
        if len(steps) == before:
            print(f"  WARNING: --skip '{step_id}' did not match any step id")

    return steps


def fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes}m {secs}s"


def run_step(step: dict, index: int, total: int, dry_run: bool) -> bool:
    script_path = ROOT_DIR / step["script"]
    label = step["label"]
    tag = f"[{index}/{total}]"

    print(f"\n{BOLD}{CYAN}{tag} {label}{RESET}")
    print(f"    {YELLOW}→ {step['script']}{RESET}")

    if not script_path.exists():
        print(f"    {RED}✗ Script not found: {script_path}{RESET}")
        return False

    if dry_run:
        print(f"    {YELLOW}(dry run — skipping execution){RESET}")
        return True

    start = time.time()
    proc = subprocess.Popen(
        [sys.executable, str(script_path)],
        cwd=str(ROOT_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        print(line, end="")
    result_code = proc.wait()
    elapsed = time.time() - start

    if result_code == 0:
        print(f"    {GREEN}✓ Done in {fmt_duration(elapsed)}{RESET}")
        return True

    print(f"    {RED}✗ FAILED (exit code {result_code}) after {fmt_duration(elapsed)}{RESET}")
    return False


def run_pipeline(cfg: Dict[str, object], log_file: Optional[Path] = None) -> int:
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, "w", encoding="utf-8") as fh:
            tee_out = TeeStream(sys.stdout, fh)
            tee_err = TeeStream(sys.stderr, fh)
            with contextlib.redirect_stdout(tee_out), contextlib.redirect_stderr(tee_err):
                return _run_pipeline(cfg)
    return _run_pipeline(cfg)


def _run_pipeline(cfg: Dict[str, object]) -> int:
    steps = resolve_steps(cfg)
    total = len(steps)
    dry_run = bool(cfg.get("dry_run"))
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print(f"\n{'=' * 58}")
    print("  R2RML MAPPING PIPELINE")
    print(f"  Started : {started_at}")
    print(f"  Steps   : {total}")
    if dry_run:
        print("  Mode    : DRY RUN")
    print(f"{'=' * 58}")

    results = []
    total_start = time.time()

    for index, step in enumerate(steps, start=1):
        ok = run_step(step, index, total, dry_run)
        results.append((step, ok))
        if not ok and not dry_run:
            print(f"\n{RED}{BOLD}Pipeline stopped at: {step['label']}{RESET}")
            print("Fix the error above and resume with:")
            print(f"  python src/runners/create_mapping_single_dataset.py --from {step['id']}\n")
            break

    elapsed_total = time.time() - total_start
    passed = sum(1 for _, ok in results if ok)
    failed = sum(1 for _, ok in results if not ok)
    skipped = total - len(results)

    print(f"\n{'=' * 58}")
    print(f"  PIPELINE SUMMARY  ({fmt_duration(elapsed_total)} total)")
    print(f"{'=' * 58}")
    for step, ok in results:
        icon = f"{GREEN}✓{RESET}" if ok else f"{RED}✗{RESET}"
        print(f"  {icon}  {step['label']}")
    if skipped:
        for step in steps[len(results):]:
            print(f"  {YELLOW}–{RESET}  {step['label']}  (skipped after failure)")

    print(f"\n  Passed : {GREEN}{passed}{RESET}")
    if failed:
        print(f"  Failed : {RED}{failed}{RESET}")
    if skipped:
        print(f"  Skipped: {YELLOW}{skipped}{RESET}")
    print(f"{'=' * 58}\n")

    return 0 if failed == 0 else 1


def main() -> None:
    args = parse_args()
    ensure_workspace_dirs()

    if args.dataset_dir is not None:
        dataset = resolve_dataset_files(args.dataset_dir.resolve())
        staged_paths = stage_dataset(dataset)
        print(f"Staged dataset '{dataset.dataset_name}' into workspace:")
        print(f"  dump     : {staged_paths['dump']}")
        print(f"  ontology : {staged_paths['ontology']}")

    exit_code = run_pipeline(
        {
            "from_id": args.from_id,
            "only_id": args.only_id,
            "skip_ids": args.skip_ids,
            "dry_run": args.dry_run,
        }
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
