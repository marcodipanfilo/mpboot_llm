"""
Generate OWL/XML ontology files for dataset directories that currently only have ontology.ttl.
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runners.common import convert_ttl_to_owlxml, list_dataset_dirs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate ontology.owl files in OWL/XML syntax from ontology.ttl files."
    )
    parser.add_argument("dataset_root", type=Path, help="Root directory containing dataset subdirectories")
    parser.add_argument("--dataset", action="append", default=[], help="Only convert one dataset name")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing ontology.owl files")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    dataset_dirs = list_dataset_dirs(args.dataset_root.resolve())
    selected = set(args.dataset)

    converted = 0
    skipped = 0

    for dataset_dir in dataset_dirs:
        if selected and dataset_dir.name not in selected:
            continue

        ttl_file = dataset_dir / "ontology.ttl"
        owl_file = dataset_dir / "ontology.owl"

        if not ttl_file.exists():
            print(f"SKIP {dataset_dir.name}: ontology.ttl not found")
            skipped += 1
            continue

        if owl_file.exists() and not args.overwrite:
            print(f"SKIP {dataset_dir.name}: ontology.owl already exists")
            skipped += 1
            continue

        print(f"CONVERT {dataset_dir.name}: {ttl_file} -> {owl_file}")
        convert_ttl_to_owlxml(ttl_file, owl_file)
        converted += 1

    print(f"\nConverted: {converted}")
    print(f"Skipped  : {skipped}")


if __name__ == "__main__":
    main()
