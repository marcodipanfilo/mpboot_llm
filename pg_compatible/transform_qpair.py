#!/usr/bin/env python3
import re
import sys
from pathlib import Path
from sanitizer import transform_sql_for_qpair

TOP_LEVEL_KEY_RE = re.compile(r"^\s*[A-Za-z][A-Za-z0-9_]*\s*=")
SQL_KEY_RE = re.compile(r"^\s*sql\s*=", re.IGNORECASE)


def strip_dataset_schema_prefixes(sql: str, input_path: Path) -> str:
    dataset_dir = input_path.parent.parent
    if dataset_dir.name != "mondial_rel":
        return sql

    for schema_name in ("mondial_rdf2sql_standard", "mondial_rel"):
        sql = re.sub(rf"\b{re.escape(schema_name)}\.", "", sql, flags=re.IGNORECASE)
    return sql


def extract_sql_block(lines):
    """
    Finds the sql block in a qpair file.
    Accepts:
      sql=
      sql =
      SQL =
    Returns:
      (start_index, end_index, prefix)
    where prefix is the exact matched prefix, e.g. "sql=" or "sql ="
    """
    start = None
    prefix = None

    for i, line in enumerate(lines):
        if SQL_KEY_RE.match(line):
            start = i
            m = re.match(r"^(\s*sql\s*=)", line, re.IGNORECASE)
            prefix = m.group(1)
            break

    if start is None:
        raise ValueError("No sql= block found")

    end = start + 1
    while end < len(lines):
        if TOP_LEVEL_KEY_RE.match(lines[end]):
            break
        end += 1

    return start, end, prefix


def make_qpair_pg_compatible_file(input_path: Path, output_path: Path):
    text = input_path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)

    try:
        start, end, prefix = extract_sql_block(lines)
    except ValueError as e:
        raise ValueError(f"{input_path}: {e}") from e

    sql_block = "".join(lines[start:end])
    sql_content = sql_block[len(prefix):]

    new_sql, mapping, collisions = transform_sql_for_qpair(sql_content)
    new_sql = strip_dataset_schema_prefixes(new_sql, input_path)

    lines[start:end] = [prefix + new_sql]
    output_path.write_text("".join(lines), encoding="utf-8")

    return mapping, collisions


def main():
    if len(sys.argv) not in {2, 3}:
        print("Usage: python make_qpair_pg_compatible.py input.qpair [output.qpair]", file=sys.stderr)
        sys.exit(1)

    input_path = Path(sys.argv[1])
    output_path = (
        Path(sys.argv[2])
        if len(sys.argv) == 3
        else input_path.with_name(input_path.stem + "_pg_compatible.qpair")
    )

    mapping, collisions = make_qpair_pg_compatible_file(input_path, output_path)

    print(f"Written: {output_path}")
    print(f"Identifiers normalized: {len(mapping)}")

    if collisions:
        print("\n⚠ collisions detected:")
        for new_name, originals in sorted(collisions.items()):
            print(f"  {new_name} <- {sorted(originals)}")


if __name__ == "__main__":
    main()
