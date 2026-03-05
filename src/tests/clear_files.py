"""
clear_mappings.py — Reset all generated mapping files before a new schema run.

Deletes all JSON files in:
  - src2/outputs/DB_as_json/
  - src2/outputs/mappings/
  - src2/outputs/
Deletes specific non-JSON files (e.g. .ttl) and empties memory files.

Usage:
  python clear_mappings.py           # dry run — shows what would be cleared
  python clear_mappings.py --confirm # actually clears the files
"""

import os
import glob
import json
import sys

DRY_RUN = "--confirm" not in sys.argv

MAPPINGS_DIR   = "src2/outputs/mappings"
OUTPUTS_DIR    = "src2/outputs"
DB_AS_JSON_DIR = "src2/outputs/DB_as_json"
MEMORY_DIR     = "src2/memory"

# Non-JSON files that also need deletion
EXTRA_DELETE_FILES = [
    os.path.join(MAPPINGS_DIR, "mappings_r2rml.ttl"),
]

# Memory files in src2/memory/ (these get emptied, not deleted)
MEMORY_FILES = [
    "enrichment.json",
    "understanding.json",
]


def clear_json(path: str):
    """Overwrite a JSON file with an empty dict {}."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump({}, f)


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


def process_named(file_list: list, base_dir: str):
    """Empty specific named files (for memory files)."""
    cleared = 0
    skipped = 0
    for fname in file_list:
        path = os.path.join(base_dir, fname)
        if not os.path.exists(path):
            print(f"  SKIP  (not found) {path}")
            skipped += 1
            continue

        size = os.path.getsize(path)
        if DRY_RUN:
            print(f"  EMPTY  (dry run) {path}  [{size} bytes]")
        else:
            clear_json(path)
            print(f"  EMPTIED  {path}  [{size} bytes → 2 bytes]")
            cleared += 1

    return cleared, skipped


def process_extra_deletes():
    """Delete non-JSON files that need full removal (e.g. .ttl)."""
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
            print(f"  DELETED  {path}")
            deleted += 1
    return deleted, skipped


def main():
    print("=" * 56)
    print("  MAPPING FILES RESET")
    if DRY_RUN:
        print("  MODE: DRY RUN — run with --confirm to apply")
    else:
        print("  MODE: CONFIRMED — files will be deleted/cleared")
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

    print(f"\n── {MAPPINGS_DIR} (non-JSON extras) ──")
    d, s = process_extra_deletes()
    total_deleted += d
    total_skipped += s

    print(f"\n── {OUTPUTS_DIR} (delete all JSON, top-level only) ──")
    d, f = process_glob(OUTPUTS_DIR)
    total_deleted += d
    total_found += f

    print(f"\n── {MEMORY_DIR} (schema memory — emptied, not deleted) ──")
    c, s = process_named(MEMORY_FILES, MEMORY_DIR)
    total_deleted += c
    total_skipped += s

    print(f"\n{'=' * 56}")
    if DRY_RUN:
        print(f"  {total_found + len(MEMORY_FILES) - total_skipped} files would be affected")
        print(f"  {total_skipped} named files not found (already clean)")
        print(f"\n  Run with --confirm to apply.")
    else:
        print(f"  {total_deleted} files deleted/cleared")
        print(f"  {total_skipped} named files not found (already clean)")
        print(f"\n  Ready for a fresh run: phase1 → phase2 → ... → phase6b → phase7")
    print("=" * 56)


if __name__ == "__main__":
    main()