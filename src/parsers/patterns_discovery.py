"""
Database Patterns Discovery
Discovers structural patterns in the database:
- SE: Strong Entity tables
- SEw: Weak Entity tables
- SR: Pure many-to-many bridge tables
- SRR: Bridge tables with extra attributes
- SE_SH: Strong Entity that is also a subclass (inherited PK from parent)
"""

import json
import os
from typing import Any, Dict, List, Set


def discover_db_patterns(
    tables_structure_file: str = "src/outputs/DB_as_json/tables_structure.json",
    relationships_file: str = "src/outputs/DB_as_json/table_relationships.json"
) -> Dict[str, str]:
    """
    Discover structural patterns in the database.

    Returns:
        Flat dict mapping each table name to its pattern: { "table_name": "SE" | "SEw" | "SR" | "SRR" | "SE_SH" }
    """

    print("Discovering database patterns...")

    with open(tables_structure_file, 'r') as f:
        tables_structure = json.load(f)

    with open(relationships_file, 'r') as f:
        relationships_data = json.load(f)

    # Build schema structures
    schema = {}
    referenced_by = {}  # (table, column) -> [(referencing_table, referencing_column)]

    for table_name, table_info in tables_structure.items():
        columns  = table_info['columns']
        pk_cols  = set(table_info['primary_keys'])
        col_metadata = {}
        fk_cols  = set()
        fk_map   = {}

        for col in columns:
            col_name = col['name']
            col_metadata[col_name] = {
                'data_type': col['data_type'],
                'is_pk':     col['is_primary_key'],
                'is_fk':     col['is_foreign_key'],
                'nullable':  col['is_nullable']
            }
            if col['is_foreign_key'] and col.get('foreign_key_reference'):
                fk_cols.add(col_name)
                ref = col['foreign_key_reference']
                fk_map[col_name] = {'ref_table': ref['table'], 'ref_column': ref['column']}
                key = (ref['table'], ref['column'])
                referenced_by.setdefault(key, []).append((table_name, col_name))

        schema[table_name] = {
            'columns': col_metadata,
            'pk':      pk_cols,
            'fk_cols': fk_cols,
            'fk_map':  fk_map
        }

    # Classify each table
    table_patterns: Dict[str, str] = {}

    for table_name, info in schema.items():
        all_cols = list(info['columns'].keys())
        pk_cols  = list(info['pk'])
        fk_cols  = list(info['fk_cols'])
        attr_cols = [c for c in all_cols if c not in pk_cols and c not in fk_cols]

        pk_referenced  = any(referenced_by.get((table_name, c)) for c in pk_cols)
        all_pk_are_fk  = bool(pk_cols) and all(c in fk_cols for c in pk_cols)
        some_pk_fk     = any(c in fk_cols for c in pk_cols)
        some_pk_non_fk = any(c not in fk_cols for c in pk_cols)

        # SEw — composite PK where some parts are FK and some are not, no extra attributes
        is_weak = (
            len(pk_cols) >= 2 and
            some_pk_fk and
            some_pk_non_fk and
            len(attr_cols) == 0 and
            not pk_referenced
        )

        # SR / SRR — all columns are PKs and all PKs are FKs (pure junction)
        is_bridge = (
            len(pk_cols) >= 2 and
            len(pk_cols) == len(all_cols) and
            all_pk_are_fk and
            not pk_referenced
        )

        if is_weak:
            table_patterns[table_name] = "SEw"
        elif is_bridge:
            table_patterns[table_name] = "SR" if len(attr_cols) == 0 else "SRR"
        else:
            table_patterns[table_name] = "SE"

    # Detect SE_SH — SE tables whose sole PK column is also an FK to another SE table's PK
    for table_name, info in schema.items():
        if table_patterns.get(table_name) != "SE":
            continue

        pk_cols = list(info['pk'])
        fk_map  = info['fk_map']

        for pk_col in pk_cols:
            if pk_col not in fk_map:
                continue
            parent_table  = fk_map[pk_col]['ref_table']
            parent_pk_col = fk_map[pk_col]['ref_column']

            if table_patterns.get(parent_table) not in {"SE", "SE_SH"}:
                continue

            parent_info = schema.get(parent_table)
            if not parent_info:
                continue

            if parent_pk_col in parent_info['pk']:
                table_patterns[table_name] = "SE_SH"
                break

    return table_patterns


def save_patterns(table_patterns: Dict[str, str], output_dir: str = "src/memory/"):
    """Save the flat pattern map to patterns.json"""
    os.makedirs(output_dir, exist_ok=True)
    output_file = os.path.join(output_dir, "patterns_final.json")
    with open(output_file, 'w') as f:
        json.dump(table_patterns, f, indent=2)
    print(f"✓ Patterns saved to: {output_file}")
    return output_file


if __name__ == "__main__":
    table_patterns = discover_db_patterns()

    # Summary
    from collections import Counter
    counts = Counter(table_patterns.values())
    print(f"\n{'='*50}")
    print("DATABASE PATTERNS SUMMARY")
    print(f"{'='*50}")
    for pattern, count in sorted(counts.items()):
        print(f"  {pattern:<8} {count} tables")
    print(f"  {'TOTAL':<8} {len(table_patterns)} tables")

    print(f"\n{'='*50}")
    for pattern in ["SE", "SE_SH", "SEw", "SR", "SRR"]:
        tables = sorted(t for t, p in table_patterns.items() if p == pattern)
        if tables:
            print(f"\n{pattern} ({len(tables)}):")
            for t in tables:
                print(f"  - {t}")

    save_patterns(table_patterns)