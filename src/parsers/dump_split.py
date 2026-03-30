
"""
split_dump.py
-------------
Analyse a PostgreSQL dump file.  If it contains exactly two schemas, split it
into two self-contained dump files – one per schema.  If only one schema is
found the script prints a message and exits.

Input : src/inputs/database/dump.sql
Output: src/inputs/database/dump.sql   (first schema discovered)
        src/inputs/database/dump_2.sql (second schema discovered)
"""

import re
import sys
from pathlib import Path

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

    # Buffers: preamble (global header), per-schema lines
    preamble: list[str]        = []
    postamble: list[str]       = []
    buf: dict[str, list[str]]  = {schema_a: [], schema_b: []}

    current_schema: str | None = None
    in_copy_block = False          # inside COPY … FROM stdin data
    in_preamble   = True           # before any schema-specific content
    in_postamble  = False          # after final "-- completed on …" line

    for line in lines:
        # ── detect COPY block boundaries ──────────────────────────────
        if in_copy_block:
            # COPY data ends with a line that is exactly `\.` (+ newline)
            buf[current_schema].append(line)
            if line.rstrip("\r\n") == "\\.":
                in_copy_block = False
            continue

        # ── detect postamble (dump-complete footer) ───────────────────
        if line.strip().lower().startswith("-- completed on"):
            in_postamble = True

        if in_postamble:
            postamble.append(line)
            continue

        # ── detect search_path switch ─────────────────────────────────
        m = SP_RE.match(line)
        if m:
            name = m.group(1).lower()
            if name in buf:
                current_schema = name
                in_preamble = False
                buf[current_schema].append(line)
                continue

        # ── detect CREATE / DROP SCHEMA ───────────────────────────────
        m_ddl = SCHEMA_DDL_RE.match(line.strip())
        if m_ddl:
            target = m_ddl.group(4).lower()
            if target in buf:
                in_preamble = False
                buf[target].append(line)
                continue

        # ── TOC comment lines often name the schema ───────────────────
        # e.g.  "-- name: airport; type: table data; schema: mondial_rel;"
        toc_schema = None
        if line.strip().startswith("--") and "schema:" in line.lower():
            for s in (schema_a, schema_b):
                if f"schema: {s}" in line.lower():
                    toc_schema = s
                    break

        # ── global preamble (SET statements before any schema content) ─
        if in_preamble:
            preamble.append(line)
            continue

        # ── route the line to the active schema ───────────────────────
        if current_schema is None:
            # Shouldn't happen after preamble, but be safe
            preamble.append(line)
            continue

        buf[current_schema].append(line)

        # ── detect start of COPY block ────────────────────────────────
        if line.strip().lower().startswith("copy ") and "from stdin" in line.lower():
            in_copy_block = True

    # ── write output files ────────────────────────────────────────────────
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    out_a = OUTPUT_DIR / "dump.sql"
    out_b = OUTPUT_DIR / "dump_2.sql"

    def write_dump(path: Path, schema_name: str, schema_lines: list[str]):
        with open(path, "w", encoding="utf-8") as f:
            # global header
            for l in preamble:
                f.write(l)
            # schema-specific content
            for l in schema_lines:
                f.write(l)
            # footer
            for l in postamble:
                f.write(l)

    write_dump(out_a, schema_a, buf[schema_a])
    write_dump(out_b, schema_b, buf[schema_b])

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


if __name__ == "__main__":
    if not INPUT_PATH.exists():
        print(f"Error: {INPUT_PATH} not found.")
        sys.exit(1)
    split_dump(INPUT_PATH)