"""
Ontology Mapper — Phase Final: LLM-Powered R2RML Revision

Sends to the LLM in a single focused prompt:
  1. The full R2RML TTL produced by phase 8
  2. A compact ontology summary (classes, object-properties, data-properties)
  3. The full tables_structure.json (schema + datatypes)
  4. Sample data rows per table, extracted from dump.sql using the same
     COPY/INSERT parser used by dump_explorer.py

Asks the LLM to revise the TTL for:
  A) Property-name correctness  — align every :predicate in the mapping
     against the ontology's declared property names (exact or best match).
  B) WHERE-clause type correctness — verify that every sql_filter value
     matches the actual column data_type and real stored values from the
     dump samples (boolean → true/false, integer → 0/1, string → 'val').
  C) Hyphen-safe SQL aliasing — convert rr:tableName to rr:sqlQuery with
     underscore aliases for any table/column containing hyphens, so that
     the R2RML engine never emits unquoted hyphens in JOIN SQL.

The LLM must return ONLY valid Turtle/R2RML — no explanation, no markdown,
no commentary.  Any wrapping (```turtle … ```) is stripped automatically.

Reads  : src/outputs/mappings/mappings_r2rml.ttl        (required)
         src/inputs/ontology/ontology.owl                (required)
         src/outputs/DB_as_json/tables_structure.json    (required)
         src/inputs/database/dump.sql                    (optional — for samples)
Writes : src/outputs/mappings/mappings_r2rml_final.ttl
"""

import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Set, Tuple

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.llm_config import LLMConfig
from config.llm_config import SELECTED_PROVIDER

# ── Verbosity — set to True to see progress details in the console ──
VERBOSE = False

def log(msg: str, *, warn: bool = False):
    """Print only if VERBOSE=True, but always print warnings."""
    if warn or VERBOSE:
        print(msg)

# ── Paths ────────────────────────────────────────────────────────────────────
OUTPUT_DIR          = "src/outputs"
MAPPINGS_DIR        = os.path.join(OUTPUT_DIR, "mappings")
DB_JSON_DIR         = os.path.join(OUTPUT_DIR, "DB_as_json")
ONTOLOGY_FILE       = "src/inputs/ontology/ontology.owl"
DUMP_FILE           = "src/inputs/database/dump.sql"
TABLES_STRUCT_FILE  = os.path.join(DB_JSON_DIR, "tables_structure.json")
R2RML_INPUT_FILE    = os.path.join(MAPPINGS_DIR, "mappings_r2rml.ttl")
R2RML_OUTPUT_FILE   = os.path.join(MAPPINGS_DIR, "mappings_r2rml_final.ttl")

# How many sample rows to include per table
SAMPLE_ROWS_PER_TABLE = 5

# max_tokens for the LLM response — must be large enough to hold the full TTL
# The input TTL is ~37 KB; we ask for a revised version of similar size.
# Most providers support at least 8 192; use 16 000 as target.
RESPONSE_MAX_TOKENS = 16_000


# ============================================================
# Ontology parser — produces a compact human-readable summary
# ============================================================

def parse_ontology_summary(owl_path: str) -> str:
    """
    Parse an OWL/XML ontology and return a compact plain-text summary:
      CLASSES: ...
      OBJECT_PROPERTIES: ...
      DATA_PROPERTIES: ...

    This keeps the prompt tight while giving the LLM everything it needs
    to verify property names.
    """
    if not os.path.exists(owl_path):
        return "(ontology file not found)"

    try:
        tree = ET.parse(owl_path)
        root = tree.getroot()
    except ET.ParseError as e:
        return f"(ontology parse error: {e})"

    classes: List[str] = []
    object_props: List[str] = []
    data_props: List[str] = []

    for child in root:
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if tag != "Declaration":
            continue
        for sub in child:
            sub_tag = sub.tag.split("}")[-1] if "}" in sub.tag else sub.tag
            iri = sub.get("IRI", "").lstrip("#").lstrip("/").strip()
            if not iri or iri.startswith("http://www.w3.org"):
                continue
            if sub_tag == "Class":
                classes.append(iri)
            elif sub_tag == "ObjectProperty":
                object_props.append(iri)
            elif sub_tag == "DataProperty":
                data_props.append(iri)

    lines = [
        f"CLASSES ({len(classes)}):  " + ",  ".join(sorted(classes)),
        "",
        f"OBJECT_PROPERTIES ({len(object_props)}):  " + ",  ".join(sorted(object_props)),
        "",
        f"DATA_PROPERTIES ({len(data_props)}):  " + ",  ".join(sorted(data_props)),
    ]
    return "\n".join(lines)


