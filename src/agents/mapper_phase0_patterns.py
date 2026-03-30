"""
Phase 0 – SQL Dump Parser + Constraint Completion
==================================================
Reads a PostgreSQL dump.sql (DDL + COPY data), extracts the schema and a
sample of up to 5 rows per table, then asks the LLM to identify missing
constraints (PKs, FKs, UNIQUE) using semantic reasoning.

The LLM returns missing constraints as standard ALTER TABLE / ADD CONSTRAINT
SQL statements in the same dialect as the dump.  Phase 0 merges those into
the original dump and writes the enriched file to:
    src/inputs/database/dump_new.sql

Pipeline
--------
Phase 1 – Parse:  Extract DDL blocks, existing constraints, and ≤5 sample
          rows per table directly from the .sql dump file.

Phase 2 – Analyse (LLM):  Send the compact schema + samples to the LLM.
          The LLM reasons over all patterns (regular FK, inheritance FK,
          junction/bridge FK, missing PK, implicit UNIQUE) and returns ONLY
          the constraints that are genuinely missing — it does NOT invent
          constraints when the schema looks complete.

Phase 3 – Merge:  Inject the new ALTER TABLE statements into the dump just
          before the first existing ALTER TABLE block (or at the end if none
          exist) and write dump_new.sql.

Reads  : src/inputs/database/dump.sql
Writes : src/inputs/database/dump_new.sql
"""

import re
import os
import sys
import requests
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.llm_config import LLMConfig, SELECTED_PROVIDER

# ===== PATHS =====
INPUT_DUMP  = "src/inputs/database/dump.sql"
OUTPUT_DUMP = "src/inputs/database/dump_new.sql"
MAX_SAMPLE_ROWS = 5


