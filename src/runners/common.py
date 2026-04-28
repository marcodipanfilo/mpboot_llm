import glob
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

ROOT_DIR = Path(__file__).resolve().parents[2]
SRC_DIR = ROOT_DIR / "src"

WORKSPACE_INPUTS_DIR = SRC_DIR / "inputs"
WORKSPACE_DATABASE_DIR = WORKSPACE_INPUTS_DIR / "database"
WORKSPACE_ONTOLOGY_DIR = WORKSPACE_INPUTS_DIR / "ontology"
WORKSPACE_OUTPUTS_DIR = SRC_DIR / "outputs"
WORKSPACE_DB_JSON_DIR = WORKSPACE_OUTPUTS_DIR / "DB_as_json"
WORKSPACE_MAPPINGS_DIR = WORKSPACE_OUTPUTS_DIR / "mappings"
WORKSPACE_MEMORY_DIR = SRC_DIR / "memory"
ARCHIVE_OUTPUTS_DIR = ROOT_DIR / "outputs"
TOOLS_DIR = ROOT_DIR / ".tools"
ROBOT_DIR = TOOLS_DIR / "robot"
ROBOT_BIN = ROBOT_DIR / "robot"

WORKSPACE_EXTRA_DELETE_FILES = [
    WORKSPACE_MAPPINGS_DIR / "mappings_r2rml.ttl",
    WORKSPACE_MAPPINGS_DIR / "mappings_r2rml_final.ttl",
    WORKSPACE_DATABASE_DIR / "dump_new.sql",
    WORKSPACE_DATABASE_DIR / "constraint_metadata.json",
]

WORKSPACE_MEMORY_FILES = [
    "understanding.json",
    "patterns.json",
    "patterns_final.json",
    "enrichment.json",
]

STEPS = [
    {"id": "phase0", "label": "Phase 0 — Patterns", "script": "src/agents/mapper_phase0_patterns.py"},
    {"id": "dump_explorer", "label": "DB Dump Explorer", "script": "src/parsers/dump_explorer.py"},
    {"id": "patterns_discovery", "label": "Pattern Discovery", "script": "src/parsers/patterns_discovery.py"},
    {"id": "understanding", "label": "Schema Understanding", "script": "src/agents/understanding.py"},
    {"id": "enrichment", "label": "Schema Enrichment", "script": "src/agents/enrichment.py"},
    {"id": "phase1", "label": "Phase 1 — SE", "script": "src/agents/mapper_phase1_SE.py"},
    {"id": "phase2", "label": "Phase 2 — SH", "script": "src/agents/mapper_phase2_SH.py"},
    {"id": "phase3", "label": "Phase 3 — SEw", "script": "src/agents/mapper_phase3_SEw.py"},
    {"id": "phase4", "label": "Phase 4 — SRR", "script": "src/agents/mapper_phase4_SRR.py"},
    {"id": "phase5", "label": "Phase 5 — SR", "script": "src/agents/mapper_phase5_SR.py"},
    {"id": "phase6", "label": "Phase 6 — Filters/HIDDEN", "script": "src/agents/mapper_phase6_filters.py"},
    {"id": "phase5.B", "label": "Phase 5.B — OP", "script": "src/agents/mapper_phase5_OP.py"},
    {"id": "phase7", "label": "Phase 7 — Verifier", "script": "src/agents/mapper_phase7_verifier.py"},
    {"id": "phase8", "label": "Phase 8 — R2RML Generator", "script": "src/agents/mapper_phase8_R2RML.py"},
]


@dataclass
class WorkspaceDataset:
    dataset_dir: Path
    dump_file: Path
    ontology_file: Path
    dataset_name: str