# ============================================================
# Hyphen inventory — detect tables/columns with hyphens
# ============================================================

def build_hyphen_inventory(tables_structure: Dict) -> Tuple[Dict[str, List[str]], str]:
    """
    Scan tables_structure.json for table names and column names containing
    hyphens. Returns:
      - hyphen_map: { table_name: [col_with_hyphen, ...] }
                    Also includes table_name itself if it contains a hyphen
                    (stored under key with value []).
      - inventory_text: A human-readable summary for the LLM prompt.

    Only entries that actually contain '-' are included.
    """
    hyphen_map: Dict[str, List[str]] = {}

    for table_name, table_info in tables_structure.items():
        hyphen_cols: List[str] = []
        columns = table_info.get("columns", {})
        for col_name in columns:
            if "-" in col_name:
                hyphen_cols.append(col_name)
        # Record if the table name itself or any columns have hyphens
        if "-" in table_name or hyphen_cols:
            hyphen_map[table_name] = hyphen_cols

    if not hyphen_map:
        return hyphen_map, "(no hyphenated table/column names found)"

    lines: List[str] = []
    for tbl in sorted(hyphen_map.keys()):
        cols = hyphen_map[tbl]
        tbl_flag = " [TABLE NAME HAS HYPHEN]" if "-" in tbl else ""
        if cols:
            col_list = ", ".join(f'"{c}" → alias as "{c.replace("-", "_")}"' for c in cols)
            lines.append(f'  TABLE "{tbl}"{tbl_flag}: {col_list}')
        else:
            lines.append(f'  TABLE "{tbl}"{tbl_flag}: (no hyphenated columns)')
    return hyphen_map, "\n".join(lines)


# ============================================================
# Dump parser — same COPY/INSERT logic as dump_explorer.py
# ============================================================

def _extract_copy_block(sql_content: str, table_name: str,
                        limit: int) -> List[Dict]:
    """Extract rows from a PostgreSQL COPY … FROM stdin block."""
    # Handle both quoted and unquoted table names in COPY statement
    pattern = (
        r'COPY\s+"?' + re.escape(table_name) + r'"?\s*\(([^)]+)\)\s*FROM\s+stdin\s*;'
        r'(.*?)(?=\n\\.)'
    )
    match = re.search(pattern, sql_content, re.IGNORECASE | re.DOTALL)
    if not match:
        return []

    col_names  = [c.strip().strip('"') for c in match.group(1).split(",")]
    data_block = match.group(2).strip()
    rows: List[Dict] = []

    for line in data_block.splitlines():
        line = line.strip()
        if not line or line.startswith("--"):
            continue
        values = line.split("\t")
        row = {
            col_names[i]: (None if v == r"\N" else v)
            for i, v in enumerate(values)
            if i < len(col_names)
        }
        rows.append(row)
        if len(rows) >= limit:
            break

    return rows


def _extract_insert_block(sql_content: str, table_name: str,
                           limit: int) -> List[Dict]:
    """Extract rows from INSERT INTO … VALUES (…) blocks."""
    pattern = (
        r'INSERT\s+INTO\s+"?' + re.escape(table_name) + r'"?\s*\(([^)]+)\)'
        r'\s+VALUES\s+(.*?);'
    )
    match = re.search(pattern, sql_content, re.IGNORECASE | re.DOTALL)
    if not match:
        return []

    col_names      = [c.strip().strip('"') for c in match.group(1).split(",")]
    values_section = match.group(2)
    tuples         = re.findall(r"\(([^)]+)\)", values_section)
    rows: List[Dict] = []

    for tpl in tuples[:limit]:
        raw_vals = [v.strip().strip("'\"") for v in tpl.split(",")]
        row = {
            col_names[i]: (None if raw_vals[i].upper() == "NULL" else raw_vals[i])
            for i in range(min(len(col_names), len(raw_vals)))
        }
        rows.append(row)

    return rows


