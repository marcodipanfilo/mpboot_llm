#!/usr/bin/env python3

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import List, Tuple

try:
    from rdflib import Graph
except ImportError:
    print("Error: rdflib is not installed. Install it with: pip install rdflib", file=sys.stderr)
    sys.exit(2)


ALLOWED_ROOT_FILES: set[str] = set()

ALLOWED_SUBFOLDER_ROOT_FILES = {
    "dump_pg_compatible.sql",
    "ontology.ttl",
    "ontology.owl",
}


def list_immediate_subdirs(base_dir: Path) -> List[Path]:
    return sorted([p for p in base_dir.iterdir() if p.is_dir()])


def get_all_files(folder: Path) -> List[Path]:
    return sorted([p for p in folder.rglob("*") if p.is_file()])


def is_allowed_dataset_file(folder: Path, file_path: Path) -> bool:
    rel = file_path.relative_to(folder)

    if rel.parent == Path(".") and rel.name in ALLOWED_SUBFOLDER_ROOT_FILES:
        return True

    if len(rel.parts) == 2 and rel.parts[0] == "queries" and rel.suffix == ".qpair":
        return True

    return False


def count_qpairs(folder: Path) -> int:
    queries_dir = folder / "queries"
    if not queries_dir.is_dir():
        return 0
    return len(list(queries_dir.glob("*.qpair")))


def audit_dataset_folder(folder: Path) -> Tuple[List[str], List[Path], List[Path]]:
    missing: List[str] = []
    unexpected: List[Path] = []

    dump_file = folder / "dump_pg_compatible.sql"
    ttl_file = folder / "ontology.ttl"
    owl_file = folder / "ontology.owl"
    queries_dir = folder / "queries"

    if not dump_file.is_file():
        missing.append("dump_pg_compatible.sql")

    if not ttl_file.is_file():
        missing.append("ontology.ttl")

    if not owl_file.is_file():
        missing.append("ontology.owl")

    if not queries_dir.is_dir():
        missing.append("queries/")
    elif count_qpairs(folder) == 0:
        missing.append("queries/*.qpair")

    all_files = get_all_files(folder)

    for file_path in all_files:
        if not is_allowed_dataset_file(folder, file_path):
            unexpected.append(file_path)

    return missing, unexpected, all_files


def convert_ttl_to_owl(ttl_path: Path, owl_path: Path) -> None:
    graph = Graph()
    graph.parse(ttl_path, format="turtle")
    graph.serialize(destination=str(owl_path), format="xml")


def convert_owl_to_ttl(owl_path: Path, ttl_path: Path) -> None:
    graph = Graph()
    graph.parse(owl_path, format="xml")
    graph.serialize(destination=str(ttl_path), format="turtle")


def maybe_generate_missing_ontology(folder: Path, verbose: bool = True) -> bool:
    ttl_file = folder / "ontology.ttl"
    owl_file = folder / "ontology.owl"

    ttl_exists = ttl_file.is_file()
    owl_exists = owl_file.is_file()

    if ttl_exists and not owl_exists:
        if verbose:
            print("  ↻ Converting ontology.ttl -> ontology.owl")
        convert_ttl_to_owl(ttl_file, owl_file)
        return True

    if owl_exists and not ttl_exists:
        if verbose:
            print("  ↻ Converting ontology.owl -> ontology.ttl")
        convert_owl_to_ttl(owl_file, ttl_file)
        return True

    return False


def delete_files(paths: List[Path], relative_to: Path, verbose: bool = True, label: str = "Deleting files") -> None:
    if not paths:
        return

    if verbose:
        print(f"  🗑 {label}:")
    for path in paths:
        if verbose:
            print(f"    - {path.relative_to(relative_to)}")
        path.unlink(missing_ok=True)


def delete_root_unexpected_files(base_dir: Path, verbose: bool = True) -> List[Path]:
    deleted: List[Path] = []

    for path in sorted(base_dir.iterdir()):
        if path.is_file():
            if path.name not in ALLOWED_ROOT_FILES:
                deleted.append(path)

    delete_files(deleted, base_dir, verbose=verbose, label="Deleting unexpected root files")
    return deleted


