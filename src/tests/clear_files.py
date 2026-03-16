"""
clear_mappings.py — Reset all generated mapping files before a new schema run.

Deletes all JSON files in:
  - src/outputs/DB_as_json/
  - src/outputs/mappings/
  - src/outputs/
Deletes specific non-JSON files (e.g. .ttl, .sql).
Deletes memory files in src/memory/.

Usage:
  python clear_mappings.py           # dry run — shows what would be cleared
  python clear_mappings.py --confirm # actually clears the files
"""

import os
import glob
import json
import sys

DRY_RUN = "--confirm" not in sys.argv

MAPPINGS_DIR   = "src/outputs/mappings"
OUTPUTS_DIR    = "src/outputs"
DB_AS_JSON_DIR = "src/outputs/DB_as_json"
MEMORY_DIR     = "src/memory"

# Non-JSON files that also need deletion
EXTRA_DELETE_FILES = [
    os.path.join(MAPPINGS_DIR, "mappings_r2rml.ttl"),
    os.path.join(MAPPINGS_DIR, "mappings_r2rml_final.ttl"),
    os.path.join("src/inputs/database", "dump_new.sql"),
]

# Memory files in src/memory/ (these get DELETED, not emptied)
MEMORY_FILES = [
    "understanding.json",
    "patterns.json",
    "patterns_final.json",
    "enrichment.json",
]


def delete_file(path: str):
    """Delete a file entirely."""
    os.remove(path)


def process_glob(base_dir: str, pattern: str = "*.json"):
    """Delete all files matching pattern in base_dir (non-recursive)."""
    deleted = 0
    matches = sorted(glob.glob(os.path.join(base_dir, pattern)))

    if not matches:
        print(f"  (no {pattern} files found)")
        return 0, 0

    for path in matches:
        size = os.path.getsize(path)
        if DRY_RUN:
            print(f"  DELETE (dry run) {path}  [{size} bytes]")
        else:
            delete_file(path)
            print(f"  DELETED  {path}  [{size} bytes]")
            deleted += 1

    return deleted, len(matches)


def process_named_deletes(file_list: list, base_dir: str):
    """Delete specific named files entirely."""
    deleted = 0
    skipped = 0
    for fname in file_list:
        path = os.path.join(base_dir, fname)
        if not os.path.exists(path):
            print(f"  SKIP  (not found) {path}")
            skipped += 1
            continue

        size = os.path.getsize(path)
        if DRY_RUN:
            print(f"  DELETE (dry run) {path}  [{size} bytes]")
        else:
            delete_file(path)
            print(f"  DELETED  {path}  [{size} bytes]")
            deleted += 1

    return deleted, skipped


def process_extra_deletes():
    """Delete non-JSON files that need full removal (e.g. .ttl, .sql)."""
    deleted = 0
    skipped = 0
    for path in EXTRA_DELETE_FILES:
        if not os.path.exists(path):
            print(f"  SKIP  (not found) {path}")
            skipped += 1
            continue
        size = os.path.getsize(path)
        if DRY_RUN:
            print(f"  DELETE (dry run) {path}  [{size} bytes]")
        else:
            delete_file(path)
            print(f"  DELETED  {path}  [{size} bytes]")
            deleted += 1
    return deleted, skipped


def main():
    print("=" * 56)
    print("  MAPPING FILES RESET")
    if DRY_RUN:
        print("  MODE: DRY RUN — run with --confirm to apply")
    else:
        print("  MODE: CONFIRMED — files will be deleted")
    print("=" * 56)

    total_deleted = 0
    total_found = 0
    total_skipped = 0

    print(f"\n── {DB_AS_JSON_DIR} (delete all JSON) ──")
    d, f = process_glob(DB_AS_JSON_DIR)
    total_deleted += d
    total_found += f

    print(f"\n── {MAPPINGS_DIR} (delete all JSON) ──")
    d, f = process_glob(MAPPINGS_DIR)
    total_deleted += d
    total_found += f

    print(f"\n── {MAPPINGS_DIR} + src/inputs/database (non-JSON extras) ──")
    d, s = process_extra_deletes()
    total_deleted += d
    total_skipped += s

    print(f"\n── {OUTPUTS_DIR} (delete all JSON, top-level only) ──")
    d, f = process_glob(OUTPUTS_DIR)
    total_deleted += d
    total_found += f

    print(f"\n── {MEMORY_DIR} (memory files — deleted) ──")
    d, s = process_named_deletes(MEMORY_FILES, MEMORY_DIR)
    total_deleted += d
    total_skipped += s

    print(f"\n{'=' * 56}")
    if DRY_RUN:
        print(f"  {total_found + len(MEMORY_FILES) - total_skipped} files would be affected")
        print(f"  {total_skipped} named files not found (already clean)")
        print(f"\n  Run with --confirm to apply.")
    else:
        print(f"  {total_deleted} files deleted")
        print(f"  {total_skipped} named files not found (already clean)")
        print(f"\n  Ready for a fresh run: phase1 → phase2 → ... → phase6b → phase7")
    print("=" * 56)


if __name__ == "__main__":
    main()