def get_sample_rows(dump_path: str, table_name: str,
                    limit: int = SAMPLE_ROWS_PER_TABLE) -> List[Dict]:
    """
    Extract up to `limit` sample rows for `table_name` from a SQL dump.
    Tries COPY format first (PostgreSQL), then INSERT format.
    Returns [] if the dump is unavailable or the table has no data.
    """
    if not os.path.exists(dump_path):
        return []
    try:
        with open(dump_path, "r", encoding="utf-8", errors="ignore") as fh:
            sql = fh.read()
        rows = _extract_copy_block(sql, table_name, limit)
        if not rows:
            rows = _extract_insert_block(sql, table_name, limit)
        return rows
    except Exception as e:
        log(f"  [WARN] Sample extraction failed for {table_name}: {e}", warn=True)
        return []


def build_samples_block(tables_structure: Dict, dump_path: str) -> str:
    """
    Build a compact text block showing sample rows for every table.
    Format per table:
        TABLE <name>: col1=v1, col2=v2, ... | col1=v2, ...
    """
    lines: List[str] = []
    for table_name in sorted(tables_structure.keys()):
        rows = get_sample_rows(dump_path, table_name)
        if not rows:
            lines.append(f"TABLE {table_name}: (no sample data available)")
            continue
        row_strs = []
        for row in rows:
            pairs = ", ".join(f"{k}={v!r}" for k, v in row.items())
            row_strs.append(pairs)
        lines.append(f"TABLE {table_name}: " + " | ".join(row_strs))
    return "\n".join(lines)


# ============================================================
# LLM caller — same pattern as all other pipeline agents
# ============================================================