def delete_empty_dirs(folder: Path, verbose: bool = True) -> List[Path]:
    deleted: List[Path] = []

    # deepest first
    for path in sorted(folder.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_dir():
            try:
                next(path.iterdir())
            except StopIteration:
                deleted.append(path)
                if verbose:
                    print(f"  🧹 Deleting empty directory: {path.relative_to(folder)}")
                path.rmdir()

    return deleted


def delete_unexpected_files_in_dataset(folder: Path, verbose: bool = True) -> List[Path]:
    _, unexpected, _ = audit_dataset_folder(folder)
    delete_files(unexpected, folder, verbose=verbose, label="Deleting unexpected files")
    return unexpected


def print_dataset_report(folder: Path, missing: List[str], unexpected: List[Path], all_files: List[Path]) -> None:
    print("=" * 40)
    print(f"Folder: {folder}")

    print("  Files found (excluding .qpair):")
    shown_any = False
    for file_path in all_files:
        rel = file_path.relative_to(folder)
        if not (len(rel.parts) == 2 and rel.parts[0] == "queries" and rel.suffix == ".qpair"):
            print(f"    - {rel}")
            shown_any = True
    if not shown_any:
        print("    - none")

    print()

    dump_file = folder / "dump_pg_compatible.sql"
    ttl_file = folder / "ontology.ttl"
    owl_file = folder / "ontology.owl"
    queries_dir = folder / "queries"
    qpair_count = count_qpairs(folder)

    print(f"  {'✅' if dump_file.is_file() else '❌'} dump_pg_compatible.sql")
    print(f"  {'✅' if ttl_file.is_file() else '❌'} ontology.ttl")
    print(f"  {'✅' if owl_file.is_file() else '❌'} ontology.owl")

    if queries_dir.is_dir():
        print("  ✅ queries/")
        if qpair_count > 0:
            print(f"  ✅ {qpair_count} .qpair file(s)")
        else:
            print("  ❌ queries/*.qpair")
    else:
        print("  ❌ queries/")

    if missing:
        print("  Missing:")
        for item in missing:
            print(f"    - {item}")

    if unexpected:
        print("  Unexpected files:")
        for path in unexpected:
            print(f"    - {path.relative_to(folder)}")

    if not missing and not unexpected:
        print("  🎉 Structure OK")
    else:
        print("  ❌ Structure NOT OK")

    print()


def run_audit(base_dir: Path) -> int:
    had_error = False

    root_unexpected = [p for p in sorted(base_dir.iterdir()) if p.is_file() and p.name not in ALLOWED_ROOT_FILES]
    if root_unexpected:
        print("=" * 40)
        print(f"Base directory: {base_dir}")
        print("  Unexpected root files:")
        for path in root_unexpected:
            print(f"    - {path.name}")
        print()
        had_error = True

    for folder in list_immediate_subdirs(base_dir):
        missing, unexpected, all_files = audit_dataset_folder(folder)
        print_dataset_report(folder, missing, unexpected, all_files)
        if missing or unexpected:
            had_error = True

    return 1 if had_error else 0


def run_fix(base_dir: Path) -> int:
    had_error = False

    print("=" * 40)
    print(f"Base directory: {base_dir}")
    delete_root_unexpected_files(base_dir, verbose=True)
    print()

    for folder in list_immediate_subdirs(base_dir):
        print("=" * 40)
        print(f"Folder: {folder}")

        missing_before, unexpected_before, all_files_before = audit_dataset_folder(folder)
        print("  Before:")
        print_dataset_report(folder, missing_before, unexpected_before, all_files_before)

        delete_unexpected_files_in_dataset(folder, verbose=True)
        delete_empty_dirs(folder, verbose=True)

        try:
            generated = maybe_generate_missing_ontology(folder, verbose=True)
            if not generated:
                print("  ↻ No ontology conversion needed")
        except Exception as exc:
            print(f"  ❌ Ontology conversion failed: {exc}")

        delete_empty_dirs(folder, verbose=True)

        missing_after, unexpected_after, all_files_after = audit_dataset_folder(folder)

        print("  After:")
        print_dataset_report(folder, missing_after, unexpected_after, all_files_after)

        if missing_after or unexpected_after:
            print("  🗑 Folder still invalid after fixing -> deleting folder")
            shutil.rmtree(folder)
            had_error = True
        else:
            print("  ✅ Folder kept")
        print()

    return 1 if had_error else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Audit and fix dataset folders. "
            "In fix mode, unexpected root files are deleted, unexpected files inside dataset folders "
            "are deleted, a missing ontology counterpart is generated, and invalid folders are removed."
        )
    )
    parser.add_argument(
        "base_dir",
        nargs="?",
        default=".",
        help="Base directory whose immediate subfolders should be checked.",
    )
    parser.add_argument(
        "--mode",
        choices=["audit", "fix"],
        default="audit",
        help="audit = only report, fix = delete unexpected files and remove invalid folders.",
    )

    args = parser.parse_args()
    base_dir = Path(args.base_dir).resolve()

    if not base_dir.is_dir():
        print(f"Error: base directory does not exist or is not a directory: {base_dir}", file=sys.stderr)
        return 2

    if args.mode == "audit":
        return run_audit(base_dir)

    return run_fix(base_dir)


if __name__ == "__main__":
    raise SystemExit(main())