"""
SQL Dump Parser - PostgreSQL ALTER TABLE Format
Parses SQL dump files and extracts table structures and relationships.

Fixed issues vs previous version:
  1. PK regex: constraint names with hyphens/special chars (e.g. "Co-authorPK")
     were silently skipped because \w+ doesn't match hyphens.
  2. FK regex: same constraint-name issue.
  3. Column type: "character varying(N)" was collapsed to "character(N)".
     Fix: normalise "character varying" → "varchar" during column parse.
  4. Output path: updated to src/outputs/DB_as_json/ to match project layout.
  5. [NEW] CREATE TABLE / ALTER TABLE: quoted table names like "Abstract",
     "has_a_committee_co-chair" were silently skipped because the regex used
     \w+ which does not match quoted identifiers or hyphens.
     Fix: all table-name captures now accept both "quoted" and unquoted forms.
"""

import re
import json
import os
from typing import Dict, List, Optional
from collections import defaultdict


class SQLDumpParser:
    """Parser for SQL dump files with ALTER TABLE constraints."""

    def __init__(self, dump_file_path: str):
        self.dump_file_path = dump_file_path
        self.tables: Dict     = {}
        self.relationships    = defaultdict(list)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def parse(self):
        """Parse the dump file end-to-end."""
        print(f"Parsing SQL dump: {self.dump_file_path}")

        with open(self.dump_file_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()

        content = self._remove_comments(content)

        self._extract_create_tables(content)
        print(f"Found {len(self.tables)} tables from CREATE TABLE statements")

        self._parse_alter_primary_keys(content)
        self._parse_alter_foreign_keys(content)
        self._build_relationships()

        print(f"\n{'='*60}")
        print("Parsing Summary:")
        print(f"{'='*60}")
        for table_name, table_data in sorted(self.tables.items()):
            fk_count = sum(1 for col in table_data['columns'] if col['is_foreign_key'])
            pk_count = sum(1 for col in table_data['columns'] if col['is_primary_key'])
            print(f"  {table_name}: {len(table_data['columns'])} cols, {pk_count} PKs, {fk_count} FKs")

    def generate_json_outputs(self, output_dir: str = "src/outputs/DB_as_json"):
        """Write the four JSON output files."""
        os.makedirs(output_dir, exist_ok=True)

        # ── tables_structure.json ────────────────────────────────────
        tables_structure = {}
        for table_name, table_data in self.tables.items():
            tables_structure[table_name] = {
                'columns':            table_data['columns'],
                'primary_keys':       table_data['primary_keys'],
                'total_columns':      len(table_data['columns']),
                'total_primary_keys': len(table_data['primary_keys']),
                'total_foreign_keys': len(table_data['foreign_keys'])
            }

        self._write_json(output_dir, "tables_structure.json", tables_structure)

        # ── table_relationships.json ─────────────────────────────────
        relationships_output = {}
        for table_name in self.tables:
            links = self.relationships.get(table_name, [])
            relationships_output[table_name] = {
                'links_to_tables':    sorted(set(l['links_to'] for l in links)),
                'total_foreign_keys': len(links),
                'foreign_key_details': links
            }

        self._write_json(output_dir, "table_relationships.json", relationships_output)

        # ── relationship_summary.json ────────────────────────────────
        relationship_summary = {}
        for table_name in self.tables:
            related: set = set()
            for rel in self.relationships.get(table_name, []):
                related.add(rel['links_to'])
            for other, rels in self.relationships.items():
                for rel in rels:
                    if rel['links_to'] == table_name:
                        related.add(other)
            relationship_summary[table_name] = sorted(related)

        self._write_json(output_dir, "relationship_summary.json", relationship_summary)

        # ── summary.json ─────────────────────────────────────────────
        summary = {
            'total_tables':        len(self.tables),
            'total_primary_keys':  sum(len(t['primary_keys']) for t in self.tables.values()),
            'total_foreign_keys':  sum(len(t['foreign_keys']) for t in self.tables.values()),
            'total_relationships': sum(len(v) for v in self.relationships.values()),
            'tables':              sorted(self.tables.keys())
        }
        self._write_json(output_dir, "summary.json", summary)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _write_json(self, directory: str, filename: str, data):
        path = os.path.join(directory, filename)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"✓ Generated: {path}")

    def _remove_comments(self, content: str) -> str:
        content = re.sub(r'--[^\n]*\n', '\n', content)
        content = re.sub(r'/\*.*?\*/', '', content, flags=re.DOTALL)
        return content

    @staticmethod
    def _tbl(m, base: int = 0) -> str:
        """Extract table name from a match: group(base) quoted or group(base+1) unquoted."""
        return m.group(base + 1) or m.group(base + 2)

    # ------------------------------------------------------------------
    # CREATE TABLE
    # ------------------------------------------------------------------

    def _extract_create_tables(self, content: str):
        """
        Extract CREATE TABLE statements.
        Handles both quoted identifiers ("Table_Name", "has-hyphen")
        and plain unquoted names.  Also handles optional schema prefix.
        """
        pattern = (
            r'CREATE\s+TABLE\s+'
            r'(?:IF\s+NOT\s+EXISTS\s+)?'
            r'(?:(?:"[^"]+"|\w+)\.)?'          # optional schema. (quoted or unquoted)
            r'(?:"([^"]+)"|(\w+))'             # table name: quoted OR unquoted
            r'\s*\((.*?)\)\s*;'                # body
        )
        for match in re.finditer(pattern, content, re.IGNORECASE | re.DOTALL):
            table_name = match.group(1) or match.group(2)   # quoted wins
            table_body = match.group(3).strip()
            columns    = self._parse_columns(table_body)
            self.tables[table_name] = {
                'columns':      columns,
                'primary_keys': [],
                'foreign_keys': []
            }

    def _parse_columns(self, table_body: str) -> List[Dict]:
        columns = []
        for line in self._split_by_comma(table_body):
            line = line.strip()
            if not line:
                continue
            if re.match(
                r'(?:CONSTRAINT|PRIMARY\s+KEY|FOREIGN\s+KEY|UNIQUE|CHECK|KEY)\b',
                line, re.IGNORECASE
            ):
                continue
            col = self._parse_column_definition(line)
            if col:
                columns.append(col)
        return columns

    def _parse_column_definition(self, col_def: str) -> Optional[Dict]:
        parts = col_def.split(None, 1)
        if len(parts) < 2:
            return None

        col_name  = self._clean_identifier(parts[0])
        remainder = parts[1].strip()

        remainder = re.sub(r'character\s+varying', 'varchar',   remainder, flags=re.IGNORECASE)
        remainder = re.sub(r'double\s+precision',  'double',    remainder, flags=re.IGNORECASE)
        remainder = re.sub(
            r'timestamp\s+(?:with(?:out)?\s+time\s+zone)',
            'timestamp', remainder, flags=re.IGNORECASE
        )

        type_match = re.match(r'(\w+)(?:\(([^)]+)\))?', remainder)
        if not type_match:
            return None

        base_type = type_match.group(1)
        precision = type_match.group(2)
        data_type = f"{base_type}({precision})" if precision else base_type

        col_def_upper     = col_def.upper()
        is_nullable       = 'NOT NULL' not in col_def_upper
        is_auto_increment = any(
            x in col_def_upper
            for x in ('SERIAL', 'AUTO_INCREMENT', 'AUTOINCREMENT', 'IDENTITY')
        )

        default_value = None
        dv_match = re.search(
            r"DEFAULT\s+('([^']*)'|\"([^\"]*)\"|([^\s,]+))",
            remainder, re.IGNORECASE
        )
        if dv_match:
            default_value = dv_match.group(2) or dv_match.group(3) or dv_match.group(4)

        return {
            'name':                  col_name,
            'data_type':             data_type,
            'is_primary_key':        False,
            'is_foreign_key':        False,
            'is_nullable':           is_nullable,
            'is_auto_increment':     is_auto_increment,
            'default_value':         default_value,
            'foreign_key_reference': None
        }

    # ------------------------------------------------------------------
    # ALTER TABLE — PRIMARY KEY
    # ------------------------------------------------------------------

    def _parse_alter_primary_keys(self, content: str):
        """
        Matches quoted and unquoted table names:
          ALTER TABLE ONLY "Abstract"   ADD CONSTRAINT "AbstractPK" PRIMARY KEY ("ID");
          ALTER TABLE ONLY plain_table  ADD CONSTRAINT PlainPK      PRIMARY KEY (id);
        """
        pattern = (
            r'ALTER\s+TABLE\s+(?:ONLY\s+)?'
            r'(?:(?:"[^"]+"|[\w$]+)\s*\.\s*)?'                   # optional schema. prefix
            r'(?:"([^"]+)"|([\w$]+))'                            # table name: quoted OR unquoted
            r'\s+ADD\s+CONSTRAINT\s+(?:"([^"]+)"|([\w$-]+))'     # constraint name
            r'\s+PRIMARY\s+KEY\s*\(([^)]+)\)'                    # column list
        )
        for match in re.finditer(pattern, content, re.IGNORECASE | re.DOTALL):
            table_name      = match.group(1) or match.group(2)
            constraint_name = match.group(3) or match.group(4)
            pk_cols_raw     = match.group(5)
            pk_columns      = [self._clean_identifier(c) for c in pk_cols_raw.split(',')]

            if table_name not in self.tables:
                print(f"  [WARN] PK for unknown table: {table_name}")
                continue

            self.tables[table_name]['primary_keys'] = pk_columns
            for col in self.tables[table_name]['columns']:
                if col['name'] in pk_columns:
                    col['is_primary_key'] = True

            print(f"  PK  {table_name}.({', '.join(pk_columns)})  [{constraint_name}]")

    # ------------------------------------------------------------------
    # ALTER TABLE — FOREIGN KEY
    # ------------------------------------------------------------------

    def _parse_alter_foreign_keys(self, content: str):
        """
        Matches quoted and unquoted table and reference names:
          ALTER TABLE ONLY "Abstract"
              ADD CONSTRAINT "AbstractFK1" FOREIGN KEY (col) REFERENCES "Paper"("ID");
        """
        pattern = (
            r'ALTER\s+TABLE\s+(?:ONLY\s+)?'
            r'(?:(?:"[^"]+"|[\w$]+)\s*\.\s*)?'                    # optional schema. prefix (source)
            r'(?:"([^"]+)"|([\w$]+))'                             # source table: quoted OR unquoted
            r'\s+ADD\s+CONSTRAINT\s+(?:"([^"]+)"|([\w$-]+))'      # constraint name
            r'\s+FOREIGN\s+KEY\s*\(([^)]+)\)'                     # FK column(s)
            r'\s+REFERENCES\s+'
            r'(?:(?:"[^"]+"|[\w$]+)\s*\.\s*)?'                    # optional schema. prefix (ref)
            r'(?:"([^"]+)"|([\w$]+))\s*\(([^)]+)\)'              # ref table + col(s)
            r'(?:\s+(?:ON\s+(?:DELETE|UPDATE))\s+'
            r'(?:CASCADE|SET\s+NULL|SET\s+DEFAULT|RESTRICT|NO\s+ACTION))*'
        )
        for match in re.finditer(pattern, content, re.IGNORECASE | re.DOTALL):
            table_name      = match.group(1) or match.group(2)
            constraint_name = match.group(3) or match.group(4)
            fk_column       = self._clean_identifier(match.group(5))
            ref_table       = match.group(6) or match.group(7)
            ref_column      = self._clean_identifier(match.group(8))

            if table_name not in self.tables:
                print(f"  [WARN] FK for unknown table: {table_name}")
                continue

            fk_info = {
                'column':          fk_column,
                'ref_table':       ref_table,
                'ref_column':      ref_column,
                'constraint_name': constraint_name
            }
            self.tables[table_name]['foreign_keys'].append(fk_info)

            for col in self.tables[table_name]['columns']:
                if col['name'] == fk_column:
                    col['is_foreign_key']        = True
                    col['foreign_key_reference'] = {
                        'table':  ref_table,
                        'column': ref_column
                    }

            print(f"  FK  {table_name}.{fk_column} → {ref_table}.{ref_column}  [{constraint_name}]")

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------

    def _build_relationships(self):
        for table_name, table_data in self.tables.items():
            for fk in table_data['foreign_keys']:
                self.relationships[table_name].append({
                    'links_to':          fk['ref_table'],
                    'via_column':        fk['column'],
                    'references_column': fk['ref_column'],
                    'constraint_name':   fk['constraint_name']
                })

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _split_by_comma(self, text: str) -> List[str]:
        parts, current, depth = [], [], 0
        for char in text:
            if char == '(':
                depth += 1
            elif char == ')':
                depth -= 1
            elif char == ',' and depth == 0:
                parts.append(''.join(current).strip())
                current = []
                continue
            current.append(char)
        if current:
            parts.append(''.join(current).strip())
        return parts

    def _clean_identifier(self, identifier: str) -> str:
        return identifier.strip().strip('`[]"\' ')


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def main():
    dump_file  = "src/inputs/database/dump_new.sql"
    output_dir = "src/outputs/DB_as_json"

    if not os.path.exists(dump_file):
        print(f"Error: dump file not found: {dump_file}")
        return

    print("=" * 60)
    print("SQL Dump Parser — PostgreSQL ALTER TABLE Format")
    print("=" * 60)

    parser = SQLDumpParser(dump_file)
    parser.parse()
    parser.generate_json_outputs(output_dir)

    print("\n" + "=" * 60)
    print("Parsing complete.")
    print("=" * 60)


if __name__ == "__main__":
    main()