class FinalRevisionAgent:

    def __init__(self, provider: str = SELECTED_PROVIDER):
        self.provider = provider
        self.config   = LLMConfig.get_config(provider)

    def _strip_thinking(self, text: str) -> str:
        return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

    def _call(self, prompt: str, max_tokens: int = RESPONSE_MAX_TOKENS) -> str:
        if self.provider == "claude":
            headers = {
                "Content-Type": "application/json",
                "x-api-key":    self.config["api_key"],
                "anthropic-version": "2023-06-01",
            }
            data = {
                "model":      self.config["model_name"],
                "max_tokens": max_tokens,
                "messages":   [{"role": "user", "content": prompt}],
                "temperature": 0.1,   # low temperature — we want precise corrections
            }
            resp = requests.post(self.config["api_url"], headers=headers, json=data)
            if resp.status_code != 200:
                raise RuntimeError(f"Claude API error {resp.status_code}: {resp.text[:300]}")
            return resp.json()["content"][0]["text"]

        elif self.provider == "gemini":
            url = (
                f"{self.config['api_url']}/{self.config['model_name']}"
                f":generateContent?key={self.config['api_key']}"
            )
            data = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature":     0.1,
                    "maxOutputTokens": max_tokens,
                },
            }
            resp = requests.post(url,
                                 headers={"Content-Type": "application/json"},
                                 json=data)
            if resp.status_code != 200:
                raise RuntimeError(f"Gemini API error {resp.status_code}: {resp.text[:300]}")
            return resp.json()["candidates"][0]["content"]["parts"][0]["text"]

        else:  # openai-compatible (groq, openai, …)
            headers = {
                "Content-Type": "application/json",
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
                raise RuntimeError(
                    f"API error {resp.status_code}: {resp.text[:300]}"
                )
            raw = resp.json()["choices"][0]["message"]["content"]
            if self.provider == "groq":
                raw = self._strip_thinking(raw)

            # Groq retry: if the model only returned thinking with no TTL
            if self.provider == "groq" and "@prefix" not in raw:
                log("  [RETRY] No TTL in response — retrying with explicit instruction …", warn=True)
                data2 = {
                    "model":       self.config["model_name"],
                    "messages": [
                        {"role": "user",      "content": prompt},
                        {"role": "assistant", "content": raw},
                        {"role": "user",      "content": (
                            "Your response did not contain the R2RML Turtle output. "
                            "Now output ONLY the complete revised R2RML Turtle, "
                            "starting with @prefix and ending with the last triple map. "
                            "No explanation, no markdown fences."
                        )},
                    ],
                    "temperature": 0.0,
                    "max_tokens":  max_tokens,
                }
                resp2 = requests.post(self.config["api_url"], headers=headers, json=data2)
                if resp2.status_code == 200:
                    raw = self._strip_thinking(
                        resp2.json()["choices"][0]["message"]["content"]
                    )
            return raw


# ============================================================
# TTL extractor — strip any markdown / explanation wrappers
# ============================================================

def extract_ttl(raw: str) -> str:
    """
    Extract the Turtle/R2RML content from the LLM response.

    Handles:
      - Raw TTL starting directly with @prefix
      - TTL wrapped in ```turtle … ``` or ``` … ``` fences
      - TTL preceded by an explanation paragraph

    If no @prefix is found at all, returns the raw string unchanged
    (the caller will warn).
    """
    # Strip code fences
    text = re.sub(r"```(?:turtle|ttl|r2rml)?\s*", "", raw)
    text = re.sub(r"```\s*", "", text).strip()

    # Find the first @prefix declaration
    idx = text.find("@prefix")
    if idx == -1:
        # Try lowercase variant just in case
        idx = text.lower().find("@prefix")

    if idx > 0:
        preamble = text[:idx].strip()
        if preamble:
            pass
        text = text[idx:]

    # Strip any trailing explanation after the last triple-map closing '.'
    # Find last occurrence of ' .' or '.\n' that ends a block
    last_dot = text.rfind(" .")
    if last_dot != -1 and last_dot < len(text) - 10:
        # Only strip if there's a significant amount of text after the last triple map
        tail = text[last_dot + 2:].strip()
        if tail and not tail.startswith("@") and not tail.startswith("<"):
            pass
            text = text[:last_dot + 2]

    return text.strip()


# ============================================================
# Post-processing — deterministic hyphen-safe alias fix
# ============================================================

def postprocess_hyphen_aliases(ttl: str, hyphen_map: Dict[str, List[str]]) -> str:
    """
    Deterministic post-processing pass on the LLM output to guarantee that
    hyphenated column/table names are handled correctly.

    For every triples map that uses rr:tableName for a table which has
    hyphenated columns (per hyphen_map), convert it to an rr:sqlQuery
    that aliases those columns with underscores.

    Also fixes any rr:child / rr:parent / rr:column values that reference
    the original hyphenated name — they must use the underscore alias instead.

    Additionally, for existing rr:sqlQuery blocks, ensures that any
    hyphenated column referenced in SELECT, WHERE, or aliases is properly
    double-quoted in the SQL and aliased with underscores.
    """
    if not hyphen_map:
        return ttl

    # Build a global lookup: original_hyphen_col -> underscore_alias
    col_alias_map: Dict[str, str] = {}
    for table_name, cols in hyphen_map.items():
        for col in cols:
            col_alias_map[col] = col.replace("-", "_")
        if "-" in table_name:
            # Table name itself needs quoting in SQL but no alias change
            pass

    # ── Step 1: Convert rr:tableName to rr:sqlQuery where needed ──
    # Pattern: rr:logicalTable [ rr:tableName "sometable" ] ;
    def replace_tablename(match):
        table_name = match.group(1)
        if table_name not in hyphen_map:
            return match.group(0)  # no hyphens, leave as-is

        hyphen_cols = hyphen_map[table_name]
        if not hyphen_cols and "-" not in table_name:
            return match.group(0)  # table is in map but no actual hyphens

        # Build SELECT with aliases for hyphenated columns
        # We use SELECT *, "hyphen-col" AS hyphen_col ... but that's redundant.
        # Better: SELECT *, then individual aliases. But simplest correct approach
        # is to explicitly list columns. However, we don't have the full column
        # list here reliably, so use:  SELECT *, "hyp-col" AS hyp_col
        alias_parts = []
        for col in hyphen_cols:
            alias_parts.append(f'"{col}" AS {col.replace("-", "_")}')

        quoted_table = f'"{table_name}"'
        if alias_parts:
            aliases = ", ".join(alias_parts)
            sql = f"SELECT *, {aliases} FROM {quoted_table}"
        else:
            sql = f"SELECT * FROM {quoted_table}"

        return f"rr:logicalTable [ rr:sqlQuery '''{sql}''' ]"

    ttl = re.sub(
        r'''rr:logicalTable\s*\[\s*rr:tableName\s*"([^"]+)"\s*\]''',
        replace_tablename,
        ttl
    )

    # ── Step 2: Fix rr:sqlQuery blocks that reference hyphenated columns ──
    # For existing sqlQuery entries, ensure hyphenated columns are quoted
    # and aliased properly. This handles cases like:
    #   SELECT "id", "is_co-author" AS is_co_author FROM "person" WHERE "is_co-author" = true
    # which should already be correct if the LLM did its job, but we verify.

    def fix_sql_query(match):
        sql = match.group(1)
        modified = False

        for orig_col, alias in col_alias_map.items():
            # Fix unquoted references to hyphenated columns in WHERE clauses
            # e.g., WHERE is_co-author = true  →  WHERE "is_co-author" = true
            # Pattern: word boundary + bare hyphenated col (not inside quotes)
            # This is tricky with regex, so we do targeted replacements:

            # Ensure the column is quoted in WHERE clauses
            # Match: WHERE <optional stuff> bare_col = ...
            where_bare = re.compile(
                r'(WHERE\s+)' + re.escape(orig_col) + r'(\s*=)',
                re.IGNORECASE
            )
            if where_bare.search(sql):
                sql = where_bare.sub(rf'\1"{orig_col}"\2', sql)
                modified = True

            # Ensure aliased in SELECT if present but not aliased
            # Look for "orig_col" without AS alias following it
            select_pattern = re.compile(
                r'"' + re.escape(orig_col) + r'"(?!\s+AS\s)',
                re.IGNORECASE
            )
            # Only add alias in SELECT part (before FROM)
            from_idx = sql.upper().find("FROM")
            if from_idx > 0:
                select_part = sql[:from_idx]
                if f'"{orig_col}"' in select_part and f'AS {alias}' not in select_part:
                    select_part = select_part.replace(
                        f'"{orig_col}"',
                        f'"{orig_col}" AS {alias}'
                    )
                    sql = select_part + sql[from_idx:]
                    modified = True

        if modified:
            return f"rr:sqlQuery '''{sql}'''"
        return match.group(0)

    ttl = re.sub(
        r"rr:sqlQuery\s*'''(.*?)'''",
        fix_sql_query,
        ttl,
        flags=re.DOTALL
    )

    # ── Step 3: Fix rr:child / rr:parent / rr:column that still use hyphens ──
    # These must reference the underscore alias, not the original hyphenated name
    for orig_col, alias in col_alias_map.items():
        # rr:child  "some-col"  →  rr:child  "some_col"
        ttl = ttl.replace(f'rr:child  "{orig_col}"', f'rr:child  "{alias}"')
        ttl = ttl.replace(f'rr:child "{orig_col}"',  f'rr:child "{alias}"')
        ttl = ttl.replace(f'rr:parent  "{orig_col}"', f'rr:parent  "{alias}"')
        ttl = ttl.replace(f'rr:parent "{orig_col}"',  f'rr:parent "{alias}"')
        ttl = ttl.replace(f'rr:column  "{orig_col}"', f'rr:column  "{alias}"')
        ttl = ttl.replace(f'rr:column "{orig_col}"',  f'rr:column "{alias}"')

    return ttl


# ============================================================
# Prompt builder
# ============================================================

def build_prompt(
    ttl_content:       str,
    ontology_summary:  str,
    tables_structure:  Dict,
    samples_block:     str,
    hyphen_inventory:  str,
) -> str:
    tables_json = json.dumps(tables_structure, indent=2, ensure_ascii=False)

    return f"""You are an R2RML expert performing a final accuracy review.

You will receive:
1. A complete R2RML Turtle mapping file
2. The ontology (classes and properties)
3. The database schema (tables_structure.json)
4. Sample data rows from each table
5. An inventory of table/column names that contain hyphens

Your job is to produce a REVISED version of the R2RML that fixes the following categories of errors:

════════════════════════════════════════════════════════
CRITICAL RULE — Preserve exact attribute representations
════════════════════════════════════════════════════════
Before applying any fix, you MUST preserve the EXACT spelling of every
rr:column, rr:child, rr:parent, rr:template, rr:class, triple-map IRI,
and SQL alias that appears in the INPUT R2RML — character for character.
- Do NOT change underscores (_) to hyphens (-) or vice versa in any
  rr:column, rr:child, or rr:parent value UNLESS specifically instructed
  by Task C below.
- Do NOT rename triple-map IRIs (e.g. <urn:r2rml:SE_bid>).
- Do NOT change rr:template URIs.
- Do NOT change rr:class values.
- If the input uses "is_co_author" as an rr:column, keep it as "is_co_author".
- If the input uses "hasbid_inv" as an rr:child, keep it as "hasbid_inv".
When in doubt, copy the attribute value verbatim from the input.

════════════════════════════════════════════════════════
TASK A — Fix property names against the ontology
════════════════════════════════════════════════════════
Every rr:predicate in the mapping must match an actual declared property in the ontology.
The ontology uses camelCase names like :hasAuthor, :writePaper, :name, :title, :email.
Rules:
- If a predicate like :has_a_name exists in the mapping but :name or :hasName exists
  in the ontology, replace it with the correct ontology property.
- If a predicate has no close match in the ontology, keep it as-is (do not delete).
- Do NOT change rr:class values — only rr:predicate.
- Do NOT rename triple map IRIs or subject templates.

════════════════════════════════════════════════════════
TASK B — Fix WHERE clause values against actual data types and sample values
════════════════════════════════════════════════════════
Every rr:sqlQuery that contains a WHERE clause must use the correct literal type.
Cross-check each column's WHERE value against:
  (a) its data_type in tables_structure.json
  (b) the actual values shown in the sample data

Correct rules (PostgreSQL):
- data_type = boolean  AND sample shows t/f    -> WHERE col = true   (SQL boolean literal)
- data_type = integer  AND sample shows 0/1    -> WHERE col = 1      (integer literal)
- data_type = integer  AND sample shows 1/2/3  -> WHERE col = <N>    (integer literal)
- data_type = varchar  AND sample shows 'val'  -> WHERE col = 'val'  (quoted string)
- NEVER use = true or = false for an integer column
- NEVER use = 1 or = 0 for a boolean column

════════════════════════════════════════════════════════
TASK C — Hyphen-safe SQL aliasing (CRITICAL for R2RML engines)
════════════════════════════════════════════════════════
Some database column names and table names contain HYPHENS (e.g. "readbymeta-reviewer",
"co-author", "is_co-author", "co-writepaper"). Hyphens in SQL identifiers are interpreted
as the MINUS operator when unquoted, causing runtime errors like:
    ERROR: column child.readbymeta does not exist
because the engine reads  child.readbymeta-reviewer  as  child.readbymeta - reviewer.

Most R2RML engines do NOT quote column names in the JOIN SQL they generate from
rr:child / rr:parent. Therefore, hyphenated column names used in rr:child or rr:parent
will ALWAYS break.

THE FIX — for every triples map that references a table with hyphenated columns:

1. If the triples map uses  rr:tableName "sometable"  and "sometable" has hyphenated
   columns that are referenced anywhere (in rr:child, rr:parent, rr:column, or
   in a joinCondition from another triples map):
   → Replace  rr:tableName "sometable"
     with     rr:sqlQuery '''SELECT *, "hyphen-col" AS hyphen_col FROM "sometable"'''
   → This creates an underscore alias that the engine can safely use in JOINs.
   → List ALL hyphenated columns as aliases, even if not all are used in this map
     (another map's joinCondition may reference them via rr:parent).

2. If the triples map already uses rr:sqlQuery, ensure that every hyphenated column
   in the SELECT list is:
   → Double-quoted: "hyphen-col"
   → Aliased to underscore: "hyphen-col" AS hyphen_col
   → And in WHERE clauses, the hyphenated column is double-quoted: WHERE "is_co-author" = true

3. Every rr:child and rr:parent that previously referenced a hyphenated column
   MUST now use the underscore alias instead:
   → rr:child  "readbymeta-reviewer"   becomes   rr:child  "readbymeta_reviewer"
   → rr:parent "co-author"             becomes   rr:parent "co_author"

4. rr:column values for hyphenated columns should also use the underscore alias:
   → rr:column "is_co-author"   becomes   rr:column "is_co_author"
   (because rr:column is also used in generated SQL)

Here is the inventory of tables/columns with hyphens:
{hyphen_inventory}

════════════════════════════════════════════════════════
OUTPUT REQUIREMENT — CRITICAL
════════════════════════════════════════════════════════
Output ONLY the complete revised R2RML Turtle file.
- Start with the @prefix declarations, exactly as in the input.
- Include every triple map from the original, revised where needed.
- Do NOT omit any triple map.
- Do NOT add any explanation, comment, heading, or markdown.
- Do NOT wrap the output in ``` fences.
- The very first character of your response must be '@' (start of @prefix).

════════════════════════════════════════════════════════
INPUT 1 — R2RML MAPPING (to be revised)
════════════════════════════════════════════════════════
{ttl_content}

════════════════════════════════════════════════════════
INPUT 2 — ONTOLOGY SUMMARY
════════════════════════════════════════════════════════
{ontology_summary}

════════════════════════════════════════════════════════
INPUT 3 — DATABASE SCHEMA (tables_structure.json)
════════════════════════════════════════════════════════
{tables_json}

════════════════════════════════════════════════════════
INPUT 4 — SAMPLE DATA ROWS (from dump.sql)
════════════════════════════════════════════════════════
{samples_block}

════════════════════════════════════════════════════════
Important: all capital letters found in the old mapping file should remain in capital letter for the new final one.
Now output ONLY the revised R2RML Turtle  — start with @prefix:
════════════════════════════════════════════════════════"""


# ============================================================
# Main
# ============================================================

def run_final_revision():
    pass

    # ── Load required inputs ─────────────────────────────────
    for path, label in [
        (R2RML_INPUT_FILE,   "R2RML TTL"),
        (ONTOLOGY_FILE,      "Ontology OWL"),
        (TABLES_STRUCT_FILE, "tables_structure.json"),
    ]:
        if not os.path.exists(path):
            log(f"\n  ERROR: required file not found: {path}", warn=True)
            sys.exit(1)

    with open(R2RML_INPUT_FILE, "r", encoding="utf-8") as fh:
        ttl_content = fh.read()

    with open(TABLES_STRUCT_FILE, "r", encoding="utf-8") as fh:
        tables_structure: Dict = json.load(fh)

    dump_available = os.path.exists(DUMP_FILE)

    # ── Build compact ontology summary ───────────────────────
    ontology_summary = parse_ontology_summary(ONTOLOGY_FILE)

    # ── Build hyphen inventory ───────────────────────────────
    hyphen_map, hyphen_inventory = build_hyphen_inventory(tables_structure)
    if hyphen_map:
        log(f"  Found hyphenated identifiers in {len(hyphen_map)} table(s)")

    # ── Extract sample rows from dump ────────────────────────
    if dump_available:
        samples_block = build_samples_block(tables_structure, DUMP_FILE)
        n_tables_with_data = sum(
            1 for line in samples_block.splitlines()
            if "no sample data" not in line
        )
    else:
        samples_block = "(dump.sql not available — no sample rows)"

    # ── Build prompt ─────────────────────────────────────────
    prompt = build_prompt(
        ttl_content, ontology_summary, tables_structure,
        samples_block, hyphen_inventory,
    )
    prompt_chars = len(prompt)

    if prompt_chars > 600_000:
        log("\n  WARNING: prompt is very large — some providers may truncate it.", warn=True)

    # ── Call LLM ─────────────────────────────────────────────
    agent = FinalRevisionAgent(provider=SELECTED_PROVIDER)

    try:
        raw_response = agent._call(prompt, max_tokens=RESPONSE_MAX_TOKENS)
    except Exception as e:
        log(f"\n  ERROR: LLM call failed: {e}", warn=True)
        sys.exit(1)


    # ── Extract clean TTL ────────────────────────────────────
    revised_ttl = extract_ttl(raw_response)

    if "@prefix" not in revised_ttl:
        log("\n  WARNING: response does not contain @prefix — saving raw response anyway.", warn=True)
        revised_ttl = raw_response

    # ── Deterministic post-processing: fix hyphen aliases ────
    # This runs AFTER the LLM to catch anything it missed or hallucinated.
    if hyphen_map:
        revised_ttl = postprocess_hyphen_aliases(revised_ttl, hyphen_map)
        log("  Applied deterministic hyphen-alias post-processing")

    ttl_lines = revised_ttl.count("\n")

    # ── Sanity check: does the revised TTL look complete? ────
    original_maps = ttl_content.count("rr:TriplesMap")
    revised_maps  = revised_ttl.count("rr:TriplesMap")
    if revised_maps < original_maps * 0.9:
        log("  *** WARNING: significantly fewer triple maps — LLM may have truncated output", warn=True)
    elif revised_maps == 0:
        log("  *** WARNING: no triple maps found in output", warn=True)
    else:
        pass

    # ── Save output ──────────────────────────────────────────
    os.makedirs(MAPPINGS_DIR, exist_ok=True)
    with open(R2RML_OUTPUT_FILE, "w", encoding="utf-8") as fh:
        fh.write(revised_ttl)



if __name__ == "__main__":
    try:
        run_final_revision()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        pass
        import traceback
        traceback.print_exc()
        sys.exit(1)