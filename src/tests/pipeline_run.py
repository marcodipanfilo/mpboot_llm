"""
run_pipeline.py — Full R2RML mapping pipeline runner.

Execution order:
  1. parsers/dump_explorer.py
  2. parsers/patterns_discovery.py
  3. agents/understanding.py
  4. agents/enrichment.py
  5. mappers/mapper_phase0_patterns.py
  6. mappers/mapper_phase1_SE.py
  7. mappers/mapper_phase2_SH.py
  8. mappers/mapper_phase3_SEw.py
  9. mappers/mapper_phase4_SRR.py
  10. mappers/mapper_phase5_SR.py
  11. mappers/mapper_phase6_filters.py
  12. mappers/mapper_phase7_verifier.py
  13. mappers/mapper_phase8_R2RML.py

Usage:
  python run_pipeline.py                  # run full pipeline
  python run_pipeline.py --from phase3    # resume from a specific phase
  python run_pipeline.py --only phase7    # run a single phase
  python run_pipeline.py --skip phase4    # skip one phase (can repeat)
  python run_pipeline.py --dry-run        # print what would run, don't execute
"""

import subprocess
import sys
import os
import time
from datetime import datetime
from typing import List, Optional

# ============================================================
# Pipeline definition
# ============================================================

STEPS = [
    {"id": "phase0",            "label": "Phase 0 — Patterns",       "script": "src/agents/mapper_phase0_patterns.py"},
    {"id": "dump_explorer",     "label": "DB Dump Explorer",         "script": "src/parsers/dump_explorer.py"},
    {"id": "patterns_discovery","label": "Pattern Discovery",         "script": "src/parsers/patterns_discovery.py"},
    {"id": "understanding",     "label": "Schema Understanding",      "script": "src/agents/understanding.py"},
    {"id": "enrichment",        "label": "Schema Enrichment",         "script": "src/agents/enrichment.py"},
    {"id": "phase1",            "label": "Phase 1 — SE",              "script": "src/agents/mapper_phase1_SE.py"},
    {"id": "phase2",            "label": "Phase 2 — SH",              "script": "src/agents/mapper_phase2_SH.py"},
    {"id": "phase3",            "label": "Phase 3 — SEw",             "script": "src/agents/mapper_phase3_SEw.py"},
    {"id": "phase4",            "label": "Phase 4 — SRR",             "script": "src/agents/mapper_phase4_SRR.py"},
    {"id": "phase5",            "label": "Phase 5 — SR",              "script": "src/agents/mapper_phase5_SR.py"},
    {"id": "phase6",            "label": "Phase 6 — Filters/HIDDEN",  "script": "src/agents/mapper_phase6_filters.py"},
    {"id": "phase5.B",          "label": "Phase 5.B — OP",            "script": "src/agents/mapper_phase5_OP.py"},
    {"id": "phase7",            "label": "Phase 7 — Verifier",        "script": "src/agents/mapper_phase7_verifier.py"},
    {"id": "phase8",            "label": "Phase 8 — R2RML Generator", "script": "src/agents/mapper_phase8_R2RML.py"},
]

# ============================================================
# Argument parsing (no external deps — plain sys.argv)
# ============================================================

def parse_args():
    args  = sys.argv[1:]
    cfg   = {
        "from_id":  None,
        "only_id":  None,
        "skip_ids": [],
        "dry_run":  False,
    }

    i = 0
    while i < len(args):
        a = args[i]
        if a == "--dry-run":
            cfg["dry_run"] = True
        elif a == "--from" and i + 1 < len(args):
            cfg["from_id"] = args[i + 1]; i += 1
        elif a == "--only" and i + 1 < len(args):
            cfg["only_id"] = args[i + 1]; i += 1
        elif a == "--skip" and i + 1 < len(args):
            cfg["skip_ids"].append(args[i + 1]); i += 1
        elif a in ("--help", "-h"):
            print(__doc__)
            sys.exit(0)
        i += 1

    return cfg


def resolve_steps(cfg: dict) -> List[dict]:
    steps = STEPS[:]

    if cfg["only_id"]:
        matches = [s for s in steps if cfg["only_id"] in (s["id"], s["script"])]
        if not matches:
            _die(f"Unknown step id: '{cfg['only_id']}'")
        return matches

    if cfg["from_id"]:
        ids = [s["id"] for s in steps]
        if cfg["from_id"] not in ids:
            _die(f"Unknown step id: '{cfg['from_id']}'. Valid ids: {ids}")
        idx   = ids.index(cfg["from_id"])
        steps = steps[idx:]

    if cfg["skip_ids"]:
        for sid in cfg["skip_ids"]:
            before = len(steps)
            steps  = [s for s in steps if s["id"] != sid]
            if len(steps) == before:
                print(f"  WARNING: --skip '{sid}' did not match any step id")

    return steps


def _die(msg: str):
    print(f"ERROR: {msg}")
    sys.exit(1)


# ============================================================
# Runner
# ============================================================

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RESET  = "\033[0m"
BOLD   = "\033[1m"


def fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m}m {s}s"


def run_step(step: dict, index: int, total: int, dry_run: bool) -> bool:
    script = step["script"]
    label  = step["label"]
    n      = f"[{index}/{total}]"

    print(f"\n{BOLD}{CYAN}{n} {label}{RESET}")
    print(f"    {YELLOW}→ {script}{RESET}")

    if not os.path.exists(script):
        print(f"    {RED}✗ Script not found: {script}{RESET}")
        return False

    if dry_run:
        print(f"    {YELLOW}(dry run — skipping execution){RESET}")
        return True

    t0     = time.time()
    result = subprocess.run(
        [sys.executable, script],
        text=True,
        capture_output=False,   # let stdout/stderr stream to terminal live
    )
    elapsed = time.time() - t0

    if result.returncode == 0:
        print(f"    {GREEN}✓ Done in {fmt_duration(elapsed)}{RESET}")
        return True
    else:
        print(f"    {RED}✗ FAILED (exit code {result.returncode}) "
              f"after {fmt_duration(elapsed)}{RESET}")
        return False


def run_pipeline(cfg: dict):
    steps    = resolve_steps(cfg)
    total    = len(steps)
    dry_run  = cfg["dry_run"]
    start_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print(f"\n{'=' * 58}")
    print(f"  R2RML MAPPING PIPELINE")
    print(f"  Started : {start_ts}")
    print(f"  Steps   : {total}")
    if dry_run:
        print(f"  Mode    : DRY RUN")
    print(f"{'=' * 58}")

    results  = []
    t_total  = time.time()

    for i, step in enumerate(steps, start=1):
        ok = run_step(step, i, total, dry_run)
        results.append((step, ok))

        if not ok and not dry_run:
            print(f"\n{RED}{BOLD}Pipeline stopped at: {step['label']}{RESET}")
            print(f"Fix the error above and resume with:")
            print(f"  python run_pipeline.py --from {step['id']}\n")
            break

    # ── Summary ──────────────────────────────────────────────
    elapsed_total = time.time() - t_total
    passed  = sum(1 for _, ok in results if ok)
    failed  = sum(1 for _, ok in results if not ok)
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

    sys.exit(0 if failed == 0 else 1)


# ============================================================
# Entry point
# ============================================================

if __name__ == "__main__":
    cfg = parse_args()
    run_pipeline(cfg)