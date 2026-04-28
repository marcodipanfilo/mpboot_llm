#!/usr/bin/env python3
import shutil
import sys
from pathlib import Path

from transform_dump import make_dump_pg_compatible_file
from transform_qpair import make_qpair_pg_compatible_file


def process_tree(input_root: Path, output_root: Path) -> None:
    if not input_root.is_dir():
        raise ValueError(f"Input directory does not exist or is not a directory: {input_root}")

    for src in input_root.rglob("*"):
        rel_path = src.relative_to(input_root)

        if src.is_dir():
            (output_root / rel_path).mkdir(parents=True, exist_ok=True)
            continue

        dst_parent = (output_root / rel_path).parent
        dst_parent.mkdir(parents=True, exist_ok=True)

        # 1) dump.sql -> dump_pg_compatible.sql
        if src.name == "dump.sql":
            dst = dst_parent / "dump_pg_compatible.sql"
            make_dump_pg_compatible_file(src, dst)
            print(f"[dump]   {src} -> {dst}")
            continue

        # 2) *.qpair -> *_pg_compatible.qpair
        if src.suffix == ".qpair":
            dst = dst_parent / f"{src.stem}_pg_compatible.qpair"
            make_qpair_pg_compatible_file(src, dst)
            print(f"[qpair]  {src} -> {dst}")
            continue

        # 3) everything else -> copy unchanged
        dst = output_root / rel_path
        shutil.copy2(src, dst)
        print(f"[copy]   {src} -> {dst}")


def main():
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser()
    parser.add_argument("input_root", type=Path)
    parser.add_argument("output_root", type=Path, nargs="?")  # optional

    args = parser.parse_args()

    input_root = args.input_root

    if args.output_root is None:
        output_root = Path(__file__).resolve().parent / "outputs" / "data_pg_compatible"
    else:
        output_root = args.output_root

    process_tree(input_root, output_root)

if __name__ == "__main__":
    main()