# =====================================================================
#  Shared LLM helper  (identical interface to other phases)
# =====================================================================
class LLMClient:
    """Thin wrapper around multi-provider LLM calls."""

    def __init__(self, provider: str = SELECTED_PROVIDER):
        self.provider = provider
        self.config   = LLMConfig.get_config(provider)

    def strip_thinking_tags(self, text: str) -> str:
        return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

    def call(self, prompt: str, max_tokens: int = 4096) -> str:
        if self.provider == "claude":
            return self._claude(prompt, max_tokens)
        elif self.provider == "gemini":
            return self._gemini(prompt, max_tokens)
        else:
            return self._openai_compat(prompt, max_tokens)

    def _openai_compat(self, prompt: str, max_tokens: int) -> str:
        headers = {
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {self.config['api_key']}",
        }
        data = {
            "model":       self.config["model_name"],
            "messages":    [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens":  max_tokens,
        }
        resp = requests.post(self.config["api_url"], headers=headers, json=data)
        if resp.status_code != 200:
            raise Exception(f"API error {resp.status_code}: {resp.text}")
        content = resp.json()["choices"][0]["message"]["content"]
        if self.provider == "groq":
            content = self.strip_thinking_tags(content)
        return content

    def _claude(self, prompt: str, max_tokens: int) -> str:
        headers = {
            "Content-Type":      "application/json",
            "x-api-key":         self.config["api_key"],
            "anthropic-version": "2023-06-01",
        }
        data = {
            "model":       self.config["model_name"],
            "max_tokens":  max_tokens,
            "messages":    [{"role": "user", "content": prompt}],
            "temperature": 0.1,
        }
        resp = requests.post(self.config["api_url"], headers=headers, json=data)
        if resp.status_code != 200:
            raise Exception(f"Claude API error {resp.status_code}: {resp.text}")
        return resp.json()["content"][0]["text"]

    def _gemini(self, prompt: str, max_tokens: int) -> str:
        url = (
            f"{self.config['api_url']}/{self.config['model_name']}"
            f":generateContent?key={self.config['api_key']}"
        )
        data = {
            "contents":         [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.1, "maxOutputTokens": max_tokens},
        }
        resp = requests.post(url, headers={"Content-Type": "application/json"}, json=data)
        if resp.status_code != 200:
            raise Exception(f"Gemini API error {resp.status_code}: {resp.text}")
        return resp.json()["candidates"][0]["content"]["parts"][0]["text"]


# =====================================================================
#  Phase 1 – SQL Dump Parser
# =====================================================================
class DumpParser:
    """
    Parses a PostgreSQL dump file and extracts:
      - CREATE TABLE blocks (with full column definitions)
      - Existing constraint statements (ALTER TABLE … ADD CONSTRAINT …)
      - Up to MAX_SAMPLE_ROWS rows per table from COPY … FROM stdin blocks
      - Schema name (if SET search_path is present)

    The parser is intentionally permissive: it handles pg_dump output,
    hand-written DDL, dumps with or without schemas, mixed quoting styles,
    and inline vs ALTER-based constraints.
    """

    # ── regex patterns ────────────────────────────────────────────────

    # CREATE TABLE [schema.]name (
    _RE_CREATE = re.compile(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
        r"(?:(?P<schema>[`\"\w]+)\s*\.\s*)?"
        r"(?P<table>[`\"\w]+)\s*\(",
        re.IGNORECASE,
    )

    # ALTER TABLE … ADD CONSTRAINT …   (single statement ending with ;)
    _RE_ALTER = re.compile(
        r"ALTER\s+TABLE\s+.*?ADD\s+CONSTRAINT\s+.*?;",
        re.IGNORECASE | re.DOTALL,
    )

    # COPY [schema.]table (col, …) FROM stdin;
    _RE_COPY = re.compile(
        r"COPY\s+(?:[\w\"]+\s*\.\s*)?(?P<table>[\w\"]+)\s*"
        r"\((?P<cols>[^)]+)\)\s+FROM\s+stdin\s*;",
        re.IGNORECASE,
    )

    # SET search_path = schema, pg_catalog;
    _RE_SEARCH_PATH = re.compile(
        r"SET\s+search_path\s*=\s*(?P<schema>[\w\"]+)",
        re.IGNORECASE,
    )

    def __init__(self, sql_text: str):
        self.sql   = sql_text
        self.lines = sql_text.splitlines()

    # ── public entry point ────────────────────────────────────────────

    def parse(self) -> Dict:
        """
        Returns a dict:
        {
          "schema": str | None,
          "tables": {
              "table_name": {
                  "ddl":         str,            # full CREATE TABLE block
                  "columns":     [(name, type_and_constraints)],
                  "inline_pks":  [col_name],     # found inside CREATE TABLE
                  "inline_fks":  [{"col", "ref_table", "ref_col", "constraint_name"}],
                  "inline_uniq": [[col_name, …]],
                  "sample_rows": [[val, …], …],  # ≤ MAX_SAMPLE_ROWS
                  "col_names":   [str],           # column order from COPY header
              }
          },
          "existing_constraints": [str],  # raw ALTER TABLE … ADD CONSTRAINT SQL
        }
        """
        schema  = self._find_schema()
        tables  = self._parse_create_tables()
        self._parse_copy_data(tables)
        constraints = self._parse_alter_constraints()

        return {
            "schema":               schema,
            "tables":               tables,
            "existing_constraints": constraints,
        }

    # ── schema detection ──────────────────────────────────────────────

    def _find_schema(self) -> Optional[str]:
        m = self._RE_SEARCH_PATH.search(self.sql)
        if m:
            return m.group("schema").strip('"').strip("'")
        return None

    # ── CREATE TABLE parsing ──────────────────────────────────────────

    def _parse_create_tables(self) -> Dict:
        tables: Dict = {}
        pos = 0
        text = self.sql

        while True:
            m = self._RE_CREATE.search(text, pos)
            if not m:
                break

            tname = m.group("table").strip('"').strip("`").strip("'")
            body_start = m.end()  # position just after the opening '('

            # Find matching closing ')' respecting nesting
            body, body_end = self._extract_parens_body(text, body_start - 1)
            if body is None:
                pos = body_start
                continue

            ddl = text[m.start():body_end].rstrip().rstrip(";") + ";"

            cols, inline_pks, inline_fks, inline_uniq = self._parse_columns(body)

            tables[tname] = {
                "ddl":         ddl,
                "columns":     cols,
                "inline_pks":  inline_pks,
                "inline_fks":  inline_fks,
                "inline_uniq": inline_uniq,
                "sample_rows": [],
                "col_names":   [c[0] for c in cols],
            }

            pos = body_end

        return tables

    def _extract_parens_body(self, text: str, open_pos: int) -> Tuple[Optional[str], int]:
        """
        Given the position of an opening '(', return the content inside
        (exclusive) and the position just after the matching ')'.
        """
        depth  = 0
        i      = open_pos
        length = len(text)
        while i < length:
            ch = text[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return text[open_pos + 1:i], i + 1
            i += 1
        return None, open_pos

    def _parse_columns(self, body: str):
        """
        Parse a CREATE TABLE body.  Returns:
          columns      : list of (col_name, type_str)
          inline_pks   : list of PK column names
          inline_fks   : list of FK dicts
          inline_uniq  : list of lists of UNIQUE column names
        """
        columns     : List[Tuple[str, str]] = []
        inline_pks  : List[str]             = []
        inline_fks  : List[Dict]            = []
        inline_uniq : List[List[str]]       = []

        # Split on commas that are NOT inside parentheses
        parts = self._split_top_level(body)

        for part in parts:
            part = part.strip()
            if not part:
                continue

            upper = part.upper().lstrip()

            # ── table-level PRIMARY KEY ──
            if upper.startswith("PRIMARY KEY"):
                cols = self._extract_col_list(part)
                inline_pks.extend(cols)
                continue

            # ── table-level FOREIGN KEY ──
            fk_m = re.match(
                r"(?:CONSTRAINT\s+(?P<cname>[\w\"]+)\s+)?"
                r"FOREIGN\s+KEY\s*\((?P<cols>[^)]+)\)\s+"
                r"REFERENCES\s+(?:[\w\"]+\.)?(?P<rtable>[\w\"]+)"
                r"\s*\((?P<rcols>[^)]+)\)",
                part, re.IGNORECASE,
            )
            if fk_m:
                fk_cols  = [c.strip().strip('"') for c in fk_m.group("cols").split(",")]
                ref_cols = [c.strip().strip('"') for c in fk_m.group("rcols").split(",")]
                cname    = (fk_m.group("cname") or "").strip('"')
                for fc, rc in zip(fk_cols, ref_cols):
                    inline_fks.append({
                        "col":             fc,
                        "ref_table":       fk_m.group("rtable").strip('"'),
                        "ref_col":         rc,
                        "constraint_name": cname,
                    })
                continue

            # ── table-level UNIQUE ──
            if upper.startswith("UNIQUE") or re.match(
                r"CONSTRAINT\s+\S+\s+UNIQUE", part, re.IGNORECASE
            ):
                cols = self._extract_col_list(part)
                if cols:
                    inline_uniq.append(cols)
                continue

            # ── table-level CHECK / other ──
            if re.match(r"(CONSTRAINT\s+\S+\s+)?(CHECK|EXCLUDE)", part, re.IGNORECASE):
                continue

            # ── regular column definition ──
            col_m = re.match(r'(?P<name>["`\w]+)\s+(?P<type>.+)', part, re.IGNORECASE)
            if not col_m:
                continue

            col_name = col_m.group("name").strip('"').strip("`")
            col_type = col_m.group("type").strip()

            # Inline PRIMARY KEY on column
            if re.search(r"\bPRIMARY\s+KEY\b", col_type, re.IGNORECASE):
                inline_pks.append(col_name)

            # Inline REFERENCES on column
            ref_m = re.search(
                r"REFERENCES\s+(?:[\w\"]+\.)?(?P<rtable>[\w\"]+)"
                r"\s*\((?P<rcol>[^)]+)\)",
                col_type, re.IGNORECASE,
            )
            if ref_m:
                inline_fks.append({
                    "col":             col_name,
                    "ref_table":       ref_m.group("rtable").strip('"'),
                    "ref_col":         ref_m.group("rcol").strip().strip('"'),
                    "constraint_name": "",
                })

            columns.append((col_name, col_type))

        return columns, inline_pks, inline_fks, inline_uniq

    def _split_top_level(self, text: str) -> List[str]:
        """Split text on commas that are not inside parentheses."""
        parts: List[str] = []
        depth   = 0
        current : List[str] = []
        for ch in text:
            if ch == "(":
                depth += 1
                current.append(ch)
            elif ch == ")":
                depth -= 1
                current.append(ch)
            elif ch == "," and depth == 0:
                parts.append("".join(current))
                current = []
            else:
                current.append(ch)
        if current:
            parts.append("".join(current))
        return parts

    def _extract_col_list(self, text: str) -> List[str]:
        """Extract column names from the first parenthesised list in text."""
        m = re.search(r"\(([^)]+)\)", text)
        if not m:
            return []
        return [c.strip().strip('"') for c in m.group(1).split(",")]

    # ── COPY data parsing ─────────────────────────────────────────────

    def _parse_copy_data(self, tables: Dict):
        """
        Find all COPY … FROM stdin blocks and add up to MAX_SAMPLE_ROWS
        rows to the matching table entry.
        """
        copy_pattern = re.compile(
            r"COPY\s+(?:[\w\"]+\s*\.\s*)?(?P<table>[\w\"]+)\s*"
            r"\((?P<cols>[^)]+)\)\s+FROM\s+stdin\s*;\n"
            r"(?P<data>.*?)"
            r"^\\\.",
            re.IGNORECASE | re.DOTALL | re.MULTILINE,
        )

        for m in copy_pattern.finditer(self.sql):
            tname     = m.group("table").strip('"')
            cols_raw  = m.group("cols")
            data_text = m.group("data")

            col_names = [c.strip().strip('"') for c in cols_raw.split(",")]
            rows: List[List[str]] = []

            for line in data_text.splitlines():
                line = line.strip()
                if not line:
                    continue
                rows.append(line.split("\t"))
                if len(rows) >= MAX_SAMPLE_ROWS:
                    break

            # Match table name case-insensitively
            matched_key = self._match_table_key(tname, tables)
            if matched_key:
                tables[matched_key]["sample_rows"] = rows
                tables[matched_key]["col_names"]   = col_names

    def _match_table_key(self, name: str, tables: Dict) -> Optional[str]:
        if name in tables:
            return name
        lower = name.lower()
        for k in tables:
            if k.lower() == lower:
                return k
        return None

    # ── ALTER TABLE constraint parsing ────────────────────────────────

    def _parse_alter_constraints(self) -> List[str]:
        """
        Extract all ALTER TABLE … ADD CONSTRAINT … ; blocks from the dump.
        Handles multi-line statements.
        """
        constraints: List[str] = []
        pattern = re.compile(
            r"ALTER\s+TABLE\s+(?:ONLY\s+)?(?:[\w\"]+\s*\.\s*)?[\w\"\s]+"
            r"ADD\s+CONSTRAINT\s+.+?;",
            re.IGNORECASE | re.DOTALL,
        )
        for m in pattern.finditer(self.sql):
            stmt = re.sub(r"\s+", " ", m.group(0)).strip()
            constraints.append(stmt)
        return constraints


# =====================================================================
#  Phase 2 – LLM Constraint Analysis
# =====================================================================
class ConstraintAnalysisAgent:
    """
    Sends the full schema summary (DDL skeletons + sample data + existing
    constraints) to the LLM in a single prompt and asks it to identify
    any missing PK, FK, or UNIQUE constraints.

    The LLM is instructed to:
      - Return missing constraints as standard SQL ALTER TABLE statements
        in the same dialect/quoting style as the dump.
      - Return an empty list if the schema looks complete.
      - Never invent constraints that are not semantically justified.
    """

    def __init__(self, llm: LLMClient):
        self.llm = llm

    def build_prompt(self, parsed: Dict) -> str:
        tables          = parsed["tables"]
        existing_csts   = parsed["existing_constraints"]
        schema          = parsed["schema"] or ""
        schema_prefix   = f"{schema}." if schema else ""
        all_table_names = sorted(tables.keys())

        # ── Build compact schema block ──────────────────────────────
        schema_lines: List[str] = []
        for tname, info in tables.items():
            schema_lines.append(f"TABLE: {tname}")

            # columns
            pk_set = set(info["inline_pks"])
            fk_set = {fk["col"] for fk in info["inline_fks"]}
            for col_name, col_type in info["columns"]:
                tags = []
                if col_name in pk_set:
                    tags.append("PK")
                for fk in info["inline_fks"]:
                    if fk["col"] == col_name:
                        tags.append(f"FK->{fk['ref_table']}.{fk['ref_col']}")
                tag_str = "  [" + ", ".join(tags) + "]" if tags else ""
                schema_lines.append(f"  {col_name}  {col_type}{tag_str}")

            # inline unique
            for uq in info["inline_uniq"]:
                schema_lines.append(f"  UNIQUE({', '.join(uq)})")

            # sample data
            if info["sample_rows"]:
                header = " | ".join(info["col_names"])
                schema_lines.append(f"  -- sample ({min(len(info['sample_rows']), MAX_SAMPLE_ROWS)} rows):")
                schema_lines.append(f"  -- {header}")
                for row in info["sample_rows"][:MAX_SAMPLE_ROWS]:
                    schema_lines.append("  -- " + " | ".join(row))
            else:
                schema_lines.append("  -- (no data / empty table)")
            schema_lines.append("")

        schema_block = "\n".join(schema_lines)

        # ── Build existing constraints block ────────────────────────
        if existing_csts:
            existing_block = "\n".join(existing_csts)
        else:
            existing_block = "(none found in dump)"

        # ── Prompt ──────────────────────────────────────────────────
        return f"""You are a senior database architect and schema expert.

Below is the complete schema of a PostgreSQL database, extracted from a dump file.
For each table you have: column names, their types, any already-declared PK/FK/UNIQUE
constraints (marked [PK] or [FK->table.col]), and up to {MAX_SAMPLE_ROWS} sample rows.

YOUR TASK
---------
Identify constraints that are MISSING from this schema.  Use your semantic
understanding of the table and column names — not just syntactic pattern matching.

You must reason about ALL five structural patterns that can appear in a relational
schema.  Each pattern has specific constraint signatures:

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PATTERN 1 — SE  (Strong Entity)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
A regular standalone table with its own identity.
Signature: single-column PK (usually "id"), own attributes.

Missing constraint to look for:
  a) No PK declared at all → add PRIMARY KEY on the id column.
  b) A non-PK column whose name or meaning clearly refers to another table
     (e.g. "author", "conference_id", "submitted_by", "reviewer") → add FK.
  c) A natural-key column with no UNIQUE (e.g. email, username, DOI) → add UNIQUE.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PATTERN 2 — SEw  (Weak Entity)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
A table that depends on a parent entity for its existence and identity.
Signature: its PK is a partial key — one PK column is also a FK to the
parent entity, plus at least one discriminator column.
Example: "order_item(order_id [PK,FK->orders], line_no [PK], quantity)"

Missing constraints to look for:
  a) The PK column that should also be a FK to the owning entity is not
     marked as FK → add the FK on that PK column.
  b) The composite PK itself may be missing → add PRIMARY KEY(col1, col2).
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PATTERN 3 — SR  (Simple Relationship — Pure Bridge Table)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
A junction/bridge table that encodes a many-to-many relationship.
It has NO surrogate id of its own.
Signature: composite PK made of 2 (or more) columns, each of which is
ALSO a FK to a different entity table.  The table carries no own attributes
(or very few).
Examples: "paper_reviewer(pid [PK], rid [PK])",
          "author_conference(author_id [PK], conference_id [PK])".

Missing constraints to look for:
  a) The composite PK is not declared → add PRIMARY KEY(col1, col2).
  b) One or BOTH PK columns are not marked as FK even though they clearly
     reference entity tables → add FK for EACH missing one independently.
     Do not skip the second FK just because the first one exists.
  c) Column names may not match table names exactly — use domain reasoning
     (e.g. "pid" → "papers", "rid" → "reviewers", "cid" → "conferences").

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PATTERN 4 — SRR  (Reified Relationship)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
A relationship that has been promoted to a full entity because it carries
its own attributes.
Signature: surrogate PK (its own "id"), PLUS two or more FK columns pointing
to the participant entity tables, PLUS own attribute columns.
Examples: "review(id [PK], paper_id [FK->papers], reviewer_id [FK->reviewers],
           score, comments)"

Missing constraints to look for:
  a) The surrogate PK is missing → add PRIMARY KEY(id).
  b) One or more of the participant FK columns is not declared as FK even
     though semantically they clearly reference participant entity tables →
     add FK for EACH missing one.
  c) A UNIQUE constraint over the combination of participant FKs may be
     missing (to enforce that the same pair can only appear once) → add
     UNIQUE(col1, col2) if semantically justified.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ANALYSIS APPROACH
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
For EVERY table in the schema, follow this reasoning:
  1. Classify the table into one of the five patterns above (SE, SEw,
     SR, SRR) based on its columns, PK structure, and name semantics.
  2. Check the specific missing-constraint signatures for that pattern.
  3. Cross-check against EXISTING CONSTRAINTS — never re-add what is already
     declared.
  4. Only emit a constraint if you are confident it is semantically correct.
     If uncertain, skip it.  Never invent constraints that are not justified.

RULES
-----
- Return ONLY constraints that are genuinely missing.
- If the schema looks complete, return exactly: -- NO MISSING CONSTRAINTS
- Do NOT re-declare constraints that already exist.
- Every referenced table MUST be an exact name from the ALL TABLES list.
- CRITICAL FK RULE: Every FOREIGN KEY reference column MUST actually exist in
  the referenced table. For example, if you write FK (class) REFERENCES subcl (subclass),
  "subclass" must be an actual column in the "subcl" table. Never assume that
  a column exists in the target table — check the schema above.
- Do NOT add FK constraints where BOTH tables are metadata/config/catalog tables
  (e.g. tables that store schema metadata, class hierarchies, property mappings,
  column dictionaries, or RDF/OWL structural information rather than domain data).
  These internal tables should not be cross-linked with inferred FKs.
- Be CONSERVATIVE: only add a FK when you are very confident the relationship
  is real and both the source column and target column exist. When in doubt, skip it.
- Use the same quoting style as the dump (double-quotes for identifiers with
  capitals or reserved words, plain names otherwise).
- Use this exact SQL dialect:
    ALTER TABLE ONLY {schema_prefix}<table>
        ADD CONSTRAINT "<constraint_name>" FOREIGN KEY (<col>)
            REFERENCES {schema_prefix}<ref_table> (<ref_col>);
  or for PKs:
    ALTER TABLE ONLY {schema_prefix}<table>
        ADD CONSTRAINT "<table>_pkey" PRIMARY KEY (<col>);
  or for composite PKs / UNIQUE:
    ALTER TABLE ONLY {schema_prefix}<table>
        ADD CONSTRAINT "<name>" PRIMARY KEY (<col1>, <col2>);
    ALTER TABLE ONLY {schema_prefix}<table>
        ADD CONSTRAINT "<name>" UNIQUE (<col1>, <col2>);

OUTPUT FORMAT
-------------
Return a plain SQL block — no markdown fences, no explanation, no preamble.
One ALTER TABLE statement per constraint.
If nothing is missing, return exactly: -- NO MISSING CONSTRAINTS

ALL TABLES IN THIS DATABASE:
{chr(10).join("  " + t for t in all_table_names)}

EXISTING CONSTRAINTS (already in the dump — do NOT repeat these):
{existing_block}

SCHEMA:
{schema_block}
"""

    def parse_response(self, raw: str) -> List[str]:
        """
        Extract individual ALTER TABLE statements from the LLM response.
        Strips markdown fences, comments, and blank lines.
        """
        # Remove markdown code fences
        text = re.sub(r"```sql\s*", "", raw, flags=re.IGNORECASE)
        text = re.sub(r"```\s*",    "", text)
        text = text.strip()

        if not text or text.startswith("-- NO MISSING"):
            return []

        # Split into statements on semicolons
        stmts: List[str] = []
        for raw_stmt in text.split(";"):
            stmt = raw_stmt.strip()
            # Drop pure comment lines
            stmt_no_comments = re.sub(r"--[^\n]*", "", stmt).strip()
            if not stmt_no_comments:
                continue
            if re.search(r"ALTER\s+TABLE", stmt, re.IGNORECASE):
                stmts.append(stmt + ";")

        return stmts

    def run(self, parsed: Dict) -> List[str]:
        """Returns a list of new ALTER TABLE SQL statements."""
        prompt = self.build_prompt(parsed)
        print("\n  Sending schema to LLM for constraint analysis...")
        print(f"  (prompt length: {len(prompt)} chars)")
        raw = self.llm.call(prompt, max_tokens=4096)
        new_stmts = self.parse_response(raw)
        return new_stmts


# =====================================================================
#  Phase 2b – Post-Validation of LLM Constraints
# =====================================================================
def validate_constraints(new_stmts: List[str], tables: Dict) -> List[str]:
    """
    Validate every LLM-generated constraint before merging into the dump.

    Drops any FK where:
      - The source table doesn't exist
      - The source column doesn't exist in the source table
      - The referenced table doesn't exist
      - The referenced column doesn't exist in the referenced table

    This prevents downstream Phase 2/5/8 errors caused by hallucinated FKs.
    """
    # Build column index: table_name -> set of column names (lowercased)
    col_index: Dict[str, set] = {}
    for tname, info in tables.items():
        col_index[tname.lower()] = {
            c[0].lower().strip('"') for c in info["columns"]
        }

    valid: List[str] = []
    fk_re = re.compile(
        r"ALTER\s+TABLE\s+(?:ONLY\s+)?(?:[\w\"]+\s*\.\s*)?"
        r"(?P<table>[\w\"]+)\s+"
        r"ADD\s+CONSTRAINT\s+[\w\"]+\s+"
        r"FOREIGN\s+KEY\s*\((?P<col>[^)]+)\)\s+"
        r"REFERENCES\s+(?:[\w\"]+\s*\.\s*)?(?P<ref_table>[\w\"]+)"
        r"\s*\((?P<ref_col>[^)]+)\)",
        re.IGNORECASE,
    )

    for stmt in new_stmts:
        m = fk_re.search(stmt)
        if not m:
            # Not an FK — PK or UNIQUE, keep it but validate table exists
            valid.append(stmt)
            continue

        src_table  = m.group("table").strip('"').lower()
        src_col    = m.group("col").strip().strip('"').lower()
        ref_table  = m.group("ref_table").strip('"').lower()
        ref_col    = m.group("ref_col").strip().strip('"').lower()

        # Check source table and column
        if src_table not in col_index:
            print(f"    [DROP] Source table '{src_table}' not found")
            continue
        if src_col not in col_index[src_table]:
            print(f"    [DROP] Column '{src_col}' not in table '{src_table}'")
            continue

        # Check referenced table and column
        if ref_table not in col_index:
            print(f"    [DROP] Referenced table '{ref_table}' not found")
            continue
        if ref_col not in col_index[ref_table]:
            print(f"    [DROP] Referenced column '{ref_col}' not in table "
                  f"'{ref_table}' (has: {sorted(col_index[ref_table])})")
            continue

        valid.append(stmt)

    dropped = len(new_stmts) - len(valid)
    if dropped:
        print(f"\n  Post-validation: dropped {dropped} invalid constraint(s), "
              f"kept {len(valid)}")
    return valid


def assess_schema_richness(tables: Dict, existing_constraints: List[str]) -> Dict:
    """
    Assess how "rich" the schema already is in terms of constraints.
    Returns a dict with counts and a recommendation on whether Phase 0
    should add constraints at all.
    """
    n_tables = len(tables)
    if n_tables == 0:
        return {"tables": 0, "skip": True, "reason": "no tables found"}

    # Count tables that already have PKs
    tables_with_pk = sum(
        1 for info in tables.values() if info["inline_pks"]
    )
    # Count tables that already have FKs
    tables_with_fk = sum(
        1 for info in tables.values() if info["inline_fks"]
    )
    # Count ALTER TABLE constraints (PKs + FKs from outside CREATE TABLE)
    alter_pks = sum(1 for c in existing_constraints if "PRIMARY KEY" in c.upper())
    alter_fks = sum(1 for c in existing_constraints if "FOREIGN KEY" in c.upper())

    total_pks = tables_with_pk + alter_pks
    total_fks = tables_with_fk + alter_fks

    pk_coverage = total_pks / n_tables if n_tables else 0
    fk_coverage = total_fks / n_tables if n_tables else 0

    # Schema is "rich enough" if a significant portion already has PKs and
    # there are already meaningful FK relationships declared.
    skip = False
    reason = ""
    if pk_coverage >= 0.5 and total_fks >= max(5, n_tables * 0.3):
        skip = True
        reason = (f"schema already has good constraint coverage: "
                  f"{total_pks}/{n_tables} PKs ({pk_coverage:.0%}), "
                  f"{total_fks} FKs — skipping LLM inference")

    return {
        "tables": n_tables,
        "tables_with_inline_pk": tables_with_pk,
        "tables_with_inline_fk": tables_with_fk,
        "alter_pks": alter_pks,
        "alter_fks": alter_fks,
        "total_pks": total_pks,
        "total_fks": total_fks,
        "pk_coverage": pk_coverage,
        "fk_coverage": fk_coverage,
        "skip": skip,
        "reason": reason,
    }


# =====================================================================
#  Phase 3 – Merge & Write
# =====================================================================
class DumpMerger:
    """
    Injects new ALTER TABLE statements into the original dump text.

    Insertion strategy (in order of preference):
      1. Just before the first existing ALTER TABLE block.
      2. At the very end of the file.

    The new block is clearly delimited with comments so it is easy to
    identify and review.
    """

    SECTION_HEADER = (
        "\n\n--\n"
        "-- CONSTRAINTS ADDED BY PHASE 0 (auto-detected missing constraints)\n"
        "--\n\n"
    )
    SECTION_FOOTER = "\n\n-- END PHASE 0 ADDITIONS\n"

    def merge(self, original_sql: str, new_stmts: List[str]) -> str:
        if not new_stmts:
            return original_sql

        injection = (
            self.SECTION_HEADER
            + "\n\n".join(new_stmts)
            + self.SECTION_FOOTER
        )

        # Find the first ALTER TABLE position
        m = re.search(r"^ALTER\s+TABLE\b", original_sql, re.IGNORECASE | re.MULTILINE)
        if m:
            pos = m.start()
            return original_sql[:pos] + injection + original_sql[pos:]

        # Fallback: append at end
        return original_sql.rstrip() + "\n" + injection


# =====================================================================
#  Orchestrator
# =====================================================================
def run_phase0():
    print("=" * 65)
    print("  PHASE 0 — SQL DUMP PARSER + CONSTRAINT COMPLETION")
    print("=" * 65)

    # ── Load dump ───────────────────────────────────────────────────
    if not os.path.exists(INPUT_DUMP):
        raise FileNotFoundError(f"Dump file not found: {INPUT_DUMP}")

    print(f"\n  Loading dump: {INPUT_DUMP}")
    with open(INPUT_DUMP, "r", encoding="utf-8", errors="replace") as f:
        original_sql = f.read()
    print(f"  Dump size   : {len(original_sql):,} characters")

    # ── Phase 1: Parse ──────────────────────────────────────────────
    print("\n" + "-" * 65)
    print("  PHASE 1 : Parse DDL + data from dump")
    print("-" * 65)

    parser = DumpParser(original_sql)
    parsed = parser.parse()

    tables     = parsed["tables"]
    ex_csts    = parsed["existing_constraints"]
    schema     = parsed["schema"]

    print(f"  Schema      : {schema or '(none)'}")
    print(f"  Tables found: {len(tables)}")
    for tname, info in tables.items():
        n_cols    = len(info["columns"])
        n_pks     = len(info["inline_pks"])
        n_fks     = len(info["inline_fks"])
        n_samples = len(info["sample_rows"])
        print(
            f"    {tname:<40} cols={n_cols}  PKs={n_pks}  "
            f"FKs={n_fks}  samples={n_samples}"
        )

    print(f"\n  Existing ALTER TABLE constraints: {len(ex_csts)}")

    if not tables:
        print("\n  [WARN] No tables found — check dump format. Aborting.")
        return

    # ── Phase 2: LLM Analysis ───────────────────────────────────────
    print("\n" + "-" * 65)
    print("  PHASE 2 : LLM Constraint Analysis")
    print("-" * 65)

    # ── Schema richness assessment ──────────────────────────────
    richness = assess_schema_richness(tables, ex_csts)
    print(f"\n  Schema richness assessment:")
    print(f"    Tables              : {richness['tables']}")
    print(f"    PK coverage         : {richness['total_pks']}/{richness['tables']} "
          f"({richness['pk_coverage']:.0%})")
    print(f"    FK count            : {richness['total_fks']} "
          f"({richness['fk_coverage']:.0%} of tables)")

    if richness["skip"]:
        print(f"\n  [SKIP] {richness['reason']}")
        print("  Schema is sufficiently constrained — skipping LLM FK inference.")
        new_stmts = []
    else:
        llm   = LLMClient(provider=SELECTED_PROVIDER)
        print(f"\n  Provider : {SELECTED_PROVIDER}")
        print(f"  Model    : {llm.config['model_name']}")

        agent     = ConstraintAnalysisAgent(llm)
        new_stmts = agent.run(parsed)

    if new_stmts:
        print(f"\n  LLM found {len(new_stmts)} missing constraint(s):")
        for stmt in new_stmts:
            summary = re.sub(r"\s+", " ", stmt).strip()
            print(f"    + {summary[:120]}")

        # ── Post-validation ─────────────────────────────────────
        print("\n  Validating LLM constraints against actual schema...")
        new_stmts = validate_constraints(new_stmts, tables)
    else:
        print("\n  No missing constraints to add.")

    # ── Phase 3: Merge & Write ──────────────────────────────────────
    print("\n" + "-" * 65)
    print("  PHASE 3 : Merge + Write dump_new.sql")
    print("-" * 65)

    merger      = DumpMerger()
    enriched    = merger.merge(original_sql, new_stmts)

    os.makedirs(os.path.dirname(OUTPUT_DUMP), exist_ok=True)
    with open(OUTPUT_DUMP, "w", encoding="utf-8") as f:
        f.write(enriched)

    print(f"\n  Written to : {OUTPUT_DUMP}")
    print(f"  File size  : {len(enriched):,} characters")

    # ── Summary ─────────────────────────────────────────────────────
    print("\n" + "=" * 65)
    print("  PHASE 0 COMPLETE")
    print("=" * 65)
    print(f"  Tables parsed            : {len(tables)}")
    print(f"  Existing constraints     : {len(ex_csts)}")
    print(f"  New constraints injected : {len(new_stmts)}")
    print(f"  Output dump              : {OUTPUT_DUMP}")
    print()


if __name__ == "__main__":
    try:
        run_phase0()
    except Exception as e:
        print(f"\nFatal error: {e}")
        import traceback
        traceback.print_exc()