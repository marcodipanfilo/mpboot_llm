#!/usr/bin/env python3
import sys
from pathlib import Path
from sanitizer import transform_dump_sql


def main():
    if len(sys.argv) < 2:
        print("Usage: python make_dump_pg_compatible.py dump.sql")
        sys.exit(1)

    inp = Path(sys.argv[1])
    out = inp.with_name(inp.stem + "_pg_compatible.sql")

    sql = inp.read_text(encoding="utf-8")
    new_sql, mapping, collisions = transform_dump_sql(sql)

    out.write_text(new_sql, encoding="utf-8")

    print(f"Written: {out}")
    print(f"Identifiers: {len(mapping)}")

    if collisions:
        print("\n⚠ collisions:")
        for k, v in collisions.items():
            print(k, "<-", v)

def make_dump_pg_compatible_file(input_path: Path, output_path: Path):
    sql = input_path.read_text(encoding="utf-8")
    new_sql, mapping, collisions = transform_dump_sql(sql)
    output_path.write_text(new_sql, encoding="utf-8")
    return mapping, collisions

if __name__ == "__main__":
    main()