
"""
split_dump.py
-------------
Analyse a PostgreSQL dump file containing multiple schemas.

Modes:
  1. Generic split mode:
     If the dump contains exactly two schemas, split it into two self-contained
     dump files – one per schema.

  2. Keep-one-schema mode:
     Keep only a requested schema and discard all other schema-specific content,
     writing a single self-contained dump file.

Default paths preserve the legacy behaviour:
  Input : src/inputs/database/dump.sql
  Output: src/inputs/database/dump.sql   (first schema discovered)
          src/inputs/database/dump_2.sql (second schema discovered)
"""

import argparse
import re
import sys
from pathlib import Path
from typing import Optional

# ── paths ──────────────────────────────────────────────────────────────────
INPUT_PATH  = Path("src/inputs/database/dump.sql")
OUTPUT_DIR  = Path("src/inputs/database")

# ── regex for search_path switch ───────────────────────────────────────────
SP_RE = re.compile(
    r"^set\s+search_path\s*=\s*(\w+)\s*,",
    re.IGNORECASE,
)

# ── regex for schema creation / drop ───────────────────────────────────────
SCHEMA_DDL_RE = re.compile(
    r"^(create|drop)\s+schema\s+(if\s+(not\s+)?exists\s+)?(\w+)",
    re.IGNORECASE,
)


def discover_schemas(lines: list[str]) -> list[str]:
    """Return an ordered list of unique schema names found via search_path."""
    seen = []
    for line in lines:
        m = SP_RE.match(line)
        if m:
            name = m.group(1).lower()
            if name not in ("pg_catalog", "public") and name not in seen:
                seen.append(name)
    return seen


def is_global_setting(line: str) -> bool:
    """True for lines that are global postgres settings (not schema-specific)."""
    low = line.strip().lower()
    return low.startswith("set ") and not low.startswith("set search_path")


def _collect_schema_sections(lines: list[str], schemas: list[str]) -> tuple[list[str], list[str], dict[str, list[str]]]:
    preamble: list[str] = []
    postamble: list[str] = []
    buf: dict[str, list[str]] = {schema: [] for schema in schemas}

    current_schema: Optional[str] = None
    in_copy_block = False
    in_preamble = True
    in_postamble = False

    for line in lines:
        if in_copy_block:
            buf[current_schema].append(line)
            if line.rstrip("\r\n") == "\\.":
                in_copy_block = False
            continue

        if line.strip().lower().startswith("-- completed on"):
            in_postamble = True

        if in_postamble:
            postamble.append(line)
            continue

        m = SP_RE.match(line)
        if m:
            name = m.group(1).lower()
            if name in buf:
                current_schema = name
                in_preamble = False
                buf[current_schema].append(line)
                continue

        m_ddl = SCHEMA_DDL_RE.match(line.strip())
        if m_ddl:
            target = m_ddl.group(4).lower()
            if target in buf:
                current_schema = target
                in_preamble = False
                buf[current_schema].append(line)
                continue

        if in_preamble:
            preamble.append(line)
            continue

        if current_schema is None:
            preamble.append(line)
            continue

        buf[current_schema].append(line)

        if line.strip().lower().startswith("copy ") and "from stdin" in line.lower():
            in_copy_block = True

    return preamble, postamble, buf


def _write_dump(path: Path, preamble: list[str], schema_lines: list[str], postamble: list[str]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for l in preamble:
            f.write(l)
        for l in schema_lines:
            f.write(l)
        for l in postamble:
            f.write(l)


def split_dump(input_path: Path) -> None:
    lines = input_path.read_text(encoding="utf-8").splitlines(keepends=True)

    schemas = discover_schemas(lines)
    if len(schemas) < 2:
        print(f"Dump contains {len(schemas)} schema(s): {schemas or ['(none)']}. "
              "Nothing to split.")
        return
    if len(schemas) > 2:
        print(f"Dump contains {len(schemas)} schemas ({schemas}). "
              "This script only handles exactly 2. Aborting.")
        sys.exit(1)

    schema_a, schema_b = schemas[0], schemas[1]
    print(f"Found 2 schemas: '{schema_a}' and '{schema_b}'")

    preamble, postamble, buf = _collect_schema_sections(lines, schemas)

    # ── write output files ────────────────────────────────────────────────
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    out_a = OUTPUT_DIR / "dump.sql"
    out_b = OUTPUT_DIR / "dump_2.sql"

    _write_dump(out_a, preamble, buf[schema_a], postamble)
    _write_dump(out_b, preamble, buf[schema_b], postamble)

    # ── summary ───────────────────────────────────────────────────────────
    cnt_a = sum(1 for l in buf[schema_a] if l.strip().lower().startswith("create table"))
    cnt_b = sum(1 for l in buf[schema_b] if l.strip().lower().startswith("create table"))
    copy_a = sum(1 for l in buf[schema_a] if l.strip().lower().startswith("copy "))
    copy_b = sum(1 for l in buf[schema_b] if l.strip().lower().startswith("copy "))

    print(f"\n  {out_a}")
    print(f"    schema : {schema_a}")
    print(f"    tables : {cnt_a}")
    print(f"    COPY   : {copy_a}")

    print(f"\n  {out_b}")
    print(f"    schema : {schema_b}")
    print(f"    tables : {cnt_b}")
    print(f"    COPY   : {copy_b}")

    print("\nDone.")


def keep_only_schema(input_path: Path, schema_name: str, output_path: Optional[Path] = None) -> None:
    lines = input_path.read_text(encoding="utf-8").splitlines(keepends=True)
    schemas = discover_schemas(lines)
    schema_name = schema_name.lower()

    if schema_name not in schemas:
        print(f"Schema '{schema_name}' not found in dump. Found: {schemas or ['(none)']}")
        sys.exit(1)

    preamble, postamble, buf = _collect_schema_sections(lines, schemas)
    output_path = output_path or input_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _write_dump(output_path, preamble, buf[schema_name], postamble)

    table_count = sum(1 for l in buf[schema_name] if l.strip().lower().startswith("create table"))
    copy_count = sum(1 for l in buf[schema_name] if l.strip().lower().startswith("copy "))

    print(f"Kept only schema '{schema_name}' in {output_path}")
    print(f"  tables : {table_count}")
    print(f"  COPY   : {copy_count}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Split a PostgreSQL dump by schema or keep only one schema.")
    parser.add_argument("input_path", type=Path, nargs="?", default=INPUT_PATH, help="Path to dump.sql")
    parser.add_argument(
        "--keep-schema",
        help="Keep only this schema in the output dump instead of performing a 2-way split",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output file for --keep-schema mode. Defaults to overwriting the input dump.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if not args.input_path.exists():
        print(f"Error: {args.input_path} not found.")
        sys.exit(1)
    if args.keep_schema:
        keep_only_schema(args.input_path, args.keep_schema, args.output)
    else:
        if args.output is not None:
            print("--output is only supported together with --keep-schema.")
            sys.exit(1)
        split_dump(args.input_path)