def sanitize_name(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    slug = slug.strip("._-")
    return slug or "unknown"


def selected_model_name() -> str:
    return sanitize_name(selected_model_id())


def selected_model_id() -> str:
    try:
        from config.llm_config import LLMConfig
        return LLMConfig.get_selected_config()["model_name"]
    except Exception:
        return os.environ.get("MPBOOT_MODEL_NAME", "unknown_model")


def selected_provider_name() -> str:
    try:
        from config.llm_config import SELECTED_PROVIDER
        return SELECTED_PROVIDER
    except Exception:
        return os.environ.get("MPBOOT_PROVIDER", "unknown_provider")


def timestamp_id(now: Optional[datetime] = None) -> str:
    dt = now or datetime.now()
    return dt.strftime("%Y%m%d_%H%M%S_%f")


def ensure_workspace_dirs() -> None:
    for path in [
        WORKSPACE_DATABASE_DIR,
        WORKSPACE_ONTOLOGY_DIR,
        WORKSPACE_DB_JSON_DIR,
        WORKSPACE_MAPPINGS_DIR,
        WORKSPACE_MEMORY_DIR,
        ARCHIVE_OUTPUTS_DIR,
    ]:
        path.mkdir(parents=True, exist_ok=True)


def _delete_glob(base_dir: Path, pattern: str, dry_run: bool) -> Dict[str, int]:
    deleted = 0
    matches = sorted(glob.glob(str(base_dir / pattern)))

    if not matches:
        print(f"  (no {pattern} files found)")
        return {"deleted": 0, "found": 0}

    for path_str in matches:
        path = Path(path_str)
        size = path.stat().st_size
        if dry_run:
            print(f"  DELETE (dry run) {path}  [{size} bytes]")
        else:
            path.unlink()
            print(f"  DELETED  {path}  [{size} bytes]")
            deleted += 1

    return {"deleted": deleted, "found": len(matches)}


def _delete_named(files: List[Path], dry_run: bool) -> Dict[str, int]:
    deleted = 0
    skipped = 0
    found = 0
    for path in files:
        if not path.exists():
            print(f"  SKIP  (not found) {path}")
            skipped += 1
            continue
        found += 1
        size = path.stat().st_size
        if dry_run:
            print(f"  DELETE (dry run) {path}  [{size} bytes]")
        else:
            path.unlink()
            print(f"  DELETED  {path}  [{size} bytes]")
            deleted += 1
    return {"deleted": deleted, "skipped": skipped, "found": found}


def clear_workspace(confirm: bool = False) -> None:
    ensure_workspace_dirs()
    dry_run = not confirm

    print("=" * 56)
    print("  MAPPING FILES RESET")
    print("  MODE: CONFIRMED — files will be deleted" if confirm else "  MODE: DRY RUN — run with --confirm to apply")
    print("=" * 56)

    total_deleted = 0
    total_found = 0
    total_skipped = 0

    print(f"\n── {WORKSPACE_DB_JSON_DIR} (delete all JSON) ──")
    result = _delete_glob(WORKSPACE_DB_JSON_DIR, "*.json", dry_run)
    total_deleted += result["deleted"]
    total_found += result["found"]

    print(f"\n── {WORKSPACE_MAPPINGS_DIR} (delete all JSON) ──")
    result = _delete_glob(WORKSPACE_MAPPINGS_DIR, "*.json", dry_run)
    total_deleted += result["deleted"]
    total_found += result["found"]

    print(f"\n── {WORKSPACE_MAPPINGS_DIR} + {WORKSPACE_DATABASE_DIR} (non-JSON extras) ──")
    result = _delete_named(WORKSPACE_EXTRA_DELETE_FILES, dry_run)
    total_deleted += result["deleted"]
    total_skipped += result["skipped"]
    total_found += result["found"]

    print(f"\n── {WORKSPACE_OUTPUTS_DIR} (delete all JSON, top-level only) ──")
    result = _delete_glob(WORKSPACE_OUTPUTS_DIR, "*.json", dry_run)
    total_deleted += result["deleted"]
    total_found += result["found"]

    print(f"\n── {WORKSPACE_MEMORY_DIR} (memory files — deleted) ──")
    result = _delete_named([WORKSPACE_MEMORY_DIR / name for name in WORKSPACE_MEMORY_FILES], dry_run)
    total_deleted += result["deleted"]
    total_skipped += result["skipped"]
    total_found += result["found"]

    print(f"\n{'=' * 56}")
    if dry_run:
        print(f"  {total_found} files would be affected")
        print(f"  {total_skipped} named files not found (already clean)")
        print("\n  Run with --confirm to apply.")
    else:
        print(f"  {total_deleted} files deleted")
        print(f"  {total_skipped} named files not found (already clean)")
        print("\n  Workspace is ready for a fresh run.")
    print("=" * 56)


def resolve_dataset_files(dataset_dir: Path) -> WorkspaceDataset:
    if not dataset_dir.is_dir():
        raise FileNotFoundError(f"Dataset directory not found: {dataset_dir}")

    dump_candidates = [
        dataset_dir / "dump_pg_compatible.sql",
        dataset_dir / "dump.sql",
    ]
    ontology_candidates = [
        dataset_dir / "ontology.owl",
        dataset_dir / "ontology.ttl",
    ]

    dump_file = next((path for path in dump_candidates if path.exists()), None)
    ontology_file = next((path for path in ontology_candidates if path.exists()), None)

    if dump_file is None:
        raise FileNotFoundError(f"No dump file found in {dataset_dir}. Expected dump_pg_compatible.sql or dump.sql")
    if ontology_file is None:
        raise FileNotFoundError(f"No ontology file found in {dataset_dir}. Expected ontology.owl or ontology.ttl")

    return WorkspaceDataset(
        dataset_dir=dataset_dir,
        dump_file=dump_file,
        ontology_file=ontology_file,
        dataset_name=sanitize_name(dataset_dir.name),
    )


def convert_ttl_to_owlxml(source: Path, destination: Path) -> None:
    if not ROBOT_BIN.exists():
        raise FileNotFoundError(
            f"ROBOT binary not found at {ROBOT_BIN}. Run scripts/bootstrap.sh first."
        )
    subprocess.run(
        [
            str(ROBOT_BIN),
            "convert",
            "--input",
            str(source),
            "--format",
            "owx",
            "--output",
            str(destination),
        ],
        check=True,
        cwd=str(ROOT_DIR),
    )


def _stage_ontology_file(source: Path, destination: Path) -> None:
    if source.suffix.lower() == ".ttl":
        convert_ttl_to_owlxml(source, destination)
        return

    shutil.copy2(source, destination)


def stage_dataset(dataset: WorkspaceDataset) -> Dict[str, Path]:
    ensure_workspace_dirs()

    staged_dump = WORKSPACE_DATABASE_DIR / "dump.sql"
    staged_ontology = WORKSPACE_ONTOLOGY_DIR / "ontology.owl"

    shutil.copy2(dataset.dump_file, staged_dump)
    _stage_ontology_file(dataset.ontology_file, staged_ontology)

    return {
        "dump": staged_dump,
        "ontology": staged_ontology,
    }


def list_dataset_dirs(root: Path) -> List[Path]:
    if not root.is_dir():
        raise FileNotFoundError(f"Dataset root not found: {root}")
    return sorted(path for path in root.iterdir() if path.is_dir())


def archive_run_artifacts(
    *,
    run_dir: Path,
    dataset: WorkspaceDataset,
    staged_paths: Dict[str, Path],
    run_config: Dict[str, object],
) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)

    input_dir = run_dir / "inputs"
    workspace_dir = run_dir / "workspace"
    input_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir.mkdir(parents=True, exist_ok=True)

    shutil.copy2(staged_paths["dump"], input_dir / "dump.sql")
    shutil.copy2(staged_paths["ontology"], input_dir / "ontology.owl")

    if WORKSPACE_INPUTS_DIR.exists():
        shutil.copytree(WORKSPACE_INPUTS_DIR, workspace_dir / "inputs", dirs_exist_ok=True)
    if WORKSPACE_OUTPUTS_DIR.exists():
        shutil.copytree(WORKSPACE_OUTPUTS_DIR, workspace_dir / "outputs", dirs_exist_ok=True)
    if WORKSPACE_MEMORY_DIR.exists():
        shutil.copytree(WORKSPACE_MEMORY_DIR, workspace_dir / "memory", dirs_exist_ok=True)

    final_mapping_file = WORKSPACE_MAPPINGS_DIR / "mappings_r2rml.ttl"
    if final_mapping_file.exists():
        shutil.copy2(final_mapping_file, run_dir / final_mapping_file.name)

    metadata = {
        "dataset_name": dataset.dataset_name,
        "dataset_dir": str(dataset.dataset_dir),
        "source_dump_file": str(dataset.dump_file),
        "source_ontology_file": str(dataset.ontology_file),
        "selected_provider": selected_provider_name(),
        "selected_model": selected_model_id(),
        **run_config,
    }

    with open(run_dir / "run_metadata.json", "w", encoding="utf-8") as fh:
        json.dump(metadata, fh, indent=2)
