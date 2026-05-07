#!/usr/bin/env python3
import sys
from pathlib import Path
from transform_qpair import make_qpair_pg_compatible_file


def main():
    if len(sys.argv) != 3:
        print("Usage: python make_qpair_dir_pg_compatible.py input_dir output_dir", file=sys.stderr)
        sys.exit(1)

    input_dir = Path(sys.argv[1])
    output_dir = Path(sys.argv[2])

    if not input_dir.is_dir():
        print(f"Error: not a directory: {input_dir}", file=sys.stderr)
        sys.exit(1)

    output_dir.mkdir(parents=True, exist_ok=True)

    for path in sorted(input_dir.glob("*.qpair")):
        out_path = output_dir / f"{path.stem}_pg_compatible.qpair"
        make_qpair_pg_compatible_file(path, out_path)
        print(f"done: {path.name}")


if __name__ == "__main__":
    main()