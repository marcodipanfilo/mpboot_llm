"""
Ontology Mapper — Phase 6 (Hidden Patterns)

Discovers and maps hidden sub-entity patterns inside SE and SE_SH tables.
Runs TWO sequential LLM sweeps:

  SWEEP 1 — Per-table sub-entity discovery:
    For each SE/SE_SH table, gathers full evidence from the DB (column profiles,
    distinct value samples, FK relationships, enrichment, understanding) and asks
    a single LLM prompt to nominate which columns reveal hidden sub-entities, with
    a confidence score. Three candidate kinds are detected:

      KIND A — FK-based hidden subclass (HIDDEN_SH)
        A non-PK FK column references a table whose class is in the ontology.
        When the FK is NOT NULL the row belongs to that additional class.
        Filter: column IS NOT NULL  (the ONLY case where IS NOT NULL is used)
        Example: papers.author → :Author when author IS NOT NULL

      KIND B — Boolean flag sub-entity (BOOL_FLAG)   ← THE NEW MISSING PIECE
        A boolean/integer column whose name starts with is_, has_, was_, can_, did_
        encodes class membership directly in its name.
        Filter: column = true   (false/null = not a member → no triple needed)
        Example: Person.is_Reviewer = true → :Reviewer
        Example: Person.is_Contribution_1th-author = true → :Contribution_1th-author

      KIND C — Type discriminator (TYPE_DISPATCH)
        A non-FK column holds a small set of discriminator values (≤ MAX_DISTINCT_VALUES)
        where each value maps to a distinct ontology class.
        Filter: column = <value>  (one TriplesMap per non-null value)
        Example: papers.type ∈ {1, 2} → :ConferenceDocument / :AbstractDocument

    The script profiles every candidate column from SQLite before calling the LLM:
    distinct values, count, data type. Columns with too many distinct values are
    excluded from the LLM prompt automatically (no wasted token budget).

  SWEEP 2 — Global collision & consistency review:
    After all tables are processed the LLM receives the FULL picture: every proposed
    hidden class assignment alongside every existing SE/SH class. It checks for
    duplicates, wrong assignments, and suggests corrections. Only mappings that
    survive this pass are written to HIDDEN_mappings.json.

Reads  : src/memory/patterns_final.json
         src/memory/understanding.json
         src/memory/enrichment.json
         src/outputs/DB_as_json/tables_structure.json
         src/inputs/ontology/ontology.owl
         src/inputs/database/database.sqlite    (optional — value sampling)
         src/outputs/mappings/SE_mappings.json
         src/outputs/mappings/SH_mappings.json
Writes : src/outputs/mappings_process_hidden.json   (per-table LLM cache)
         src/outputs/mappings/HIDDEN_mappings.json
"""

import json
import re
import sqlite3
import requests
import xml.etree.ElementTree as ET
from typing import List, Dict, Optional, Any, Tuple
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.llm_config import LLMConfig
from config.llm_config import SELECTED_PROVIDER
from parsers.ontology_explorer import ontology_explorer

# ===== PATHS =====
MEMORY_FOLDER         = "src/memory"
DB_JSON_FOLDER        = "src/outputs/DB_as_json"
PATTERNS_FILE         = os.path.join(MEMORY_FOLDER, "patterns_final.json")
UNDERSTANDING_FILE    = os.path.join(MEMORY_FOLDER, "understanding.json")
ENRICHMENT_FILE       = os.path.join(MEMORY_FOLDER, "enrichment.json")
TABLES_STRUCTURE_FILE = os.path.join(DB_JSON_FOLDER, "tables_structure.json")
ONTOLOGY_FILE         = "src/inputs/ontology/ontology.owl"
SQLITE_DB_FILE        = "src/inputs/database/database.sqlite"
DUMP_FILE             = "src/inputs/database/dump.sql"
DUMP_NEW_FILE         = "src/inputs/database/dump_new.sql"
OUTPUT_DIR            = "src/outputs"
MAPPINGS_DIR          = os.path.join(OUTPUT_DIR, "mappings")
SE_MAPPINGS_FILE      = os.path.join(MAPPINGS_DIR, "SE_mappings.json")
SH_MAPPINGS_FILE      = os.path.join(MAPPINGS_DIR, "SH_mappings.json")
PROCESS_FILE          = os.path.join(OUTPUT_DIR, "mappings_process_hidden.json")
HIDDEN_MAPPINGS_FILE  = os.path.join(MAPPINGS_DIR, "HIDDEN_mappings.json")
CONSTRAINT_META_FILE  = "src/inputs/database/constraint_metadata.json"

# ===== THRESHOLDS =====
SAMPLE_LIMIT          = 40    # max rows queried per column for distinct value discovery
MAX_DISTINCT_FOR_DISP = 15    # type discriminator: skip if more distinct values than this
MIN_CONFIDENCE        = 3     # minimum LLM confidence (1-5) to accept a suggestion

# ===== COLUMN CLASSIFICATION — 3-TIER SYSTEM =====
# Boolean flag prefixes — the column name encodes the sub-entity
BOOL_FLAG_PREFIXES    = ("is_", "has_", "was_", "can_", "did_", "will_")
BOOL_TYPES            = {"boolean", "bool"}
BOOL_OR_INT_TYPES     = {"boolean", "bool", "integer", "int", "smallint", "tinyint"}

# TIER 1 (Certain): Column is ALWAYS a discriminator. LLM only assigns classes.
# These keywords unambiguously indicate sub-entity dispatch.
TIER1_KEYWORDS        = {"type", "kind", "category", "subtype", "class"}

# TIER 2 (Likely): Column might be a discriminator. LLM decides yes/no AND assigns.
# These keywords sometimes indicate dispatch, sometimes just attributes.
TIER2_KEYWORDS        = {"role", "status", "mode", "flag", "variant", "form",
                         "level", "grade", "rank", "group"}

# Combined for backward compatibility
TYPE_KEYWORDS         = TIER1_KEYWORDS | TIER2_KEYWORDS

DISCRIMINATOR_TYPES   = {"integer", "int", "smallint", "bigint", "tinyint",
                         "boolean", "bool", "varchar", "text", "char", "character"}


def _is_metadata_table(table_name: str, se_mappings: Dict,
                        sh_mappings: Dict,
                        understanding: Dict) -> bool:
    """
    Heuristic: detect internal metadata/config/catalog tables that should
    NOT get hidden sub-entity mappings.

    CRITICAL SAFETY: A table that has been successfully mapped to a real
    ontology class in SE or SE_SH is NEVER considered metadata. The soft
    heuristic only applies to tables with no valid class mapping.
    """
    tname_lower = table_name.lower()

    # ── SAFETY: if table is mapped to a real ontology class, it's NOT metadata ──
    for phase_data in (se_mappings, sh_mappings if sh_mappings else {}):
        entry = phase_data.get(table_name)
        if entry:
            cls = entry.get("subject", {}).get("class", "").lstrip(":")
            if cls and cls not in ("", "Unknown", "Thing"):
                return False  # Real entity table — never skip

    # ── Hard-coded known metadata table names (RODI-specific) ──────────
    METADATA_PREFIXES = ("top_", "rdf2sql")
    METADATA_NAMES = {
        "allcl", "subcl", "inv", "nmj", "nmtables", "md", "mdlastchanged",
        "ocdict", "proptablemap", "rctab", "rdf2sqlconf", "toptables",
        "x1", "x2", "y1", "y2", "u", "v",
    }
    if tname_lower in METADATA_NAMES:
        return True
    if any(tname_lower.startswith(p) for p in METADATA_PREFIXES):
        return True

    # ── Soft heuristic: description explicitly says it's metadata/config ──
    # Only trigger on STRONG indicators that the table is internal infrastructure,
    # not domain data. Words like "subclass" or "schema" can appear in descriptions
    # of real domain tables (e.g. "Paper is a subclass of Document").
    meaning = understanding.get(table_name, {}).get("table_meaning", "")
    if meaning:
        meaning_lower = meaning.lower()
        STRONG_META_PHRASES = [
            "rdf to sql", "rdf2sql", "stores metadata about",
            "column dictionary", "property mapping table",
            "internal configuration", "config setting",
            "stores relationships between tables and columns",
        ]
        if any(phrase in meaning_lower for phrase in STRONG_META_PHRASES):
            return True

    return False


# ============================================================
# Ontology helpers
# ============================================================

def parse_ontology_prefixes(owl_file: str) -> Dict[str, str]:
    prefixes: Dict[str, str] = {}
    try:
        tree = ET.parse(owl_file)
        root = tree.getroot()
        for elem in root.iter():
            local = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if local == "Prefix":
                name = elem.get("name", "")
                iri  = elem.get("IRI", "")
                if iri:
                    prefixes[name] = iri
        if "" not in prefixes:
            base = (root.get("{http://www.w3.org/XML/1998/namespace}base")
                    or root.get("ontologyIRI", ""))
            if base:
                prefixes[""] = base if base.endswith("#") else base.rstrip("/") + "#"
    except ET.ParseError:
        with open(owl_file, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        for m in re.finditer(r'<Prefix\s+name="([^"]*)"\s+IRI="([^"]+)"', content):
            prefixes[m.group(1)] = m.group(2)
        if "" not in prefixes:
            m = re.search(r'ontologyIRI="([^"]+)"', content)
            if m:
                prefixes[""] = m.group(1) if m.group(1).endswith("#") else m.group(1).rstrip("/") + "#"
    except FileNotFoundError:
        print(f"  [WARN] Ontology file not found: {owl_file}")
    return prefixes


def get_ontology_base_iri(prefixes: Dict[str, str]) -> str:
    if "" in prefixes:
        return prefixes[""]
    standard = {"owl", "rdf", "rdfs", "xsd", "xml", "xsp", "swrl", "swrlb", "protege"}
    for name, iri in prefixes.items():
        if name not in standard:
            return iri
    return "http://ontology#"


# ============================================================
# JSON helpers
# ============================================================

def load_json_safe(path: str) -> Dict:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Required file not found: {path}")
    if os.path.getsize(path) == 0:
        raise ValueError(f"File is empty: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_json_optional(path: str) -> Dict:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        print(f"  [WARN] Could not parse '{path}' — starting fresh")
        return {}


def save_json(path: str, data: Any):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# ============================================================
# SQLite column profiler
# ============================================================

def profile_column(table_name: str, col_name: str,
                   db_path: str) -> Dict[str, Any]:
    """
    Query SQLite for:
      - ALL distinct non-null values (no arbitrary LIMIT for low-cardinality)
      - count of non-null rows
      - total row count
      - value occurrence counts (value → count)

    Returns a dict:
      { "values": [...], "non_null_count": N, "total_count": N,
        "distinct_count": N, "value_counts": {val: count}, "available": True/False }
    """
    result = {"values": [], "non_null_count": 0, "total_count": 0,
              "distinct_count": 0, "value_counts": {}, "available": False}
    if not os.path.exists(db_path):
        return result
    try:
        conn = sqlite3.connect(db_path)
        cur  = conn.cursor()

        cur.execute(f'SELECT COUNT(*) FROM "{table_name}"')
        result["total_count"] = cur.fetchone()[0]

        # First get the exact distinct count
        cur.execute(
            f'SELECT COUNT(DISTINCT "{col_name}") FROM "{table_name}" '
            f'WHERE "{col_name}" IS NOT NULL'
        )
        result["distinct_count"] = cur.fetchone()[0]

        # For low-cardinality columns (≤ MAX_DISTINCT_FOR_DISP), get ALL values
        # with their occurrence counts. This ensures rare values at the bottom
        # of the table (e.g. type=2 appearing only in last 5 rows) are never missed.
        if result["distinct_count"] <= MAX_DISTINCT_FOR_DISP:
            cur.execute(
                f'SELECT "{col_name}", COUNT(*) as cnt FROM "{table_name}" '
                f'WHERE "{col_name}" IS NOT NULL '
                f'GROUP BY "{col_name}" ORDER BY cnt DESC'
            )
            rows = cur.fetchall()
            result["values"] = [r[0] for r in rows]
            result["value_counts"] = {r[0]: r[1] for r in rows}
        else:
            # High-cardinality: sample up to SAMPLE_LIMIT
            cur.execute(
                f'SELECT DISTINCT "{col_name}" FROM "{table_name}" '
                f'WHERE "{col_name}" IS NOT NULL LIMIT {SAMPLE_LIMIT}'
            )
            result["values"] = [r[0] for r in cur.fetchall()]

        cur.execute(
            f'SELECT COUNT(*) FROM "{table_name}" '
            f'WHERE "{col_name}" IS NOT NULL'
        )
        result["non_null_count"] = cur.fetchone()[0]

        result["available"] = True
        conn.close()
    except Exception as e:
        print(f"  [WARN] SQLite profile failed for {table_name}.{col_name}: {e}")
    return result


def _profile_column_from_dump(table_name: str, col_name: str,
                               dump_path: str) -> Dict[str, Any]:
    """
    Fallback profiler: extract ALL distinct values + counts from a PostgreSQL
    dump file's COPY ... FROM stdin blocks. Used when SQLite DB is not available.

    Reads the ENTIRE COPY block for the table (not just 5 rows) to ensure
    rare values at the bottom are never missed.
    """
    result = {"values": [], "non_null_count": 0, "total_count": 0,
              "distinct_count": 0, "value_counts": {}, "available": False}
    if not os.path.exists(dump_path):
        return result

    try:
        with open(dump_path, "r", encoding="utf-8", errors="replace") as f:
            dump_text = f.read()

        # Find COPY block for this table
        copy_re = re.compile(
            r'COPY\s+(?:[\w"]+\s*\.\s*)?["\']?' + re.escape(table_name) + r'["\']?\s*'
            r'\((?P<cols>[^)]+)\)\s+FROM\s+stdin\s*;\n(?P<data>.*?)^\\\.',
            re.IGNORECASE | re.DOTALL | re.MULTILINE,
        )
        m = copy_re.search(dump_text)
        if not m:
            # Try with quoted table name
            copy_re2 = re.compile(
                r'COPY\s+(?:[\w"]+\s*\.\s*)?"' + re.escape(table_name) + r'"\s*'
                r'\((?P<cols>[^)]+)\)\s+FROM\s+stdin\s*;\n(?P<data>.*?)^\\\.',
                re.IGNORECASE | re.DOTALL | re.MULTILINE,
            )
            m = copy_re2.search(dump_text)
        if not m:
            # Broadest fallback: match table name as a word boundary anywhere in COPY line
            copy_re3 = re.compile(
                r'COPY\s+[^\n]*\b' + re.escape(table_name) + r'\b[^\n]*'
                r'\((?P<cols>[^)]+)\)\s+FROM\s+stdin\s*;\n(?P<data>.*?)^\\\.',
                re.IGNORECASE | re.DOTALL | re.MULTILINE,
            )
            m = copy_re3.search(dump_text)
        if not m:
            print(f"  [DUMP-PROFILE] No COPY block found for '{table_name}' — column '{col_name}' has no values")
            return result

        col_names = [c.strip().strip('"') for c in m.group("cols").split(",")]
        data_text = m.group("data")

        # Find column index
        col_idx = None
        for i, cn in enumerate(col_names):
            if cn.lower() == col_name.lower():
                col_idx = i
                break
        if col_idx is None:
            return result

        # Count all values
        value_counts = {}
        total = 0
        non_null = 0
        for line in data_text.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if col_idx >= len(parts):
                continue
            total += 1
            val = parts[col_idx]
            if val == "\\N":
                continue  # NULL
            non_null += 1
            # Normalize: try int conversion for integer-like values
            try:
                val_norm = int(val)
            except (ValueError, TypeError):
                val_norm = val
            value_counts[val_norm] = value_counts.get(val_norm, 0) + 1

        if not value_counts:
            return result

        # Sort by count descending
        sorted_vals = sorted(value_counts.items(), key=lambda x: -x[1])
        result["values"] = [v for v, c in sorted_vals]
        result["value_counts"] = dict(sorted_vals)
        result["total_count"] = total
        result["non_null_count"] = non_null
        result["distinct_count"] = len(value_counts)
        result["available"] = True

    except Exception as e:
        print(f"  [WARN] Dump profile failed for {table_name}.{col_name}: {e}")
    return result


def profile_table_columns(table_name: str, columns: List[Dict],
                           pk_set: set, db_path: str) -> Dict[str, Dict]:
    """
    Profile columns for hidden pattern detection.
    Uses SQLite if available, otherwise falls back to parsing the PostgreSQL dump.

    IMPORTANT: Always profiles columns whose name contains Tier 1 keywords
    (type/kind/category) even if they're part of the primary key.
    A 'type' column in a composite PK is still a discriminator.
    """
    use_sqlite = os.path.exists(db_path)
    dump_path = ""
    if not use_sqlite:
        for dp in (DUMP_NEW_FILE, DUMP_FILE):
            if os.path.exists(dp):
                dump_path = dp
                break

    profiles = {}
    for col in columns:
        name = col["name"]
        nl = name.lower()
        is_tier1 = any(kw in nl for kw in TIER1_KEYWORDS)
        # Skip PK columns UNLESS they match Tier 1 keywords (type discriminators)
        if name in pk_set and not is_tier1:
            continue
        if use_sqlite:
            profiles[name] = profile_column(table_name, name, db_path)
        elif dump_path:
            profiles[name] = _profile_column_from_dump(table_name, name, dump_path)
        else:
            profiles[name] = {"values": [], "non_null_count": 0, "total_count": 0,
                              "distinct_count": 0, "value_counts": {}, "available": False}
    return profiles


# ============================================================
# Mapping helpers
# ============================================================

def get_base_subject(table_name: str, se_mappings: Dict,
                     sh_mappings: Dict,
                     tables_structure: Dict = None) -> Optional[str]:
    """Return the subject template for a table.
    Validates that the template uses this table's own columns and IRI path.
    If not (e.g. phase 2 copied a parent template), rebuilds from actual PKs.

    CRITICAL for SE_SH: a child table legitimately inherits its parent's IRI
    path (e.g. paper uses 'document/{id}' because Paper subclassOf Document).
    The template is correct if the base path matches this table OR any ancestor
    in the parent chain.
    """
    import re as _re
    for phase in (se_mappings, sh_mappings):
        if table_name in phase:
            tmpl = phase[table_name]["subject"]["template"]
            if not tables_structure:
                return tmpl
            own_cols     = {c["name"] for c in tables_structure.get(table_name, {}).get("columns", [])}
            placeholders = _re.findall(r'\{([^}]+)\}', tmpl)
            base_path    = _re.sub(r'/\{[^}]+\}', '', tmpl)

            # Build the parent chain: this table + all ancestors via SE_SH parent_table
            parent_chain = {table_name}
            cur = table_name
            max_depth = 10
            while max_depth > 0:
                entry = sh_mappings.get(cur) or se_mappings.get(cur) or {}
                parent = entry.get("parent_table", "")
                if not parent or parent in parent_chain:
                    break
                parent_chain.add(parent)
                cur = parent
                max_depth -= 1

            # Path is valid if it references THIS table OR any ancestor
            path_is_valid = any(
                f"#{p}" in base_path or f"/{p}" == base_path.rsplit("/", 1)[-1] or base_path.endswith(f"#{p}") or base_path.endswith(f"/{p}")
                for p in parent_chain
            )

            needs_rebuild = (
                (placeholders and not all(p in own_cols for p in placeholders))
                or not path_is_valid
            )
            if needs_rebuild:
                pks = tables_structure.get(table_name, {}).get("primary_keys", [])
                cols = tables_structure.get(table_name, {}).get("columns", [])
                # FK detection from multiple sources
                fk_names = set()
                for c in cols:
                    if c.get("is_foreign_key"):
                        fk_names.add(c["name"])
                    if c.get("foreign_key_reference") or c.get("fk_references") or c.get("references"):
                        fk_names.add(c["name"])
                for fk in tables_structure.get(table_name, {}).get("foreign_keys", []):
                    col_name = fk.get("column") or fk.get("from_column") or fk.get("name")
                    if col_name:
                        fk_names.add(col_name)

                iri_base = _re.match(r'(https?://[^#]+#)', tmpl)
                prefix = iri_base.group(1) if iri_base else ""
                if pks:
                    non_fk_pks = [pk for pk in pks if pk not in fk_names]
                    template_pks = non_fk_pks if non_fk_pks else pks
                    parent_path = _re.sub(r'/\{[^}]+\}.*', '', tmpl)
                    if parent_path and parent_path != prefix.rstrip("#"):
                        tmpl = parent_path + "/" + "/".join(f"{{{pk}}}" for pk in template_pks)
                    else:
                        tmpl = f"{prefix}{table_name}/" + "/".join(f"{{{pk}}}" for pk in template_pks)
                else:
                    non_fk = [c["name"] for c in cols if c["name"] not in fk_names]
                    key_cols = non_fk if non_fk else [c["name"] for c in cols]
                    tmpl = f"{prefix}{table_name}/" + "/".join(f"{{{c}}}" for c in key_cols)
            return tmpl
    return None


def get_base_triple_map(table_name: str, se_mappings: Dict,
                        sh_mappings: Dict) -> Optional[str]:
    for phase in (se_mappings, sh_mappings):
        if table_name in phase:
            return phase[table_name]["triple_map_iri"]
    return None


def get_base_class(table_name: str, se_mappings: Dict,
                   sh_mappings: Dict) -> Optional[str]:
    for phase in (se_mappings, sh_mappings):
        if table_name in phase:
            raw = phase[table_name]["subject"]["class"]
            return raw.lstrip(":")
    return None


def get_all_mapped_classes(se_mappings: Dict, sh_mappings: Dict) -> set:
    classes = set()
    for mapping in (se_mappings, sh_mappings):
        for entry in mapping.values():
            cls = entry.get("subject", {}).get("class", "")
            if cls:
                classes.add(cls.lstrip(":"))
    return classes


def _to_camel_case(name: str) -> str:
    parts = re.split(r"[_\-]", name)
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


# ── Pre-built subclass index (populated once at startup) ──────────────
_subclass_of_index: Dict[str, set] = {}


def _build_subclass_index(owl_file: str) -> Dict[str, set]:
    """Parse SubClassOf from OWL once, transitively close, return child→ancestors."""
    result: Dict[str, set] = {}
    try:
        root = ET.parse(owl_file).getroot()
        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag != "SubClassOf":
                continue
            ch = list(elem)
            if len(ch) != 2:
                continue
            s0 = ch[0].tag.split("}")[-1] if "}" in ch[0].tag else ch[0].tag
            s1 = ch[1].tag.split("}")[-1] if "}" in ch[1].tag else ch[1].tag
            if s0 != "Class" or s1 != "Class":
                continue
            sub_iri = ch[0].get("IRI", "") or ch[0].get("abbreviatedIRI", "")
            sup_iri = ch[1].get("IRI", "") or ch[1].get("abbreviatedIRI", "")
            sub = sub_iri.split("#")[-1] if "#" in sub_iri else sub_iri.split("/")[-1]
            sup = sup_iri.split("#")[-1] if "#" in sup_iri else sup_iri.split("/")[-1]
            if sub and sup and sup not in ("Thing", ""):
                result.setdefault(sub, set()).add(sup)
        # Transitive closure
        changed = True
        while changed:
            changed = False
            for c, ps in list(result.items()):
                for p in list(ps):
                    new = result.get(p, set()) - ps
                    if new:
                        result[c].update(new)
                        changed = True
    except Exception:
        pass
    return result


def _is_subclass_of(child: str, parent: str) -> bool:
    """Check if child IS-A parent using the pre-built index."""
    ancestors = {child} | _subclass_of_index.get(child, set())
    return parent in ancestors


def _sanitize_filter_value(value: Any) -> str:
    """
    Clean a filter value coming from the LLM before using it in a SQL clause.

    The LLM sometimes wraps values in quotes or splits multi-word values into
    separate quoted tokens, e.g.:
      '"geological"'      -> 'geological'
      '"shut" "down"'     -> 'shut down'
      '"SLIDING SCALE"'   -> 'SLIDING SCALE'

    Steps:
      1. Strip leading/trailing whitespace.
      2. Remove all double-quote characters (LLM identifier-style quoting).
      3. Collapse multiple spaces to one and strip again.
    """
    v = str(value).strip()
    v = v.replace('"', '')   # remove all double quotes
    v = ' '.join(v.split())   # collapse multiple spaces
    return v


def _make_sql_filter(column_name: str, value: Any, data_type: str,
                     force_boolean: bool = False) -> str:
    """
    Build a type-correct SQL WHERE clause fragment.

    Decision table:
      Real SQL boolean (boolean/bool):
        → col = true / col = false   (PostgreSQL boolean literals)
      Integer column confirmed as 0/1 boolean (force_boolean=True):
        → col = 1 / col = 0         (integer comparison, NOT boolean literal)
        PostgreSQL: integer = boolean is an error. Use integer = 1, not = true.
      Integer / numeric columns (force_boolean=False):
        → col = <number>
      String columns (varchar/text/char):
        → col = 'value'  (single quotes, with internal single quotes escaped as \')
    """
    dt = data_type.lower().split("(")[0].strip()
    clean = _sanitize_filter_value(value)

    if dt in BOOL_TYPES:
        # Real SQL boolean column — use boolean literals
        bool_val = "true" if clean.lower() in ("1", "true", "t", "yes") else "false"
        return f"{column_name} = {bool_val}"

    if force_boolean:
        # Integer column storing 0/1 — use INTEGER comparison (not boolean literal)
        # PostgreSQL raises "operator does not exist: integer = boolean" for = true/false
        int_val = 1 if clean.lower() in ("1", "true", "t", "yes") else 0
        return f"{column_name} = {int_val}"

    if dt in ("varchar", "text", "char", "character", "character varying"):
        # Escape internal single quotes so the value is safe inside Turtle '''...'''
        escaped = clean.replace("'", "\\'")
        return f"{column_name} = '{escaped}'"

    # integer / bigint / smallint / numeric — emit raw value
    return f"{column_name} = {clean}"


# ============================================================
# Pre-LLM candidate assembly
# ============================================================

def _is_binary_integer(col_profile: Dict) -> bool:
    """
    Return True only when the column's real DB values are exclusively binary
    (i.e. it acts as a boolean flag). Covers:
      - Integer 0/1 columns
      - SQLite-stored PostgreSQL booleans represented as "t"/"f" strings
    Returns False when no DB data is available (safe default).
    """
    if not col_profile.get("available"):
        return False
    values = col_profile.get("values", [])
    if not values:
        return False
    # Covers: integer 0/1, Python True/False, and SQLite "t"/"f" booleans
    allowed = {0, 1, "0", "1", True, False, "t", "f", "true", "false", "yes", "no"}
    return all(str(v).lower() in {str(a).lower() for a in allowed} for v in values)


def _col_kind(col: Dict, col_profile: Dict) -> str:
    """
    Classify a column into one of:
      bool_flag | type_dispatch_t1 | type_dispatch_t2 | type_dispatch | fk_ref | other

    Priority order:
      1. FK → fk_ref
      2. Name contains Tier 1 keyword (type/kind/category) → type_dispatch_t1
         This ALWAYS wins, even if values are {0,1}. "type" means type dispatch.
      3. SQL boolean type → bool_flag
      4. Boolean prefix (is_/has_) + binary integer → bool_flag
      5. Name contains Tier 2 keyword → type_dispatch_t2
      6. Boolean column without prefix (noun like "listener") → bool_flag
      7. Everything else → other
    """
    name  = col["name"]
    dt    = col["data_type"].lower().split("(")[0].strip()
    nl    = name.lower()

    if col.get("is_foreign_key"):
        return "fk_ref"

    # ── Tier 1 keywords ALWAYS win — checked FIRST ─────────────
    # A column named "type"/"kind"/"category" is a type discriminator
    # even if its values happen to be {0, 1}. The name is the authority.
    distinct = col_profile.get("distinct_count", 0)
    if dt in DISCRIMINATOR_TYPES and distinct <= MAX_DISTINCT_FOR_DISP:
        if any(kw in nl for kw in TIER1_KEYWORDS):
            return "type_dispatch_t1"

    # ── Boolean flag detection ────────────────────────────────
    # SQL boolean type → always bool_flag (program_chair, listener, etc.)
    if dt in BOOL_TYPES:
        return "bool_flag"

    # Flag-prefix (is_/has_/was_) + binary integer
    has_flag_prefix = any(nl.startswith(pfx) for pfx in BOOL_FLAG_PREFIXES)
    if has_flag_prefix and dt in BOOL_OR_INT_TYPES:
        if _is_binary_integer(col_profile):
            return "bool_flag"

    # ── Tier 2 keywords ───────────────────────────────────────
    if dt in DISCRIMINATOR_TYPES and distinct <= MAX_DISTINCT_FOR_DISP:
        if any(kw in nl for kw in TIER2_KEYWORDS):
            return "type_dispatch_t2"

    # ── Binary integer without prefix — bool_flag as last resort ──
    # Noun columns like "listener", "reviewer" with {0,1} values.
    # But NOT columns with Tier 1/2 keywords (already handled above).
    NON_FLAG_NAMES = {"count", "amount", "total", "sum", "quantity", "num", "number",
                      "size", "length", "width", "height", "weight", "price", "cost",
                      "score", "rating", "rank", "order", "position", "index", "level"}
    if dt in BOOL_OR_INT_TYPES and _is_binary_integer(col_profile):
        if not any(nfn in nl for nfn in NON_FLAG_NAMES):
            return "bool_flag"

    return "other"


def build_table_evidence(
    table_name: str,
    tables_structure: Dict,
    understanding: Dict,
    enrichment: Dict,
    se_mappings: Dict,
    sh_mappings: Dict,
    col_profiles: Dict[str, Dict],
    constraint_meta: Dict = None,
) -> Dict:
    """
    Assemble all evidence about a table into a structured dict that will be
    serialised into the LLM prompt.
    """
    info    = tables_structure.get(table_name, {})
    pk_set  = set(info.get("primary_keys", []))
    enr     = enrichment.get(table_name, {})
    und     = understanding.get(table_name, {})
    enums   = enr.get("enum_interpretations", {})

    base_class   = get_base_class(table_name, se_mappings, sh_mappings) or "Unknown"
    table_meaning = und.get("table_meaning", "")
    col_meanings  = und.get("columns", {})

    # Load constraint names for this table
    fk_constraints = {}
    pk_constraint_name = ""
    if constraint_meta:
        t_meta = constraint_meta.get(table_name, {})
        if not t_meta:
            for k, v in constraint_meta.items():
                if k.lower() == table_name.lower():
                    t_meta = v
                    break
        fk_constraints = t_meta.get("fk_constraints", {})
        pk_constraint_name = t_meta.get("pk_constraint_name", "")

    columns_evidence = []
    for col in info.get("columns", []):
        name  = col["name"]
        nl = name.lower()
        is_tier1 = any(kw in nl for kw in TIER1_KEYWORDS)
        # Skip PK columns UNLESS they match Tier 1 keywords (type discriminators)
        if name in pk_set and not is_tier1:
            continue
        dt       = col["data_type"]
        dt_lower = dt.lower().split("(")[0].strip()
        profile  = col_profiles.get(name, {})
        kind     = _col_kind(col, profile)

        entry = {
            "name":         name,
            "data_type":    dt,
            "is_fk":        col.get("is_foreign_key", False),
            "is_nullable":  col.get("is_nullable", True),
            "kind":         kind,
            "meaning":      col_meanings.get(name, ""),
        }

        if col.get("is_foreign_key"):
            ref = col.get("foreign_key_reference", {})
            entry["fk_ref_table"] = ref.get("table", "")
            entry["fk_ref_col"]   = ref.get("column", "id")
            ref_class = get_base_class(ref.get("table", ""), se_mappings, sh_mappings)
            entry["fk_ref_class"] = ref_class or ""
            # Add FK constraint name if available
            fk_meta = fk_constraints.get(name, {})
            cn = fk_meta.get("constraint_name", "")
            if cn:
                entry["fk_constraint_name"] = cn

        if profile.get("available"):
            entry["distinct_count"]  = profile["distinct_count"]
            entry["non_null_count"]  = profile["non_null_count"]
            entry["sample_values"]   = profile["values"][:10]  # cap at 10 for prompt
            # Include value occurrence counts for type_dispatch columns
            if kind in ("type_dispatch_t1", "type_dispatch_t2") and profile.get("value_counts"):
                entry["value_counts"] = {str(k): v for k, v in profile["value_counts"].items()}
            if name in enums:
                entry["enum_labels"] = enums[name]

        # Flag the bool_flag candidate class name derived from column name
        if kind == "bool_flag":
            candidate = None
            for pfx in BOOL_FLAG_PREFIXES:
                if name.lower().startswith(pfx):
                    candidate = name[len(pfx):]
                    break
            if not candidate:
                # No prefix — use the full column name as the candidate
                # e.g. "program_chair" → "program_chair", "listener" → "listener"
                candidate = name
            entry["flag_candidate_name"] = candidate

        columns_evidence.append(entry)

    return {
        "table_name":         table_name,
        "base_class":         base_class,
        "table_meaning":      table_meaning,
        "pk_constraint_name": pk_constraint_name,
        "columns":            columns_evidence,
    }


# ============================================================
# LLM Agent
# ============================================================

class HiddenPatternAgent:

    def __init__(self, provider: str = SELECTED_PROVIDER):
        self.provider = provider
        self.config   = LLMConfig.get_config(provider)
        print(f"  LLM provider: {provider}  model: {self.config['model_name']}")

    def _strip(self, text: str) -> str:
        return re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()

    def _has_json(self, text: str) -> bool:
        t = self._strip(text)
        j = t.find("{"); e = t.rfind("}") + 1
        if j == -1 or e == 0:
            return False
        try:
            json.loads(t[j:e])
            return True
        except Exception:
            return False

    def _call(self, prompt: str, max_tokens: int = 1500) -> str:
        if self.provider == "claude":
            h = {"Content-Type": "application/json",
                 "x-api-key": self.config["api_key"],
                 "anthropic-version": "2023-06-01"}
            d = {"model": self.config["model_name"], "max_tokens": max_tokens,
                 "messages": [{"role": "user", "content": prompt}],
                 "temperature": 0.2}
            return requests.post(self.config["api_url"], headers=h,
                                 json=d).json()["content"][0]["text"]

        elif self.provider == "gemini":
            url = (f"{self.config['api_url']}/{self.config['model_name']}"
                   f":generateContent?key={self.config['api_key']}")
            d = {"contents": [{"parts": [{"text": prompt}]}],
                 "generationConfig": {"temperature": 0.2, "maxOutputTokens": max_tokens}}
            return (requests.post(url, headers={"Content-Type": "application/json"},
                                  json=d).json()
                    ["candidates"][0]["content"]["parts"][0]["text"])

        else:  # openai-compatible (groq, openai, …)
            h = {"Content-Type": "application/json",
                 "Authorization": f"Bearer {self.config['api_key']}"}
            d = {"model": self.config["model_name"],
                 "messages": [{"role": "user", "content": prompt}],
                 "temperature": 0.2, "max_tokens": max_tokens}
            raw = requests.post(self.config["api_url"], headers=h,
                                json=d).json()["choices"][0]["message"]["content"]
            if self.provider == "groq":
                raw = self._strip(raw)
                if not self._has_json(raw):
                    d2 = {"model": self.config["model_name"], "messages": [
                        {"role": "user",      "content": prompt},
                        {"role": "assistant", "content": raw},
                        {"role": "user",      "content": "Output ONLY the JSON now."}
                    ], "temperature": 0.1, "max_tokens": max_tokens}
                    raw = self._strip(
                        requests.post(self.config["api_url"], headers=h,
                                      json=d2).json()["choices"][0]["message"]["content"]
                    )
            return raw

    def _parse(self, text: str) -> Optional[Any]:
        cleaned = re.sub(r'```json\s*', '', text)
        cleaned = re.sub(r'```\s*', '', cleaned).strip()
        j = cleaned.find("{"); e = cleaned.rfind("}") + 1
        if j != -1 and e > 0:
            try:
                return json.loads(cleaned[j:e])
            except Exception:
                pass
        # Try array
        j2 = cleaned.find("["); e2 = cleaned.rfind("]") + 1
        if j2 != -1 and e2 > 0:
            try:
                return json.loads(cleaned[j2:e2])
            except Exception:
                pass
        return None

    # ----------------------------------------------------------
    # PROMPT 1 — Per-table sub-entity discovery
    # ----------------------------------------------------------

    def prompt_discover_subentities(
        self,
        evidence: Dict,
        ontology_classes: List[str],
        already_mapped: set,
    ) -> str:
        """
        Build a per-table prompt that asks the LLM to nominate columns that
        reveal hidden sub-entities.  The LLM sees full column evidence
        (kind, data type, sample values, FK targets, bool-flag names).
        """
        table_name    = evidence["table_name"]
        base_class    = evidence["base_class"]
        table_meaning = evidence["table_meaning"]
        pk_cn = evidence.get("pk_constraint_name", "")

        # Render columns evidence as readable block
        col_lines = []
        for c in evidence["columns"]:
            line = f"  • {c['name']}  type={c['data_type']}  kind={c['kind']}"
            if c.get("meaning"):
                line += f"  meaning=\"{c['meaning']}\""
            if c.get("fk_ref_table"):
                ref_cls = c.get("fk_ref_class") or "not-yet-mapped"
                line += f"  FK→{c['fk_ref_table']}(:{ref_cls})"
            if c.get("fk_constraint_name"):
                line += f"  fk_constraint=\"{c['fk_constraint_name']}\""
            if c.get("flag_candidate_name"):
                line += f"  ⚑ flag-for={c['flag_candidate_name']}"
            if c.get("sample_values") is not None:
                sv = c["sample_values"]
                labels = c.get("enum_labels", {})
                vc = c.get("value_counts", {})
                sv_str = ", ".join(
                    f"{v}({labels[str(v)]})" if str(v) in labels
                    else f"{v}({vc[str(v)]} rows)" if str(v) in vc
                    else str(v)
                    for v in sv
                )
                line += f"  values=[{sv_str}]  distinct={c.get('distinct_count', '?')}"
            col_lines.append(line)

        col_block = "\n".join(col_lines) if col_lines else "  (no candidate columns)"

        # Constraint hint block
        constraint_hint = ""
        if pk_cn:
            constraint_hint = f"\nPK CONSTRAINT NAME: \"{pk_cn}\" (may hint at the table's semantic role)\n"

        already_str = ", ".join(sorted(already_mapped)) if already_mapped else "none"

        return f"""You are an ontology mapping expert discovering hidden sub-entity patterns.

TABLE: {table_name}  (already mapped to ontology class :{base_class})
TABLE MEANING: {table_meaning or 'not available'}
{constraint_hint}

COLUMN EVIDENCE (non-PK columns only):
{col_block}

ALREADY MAPPED CLASSES (do not reuse these): {already_str}

ONTOLOGY CLASSES AVAILABLE: {', '.join(ontology_classes)}

TASK:
Identify which columns of '{table_name}' reveal that some rows also belong to a
DIFFERENT ontology class (a hidden sub-entity). Three column kinds to consider:

  A) FK column (kind=fk_ref):
     When the FK is NOT NULL the row may additionally be an instance of the
     referenced table's class.  Use filter_type="IS_NOT_NULL".

  B) Boolean flag column (kind=bool_flag):
     Column name encodes the class (e.g., is_Reviewer → :Reviewer when = true).
     The flag_candidate_name shown is the suggested class name to match in ontology.
     When value = true the row is an instance of that class.
     Use filter_type="BOOL_TRUE".

  C) Type discriminator column (kind=type_dispatch):
     Different values indicate different classes.
     Provide value_class_map for each non-null value.
     Use filter_type="VALUE_MAP".

RULES:
  - Only nominate a column if you are confident the sub-entity has semantic meaning.
  - Do NOT assign a class that is already in ALREADY MAPPED CLASSES.
  - Confidence 1-5: only include suggestions with confidence >= {MIN_CONFIDENCE}.
  - A bool_flag column with flag_candidate_name X should be matched to the closest
    ontology class whose name resembles X (exact or fuzzy match).
  - For FK columns: only accept if the referenced class exists in the ontology AND
    it makes semantic sense for rows in '{table_name}' to also be of that class.
  - For type_dispatch: each value must map to a DIFFERENT ontology class or null.

Return ONLY a JSON object:
{{
  "table": "{table_name}",
  "suggestions": [
    {{
      "column": "exact_column_name",
      "assigned_class": "OntologyClassName",
      "filter_value": "the exact DB value from db_values[] that means TRUE/active for this column",
      "value_class_map": {{"exact_db_value": "ClassName"}},
      "confidence": <1-5>,
      "reasoning": "one sentence"
    }}
  ]
}}

IMPORTANT:
  - filter_value must be copied EXACTLY from the db_values[] shown above (e.g. "t", "1", "true").
  - For fk_ref columns, set filter_value to "IS_NOT_NULL".
  - For type_dispatch columns, omit filter_value and use value_class_map instead.
  - Do NOT invent values not present in db_values[].
  - Do NOT output "kind" or "filter_type" — the script derives these from the DB schema.
  - value_class_map is only needed for type_dispatch columns; omit or set to {{}} otherwise.
  - If no columns qualify, return {{"table": "{table_name}", "suggestions": []}}"""

    # ----------------------------------------------------------
    # PROMPT 2 — Global collision & consistency review
    # ----------------------------------------------------------

    def prompt_review_collisions(
        self,
        all_proposals: Dict[str, List[Dict]],
        existing_classes: set,
        ontology_classes: List[str],
    ) -> str:
        """
        Build a global review prompt that checks all proposed hidden mappings
        for conflicts with each other and with existing SE/SH mappings.
        Returns corrections and removals.
        """
        # Render all proposals as a flat list
        prop_lines = []
        for table, suggestions in sorted(all_proposals.items()):
            for s in suggestions:
                val_map = s.get("value_class_map", {})
                val_str = f" value_map={val_map}" if val_map else ""
                prop_lines.append(
                    f"  {table}.{s['column']} -> :{s['assigned_class']}"
                    f"{val_str}  conf={s['confidence']}"
                )
        props_block = "\n".join(prop_lines) if prop_lines else "  (none)"

        existing_str = ", ".join(sorted(existing_classes)) if existing_classes else "none"

        return f"""You are an ontology mapping expert reviewing a set of proposed hidden class
assignments for correctness and consistency.

EXISTING CLASS ASSIGNMENTS (SE/SH tables — fixed, do not change):
{existing_str}

PROPOSED HIDDEN MAPPINGS (from per-table discovery sweep):
{props_block}

ONTOLOGY CLASSES AVAILABLE: {', '.join(ontology_classes)}

TASK:
Review the proposed mappings for:
  1. DUPLICATE classes — if two proposals assign the same class, decide which
     to keep and which to drop or reassign.
  2. CONFLICTS with existing classes — a proposed class must not already be used
     by an SE/SH table (unless the intent is a genuine subclass relationship,
     which must be noted).
  3. WRONG assignments — if a column name clearly suggests a different class than
     what was proposed, correct it.
  4. LOW-CONFIDENCE entries (confidence < {MIN_CONFIDENCE}) — remove these.

For each proposal you must decide one of:
  "keep"   — accept as-is
  "drop"   — remove from mappings
  "fix"    — keep but change assigned_class to the corrected value

Return ONLY a JSON object where each key is "table.column" and the value is:
{{
  "table.column": {{
    "action": "keep | drop | fix",
    "assigned_class": "corrected class or same as original",
    "reason": "one sentence"
  }},
  ...
}}
If all proposals are fine, return all of them with action="keep"."""

    # ----------------------------------------------------------
    # Targeted Tier 1 dispatch — force value→class mapping for
    # columns that are CERTAIN discriminators but LLM missed
    # ----------------------------------------------------------

    def force_dispatch_mapping(
        self,
        table_name: str,
        base_class: str,
        col_name: str,
        distinct_values: List,
        ontology_classes: List[str],
        already_mapped: set,
        value_counts: Dict = None,
    ) -> Optional[Dict]:
        """
        For a Tier 1 column (name contains type/kind/category) that the LLM
        didn't nominate, ask a targeted question: "What ontology subclass does
        each value represent?"

        When value_counts is provided, includes occurrence counts so the LLM
        can reason about which class is more common (e.g. PaperFullVersion
        typically has more rows than PaperAbstract).

        Returns a suggestion dict or None.
        """
        # Build values string with occurrence counts if available
        if value_counts:
            vals_parts = []
            for v in distinct_values[:15]:
                cnt = value_counts.get(v, value_counts.get(str(v), "?"))
                vals_parts.append(f"{v} ({cnt} rows)")
            vals_str = ", ".join(vals_parts)
            count_hint = (
                "\n\nHINT: Use the row counts to reason about which class each value "
                "represents. Classes that represent the 'main' or 'full' variant "
                "typically have MORE rows than specialized/minor subclasses. "
                "For example, if Paper has subclasses PaperFullVersion and PaperAbstract, "
                "the value with more rows is likely PaperFullVersion (full papers are "
                "typically more common than abstracts)."
            )
        else:
            vals_str = ", ".join(str(v) for v in distinct_values[:15])
            count_hint = ""

        already_str = ", ".join(sorted(already_mapped)) if already_mapped else "none"

        # Find subclasses of base_class in ontology
        subclasses = [c for c in ontology_classes
                      if c != base_class and c not in already_mapped]

        prompt = f"""You are an ontology mapping expert.

TABLE: {table_name} (mapped to ontology class :{base_class})
COLUMN: {col_name} (type discriminator column)
DISTINCT VALUES IN DATABASE: [{vals_str}]

AVAILABLE ONTOLOGY CLASSES (subclasses or related to :{base_class}):
{', '.join(subclasses)}

ALREADY MAPPED CLASSES (do not reuse): {already_str}

TASK:
The column '{col_name}' contains integer/string codes that distinguish
different sub-types of :{base_class}. Map each distinct value to the
most appropriate ontology class.

RULES:
  - Every class MUST come from the AVAILABLE list above.
  - If a value doesn't map to any class, set it to null.
  - Use the class hierarchy: if :{base_class} has known subclasses,
    prefer those. E.g. Paper → PaperFullVersion, PaperAbstract.
  - You MUST map ALL distinct values shown above, not just some.{count_hint}

Return ONLY JSON:
{{
  "column": "{col_name}",
  "assigned_class": "most_common_subclass",
  "value_class_map": {{"value1": "ClassName", "value2": "ClassName"}},
  "confidence": 4,
  "reasoning": "one sentence"
}}"""

        raw = self._call(prompt, max_tokens=800)
        result = self._parse(raw)
        if not result or not isinstance(result, dict):
            return None

        # Validate
        vcm = result.get("value_class_map", {})
        if not vcm:
            return None

        # Clean classes
        clean_vcm = {}
        for val, cls in vcm.items():
            if not cls or str(cls).lower() == "null":
                continue
            cls_clean = str(cls).lstrip(":")
            if cls_clean in ontology_classes:
                clean_vcm[val] = cls_clean

        if not clean_vcm:
            return None

        result["value_class_map"] = clean_vcm
        result["column"] = col_name
        result["assigned_class"] = list(clean_vcm.values())[0]
        result.pop("kind", None)
        result.pop("filter_type", None)

        return result

    # ----------------------------------------------------------
    # Sweep 1: discover sub-entities for one table
    # ----------------------------------------------------------

    def discover_table(
        self,
        evidence: Dict,
        ontology_classes: List[str],
        already_mapped: set,
        col_profiles: Dict[str, Dict] = None,
        tables_structure: Dict = None,
    ) -> List[Dict]:
        """
        Call the discovery prompt for one table.
        Returns list of accepted suggestion dicts (confidence >= MIN_CONFIDENCE).

        Confidence bypass: for columns that deterministically qualify as
        type_dispatch or bool_flag (via _col_kind), the LLM confidence is
        not used as a gate — the column is accepted regardless of score.
        The LLM still assigns the class names and value_class_map.
        """
        prompt   = self.prompt_discover_subentities(evidence, ontology_classes, already_mapped)
        raw      = self._call(prompt, max_tokens=1500)
        result   = self._parse(raw)

        if not result or not isinstance(result, dict):
            print(f"  [WARN] Could not parse LLM response for {evidence['table_name']}")
            print(f"  [WARN] Raw[:300]: {raw[:300]}")
            return []

        table_name = evidence.get("table_name", "")
        # Build col_def lookup for deterministic kind check
        col_defs: Dict[str, Dict] = {}
        if tables_structure and table_name:
            for c in tables_structure.get(table_name, {}).get("columns", []):
                col_defs[c["name"]] = c

        suggestions = result.get("suggestions", [])
        accepted    = []
        for s in suggestions:
            col  = s.get("column", "")
            cls  = s.get("assigned_class", "")
            conf = s.get("confidence", 0)

            if not col or not cls:
                continue
            try:
                conf = int(conf)
            except (ValueError, TypeError):
                conf = 0

            # Deterministic bypass: if _col_kind confirms type_dispatch or bool_flag,
            # accept regardless of LLM confidence — the 4 schema conditions already
            # prove the column qualifies. LLM confidence only reflects uncertainty
            # about class assignment, not column qualification.
            deterministic_kind = None
            if col_defs and col_profiles is not None:
                col_def = col_defs.get(col)
                if col_def:
                    profile = col_profiles.get(col, {})
                    deterministic_kind = _col_kind(col_def, profile)

            if deterministic_kind in ("type_dispatch_t1", "type_dispatch_t2", "type_dispatch", "bool_flag"):
                if conf < MIN_CONFIDENCE:
                    pass  # deterministic kind — bypass confidence threshold
            elif conf < MIN_CONFIDENCE:
                print(f"  [skip] {col} -> :{cls}  conf={conf} < {MIN_CONFIDENCE}")
                continue

            # Normalise
            s["assigned_class"] = cls.lstrip(":")
            s["confidence"]     = conf
            # Remove any kind/filter_type the LLM may have hallucinated —
            # kind is derived from DB schema in apply_suggestion_to_entry
            s.pop("kind",        None)
            s.pop("filter_type", None)

            # ── Validate assigned classes against ontology ─────
            # For single-class assignments: check assigned_class
            assigned = s["assigned_class"]
            if assigned not in ontology_classes:
                continue

            # For type_dispatch value_class_map: validate every class
            vcm = s.get("value_class_map")
            if vcm and isinstance(vcm, dict):
                cleaned_vcm = {}
                for val, vcls in vcm.items():
                    if not vcls or str(vcls).lower() == "null":
                        cleaned_vcm[val] = None
                        continue
                    vcls_clean = str(vcls).lstrip(":")
                    if vcls_clean in ontology_classes:
                        cleaned_vcm[val] = vcls_clean
                s["value_class_map"] = cleaned_vcm
                # If all classes were dropped, skip the whole suggestion
                if not any(v for v in cleaned_vcm.values()):
                    continue

            accepted.append(s)

        return accepted

    # ----------------------------------------------------------
    # Sweep 2: global collision review
    # ----------------------------------------------------------

    def review_all(
        self,
        all_proposals: Dict[str, List[Dict]],
        existing_classes: set,
        ontology_classes: List[str],
    ) -> Dict[str, Dict]:
        """
        Call the global review prompt.
        Returns {table.column: {action, assigned_class, reason}}.
        """
        if not all_proposals:
            return {}

        prompt = self.prompt_review_collisions(
            all_proposals, existing_classes, ontology_classes
        )
        raw    = self._call(prompt, max_tokens=2000)
        result = self._parse(raw)

        if not result or not isinstance(result, dict):
            print("  [WARN] Could not parse global review response — keeping all proposals")
            print(f"  [WARN] Raw[:300]: {raw[:300]}")
            # Default: keep everything
            decisions = {}
            for table, suggs in all_proposals.items():
                for s in suggs:
                    key = f"{table}.{s['column']}"
                    decisions[key] = {
                        "action":         "keep",
                        "assigned_class": s["assigned_class"],
                        "reason":         "review parse failed — kept by default"
                    }
            return decisions

        return result


# ============================================================
# Mapping entry builders
# ============================================================

def build_hidden_sh_entry(
    table_name: str,
    suggestion: Dict,
    base_iri: str,
    se_mappings: Dict,
    sh_mappings: Dict,
    tables_structure: Dict = None,
) -> Optional[Dict]:
    """
    Build HIDDEN_SH entry for a FK-based hidden subclass.
    Filter: column IS NOT NULL (the ONLY pattern that uses IS NOT NULL).

    Subject template selection:
      There are two distinct semantic cases for FK-based hidden subclasses:

      CASE A — "this source row IS ALSO an instance of the target class"
        Example: papers.abstract → abstracts.id
        The FK value (col_name) IS the id of an existing :Abstract instance.
        → Subject template: use the FK TARGET table's template, substituting
          {fk_col_in_target} with {col_name} (the FK column).
          e.g. conference_documents/{abstract}  (NOT conference_documents/{id})
        This produces the SAME IRI as the canonical SE/SH map for abstracts,
        so rdf:type :Abstract is added to the correct existing resource.

      CASE B — "this source row itself belongs to an additional class"
        Example: persons.is_reviewer = true  (bool_flag, handled separately)
        Or: a row in a table that IS the entity (no FK redirect needed).
        → Subject template: use the source table's own base_subject ({id}).

      Decision rule:
        If assigned_class matches the FK target table's mapped class → CASE A.
        Otherwise → CASE B (use source table template).
    """
    col_name       = suggestion["column"]
    assigned_class = suggestion["assigned_class"]
    base_subject   = get_base_subject(table_name, se_mappings, sh_mappings, tables_structure)
    base_tm        = get_base_triple_map(table_name, se_mappings, sh_mappings)

    # FK reference target
    fk_ref_table   = suggestion.get("fk_ref_table", "")
    fk_ref_col     = suggestion.get("fk_ref_col", "id")
    ref_tm         = (get_base_triple_map(fk_ref_table, se_mappings, sh_mappings)
                      if fk_ref_table else None)
    ref_resolved   = ref_tm is not None
    if not ref_tm:
        ref_tm = f"urn:r2rml:SE_{fk_ref_table}" if fk_ref_table else None

    # ── Subject template: CASE A vs CASE B vs CASE C ─────────────────────
    # CASE A: assigned_class matches the FK target's class exactly
    #   → use the target table's template with {col_name} replacing the PK placeholder
    #   e.g. papers.abstract → conference_documents/{abstract}
    #
    # CASE B: source row itself gains the new class (no FK redirect needed)
    #   → use the source table's own base_subject ({id})
    #
    # CASE C: assigned_class is a specialisation of the FK target's class
    #   (e.g. committee.has_a_committee_chair → FK points to person table, class=Person,
    #    but assigned_class=Chair which IS-A Person via Active_conference_participant)
    #   → use the FK target's template with {col_name} as PK placeholder
    #   This ensures Chair/Co-chair/etc. get person/{col_name} IRI, not committee/{id}
    #
    # Decision: use the target template whenever the FK target table's template
    # pattern semantically matches the assigned class (same base path).
    target_class = get_base_class(fk_ref_table, se_mappings, sh_mappings) if fk_ref_table else None
    target_subject = get_base_subject(fk_ref_table, se_mappings, sh_mappings, tables_structure) if fk_ref_table else None

    import re as _re

    # Detect CASE A (exact match) or CASE C (FK target is a person/entity table)
    use_target_template = False
    if target_class and target_subject:
        # CASE A: exact class match
        if target_class.lstrip(":").lower() == assigned_class.lstrip(":").lower():
            use_target_template = True
        # CASE C: the FK column value references an entity that gains the assigned_class.
        # Only trigger when:
        #   1. The assigned_class is a known SUBCLASS of the FK target's class
        #      (e.g. assigned=Chair, target=Person → Chair IS-A Person → use person/{col})
        #   2. The FK column name is NOT a back-reference
        elif target_subject:
            col_lc = col_name.lower()
            back_ref_hints = ("_inv", "inv", "ispartof", "iscommitteeof",
                              "isreviewof", "submittedto", "belongsto")
            is_back_ref = any(col_lc.endswith(h) or col_lc == h
                              for h in back_ref_hints)
            if not is_back_ref and fk_ref_table != table_name:
                _assigned_clean = assigned_class.lstrip(":")
                _target_clean   = target_class.lstrip(":")
                # Use pre-built subclass index (no inline OWL re-parsing)
                if _is_subclass_of(_assigned_clean, _target_clean):
                    use_target_template = True
                else:
                    use_target_template = False

    if use_target_template and target_subject:
        # Replace the PK placeholder ({id} or whatever the target uses) with
        # the FK column name so the IRI resolves to the target entity's IRI.
        pk_placeholder = _re.search(r"\{([^}]+)\}", target_subject)
        if pk_placeholder:
            subject_template = target_subject.replace(
                "{" + pk_placeholder.group(1) + "}",
                "{" + col_name + "}"
            )
        else:
            subject_template = target_subject
    else:
        # CASE B: source row itself gains the new class — use source template
        if not base_subject:
            # base_subject is None — table not in SE/SH mappings.
            # Try to derive a template from tables_structure PK instead of "id".
            print(f"  [WARN] build_hidden_sh_entry: no base_subject for '{table_name}' — "
                  f"falling back to raw PK lookup")
        if not base_subject and tables_structure:
            pks = tables_structure.get(table_name, {}).get("primary_keys", [])
            if pks:
                base_subject = f"{base_iri}{table_name}/" + "/".join(f"{{{pk}}}" for pk in pks)
            else:
                cols = tables_structure.get(table_name, {}).get("columns", [])
                fk_names = {c["name"] for c in cols if c.get("is_foreign_key")}
                non_fk = [c["name"] for c in cols if c["name"] not in fk_names]
                key_cols = non_fk if non_fk else [c["name"] for c in cols]
                base_subject = f"{base_iri}{table_name}/" + "/".join(f"{{{c}}}" for c in key_cols)
        subject_template = base_subject or f"{base_iri}{table_name}/{{UNKNOWN_PK}}"

    # Guard: if CASE A triggered (assigned_class == FK target class), the FK target
    # entity is already typed by its own SE/SE_SH map. Creating another HIDDEN_SH
    # that types conference/{col} as :Conference produces redundant duplicate triples.
    # Drop these — the join relationship is handled by phase 5b as an object property.
    if use_target_template and target_class and             target_class.lstrip(":").lower() == assigned_class.lstrip(":").lower():
        # Check if target class has a canonical SE/SE_SH map
        target_has_canonical = (
            fk_ref_table in se_mappings or fk_ref_table in sh_mappings
        )
        if target_has_canonical:
            print(f"  [SKIP] HIDDEN_SH {table_name}.{col_name} → :{assigned_class} "
                  f"dropped: FK target already typed by canonical SE/SE_SH map")
            return None

    return {
        "hidden_pattern":  "HIDDEN_SH",
        "triple_map_iri":  f"urn:r2rml:HIDDEN_SH_{table_name}_{col_name}",
        "source_table":    table_name,
        "trigger_column":  col_name,
        "sql_filter":      f"{col_name} IS NOT NULL",
        "base_triple_map": base_tm,
        "subject": {
            "template":        subject_template,
            "class":           f":{assigned_class}",
            "reuses_iri_from": base_tm,
        },
        "predicate_object_maps": ([{
            "predicate": f":{_to_camel_case(col_name)}",
            "object": {
                "type":               "join",
                "parent_triples_map": ref_tm,
                "resolved":           ref_resolved,
                "join_condition": {
                    "child":  col_name,
                    "parent": fk_ref_col,
                }
            }
        }] if ref_tm else []),
        "llm_suggestion": suggestion,
    }


def build_bool_flag_entry(
    table_name: str,
    suggestion: Dict,
    col: Dict,
    base_iri: str,
    se_mappings: Dict,
    sh_mappings: Dict,
    tables_structure: Dict = None,
) -> Optional[Dict]:
    """
    Build BOOL_FLAG entry.

    Filter rules (PostgreSQL-safe):
      Real SQL boolean (boolean/bool):   col = true
      Integer stored as 0/1:             col = 1   (NOT col = true — type mismatch error)
    """
    col_name       = suggestion["column"]
    assigned_class = suggestion["assigned_class"]
    base_subject   = get_base_subject(table_name, se_mappings, sh_mappings, tables_structure)
    base_tm        = get_base_triple_map(table_name, se_mappings, sh_mappings)
    # data_type is authoritative from tables_structure — default integer, never boolean
    data_type      = col.get("data_type") or "integer"
    dt             = data_type.lower().split("(")[0].strip()

    # filter_value: the actual truthy value to compare against in SQL
    # For real boolean columns:   filter_value = "true"  → col = true
    # For integer 0/1 columns:    filter_value = "1"     → col = 1
    if dt in BOOL_TYPES:
        filter_value = "true"
        sql_filter   = f"{col_name} = true"
    else:
        filter_value = "1"
        sql_filter   = f"{col_name} = 1"

    return {
        "hidden_pattern":       "BOOL_FLAG",
        "triple_map_iri":       f"urn:r2rml:HIDDEN_BF_{table_name}_{col_name}",
        "source_table":         table_name,
        "trigger_column":       col_name,
        "trigger_column_type":  data_type,   # authoritative — read by phase8
        "filter_value":         filter_value, # actual value used in WHERE
        "sql_filter":           sql_filter,
        "base_triple_map": base_tm,
        "subject": {
            "template":        base_subject or f"{base_iri}{table_name}/{{UNKNOWN_PK}}",
            "class":           f":{assigned_class}",
            "reuses_iri_from": base_tm,
        },
        "predicate_object_maps": [],
        "llm_suggestion": suggestion,
    }


def build_type_dispatch_entry(
    table_name: str,
    suggestion: Dict,
    col: Dict,
    base_iri: str,
    se_mappings: Dict,
    sh_mappings: Dict,
    tables_structure: Dict = None,
) -> Optional[Dict]:
    """
    Build TYPE_DISPATCH entry.
    One dispatch item per non-null value in value_class_map.
    """
    col_name    = suggestion["column"]
    value_map   = suggestion.get("value_class_map", {})
    base_subject = get_base_subject(table_name, se_mappings, sh_mappings, tables_structure)
    base_tm      = get_base_triple_map(table_name, se_mappings, sh_mappings)
    data_type    = col.get("data_type", "integer")

    if not value_map:
        return None

    dispatch = []
    for raw_val, cls in value_map.items():
        if not cls or str(cls).lower() == "null":
            continue
        val = _sanitize_filter_value(raw_val)
        cls = str(cls).lstrip(":")
        # Sanitize val for use in IRI: replace spaces/special chars with underscores
        val_for_iri = re.sub(r'[^A-Za-z0-9_\-]', '_', val)
        dispatch.append({
            "triple_map_iri":     f"urn:r2rml:HIDDEN_TD_{table_name}_{col_name}_{val_for_iri}",
            "filter_value":       val,
            "filter_column_type": data_type,   # authoritative — read by phase8
            "sql_filter":         _make_sql_filter(col_name, val, data_type),
            "subject": {
                "template":        base_subject or f"{base_iri}{table_name}/{{UNKNOWN_PK}}",
                "class":           f":{cls}",
                "reuses_iri_from": base_tm,
            },
            "predicate_object_maps": [],
        })

    if not dispatch:
        return None

    return {
        "hidden_pattern":       "TYPE_DISPATCH",
        "source_table":         table_name,
        "discriminator_column": col_name,
        "discriminator_type":   data_type,   # authoritative — read by phase8
        "base_triple_map":      base_tm,
        "dispatch":             dispatch,
        "llm_suggestion":       suggestion,
    }


def apply_suggestion_to_entry(
    table_name: str,
    suggestion: Dict,
    tables_structure: Dict,
    col_profiles: Dict,
    base_iri: str,
    se_mappings: Dict,
    sh_mappings: Dict,
    entry: Dict,
) -> bool:
    """
    Convert one accepted LLM suggestion into a mapping entry and append it.

    CRITICAL: kind is derived EXCLUSIVELY from tables_structure.json + DB profiles.
    The LLM only provides: column name, assigned_class, filter_value, value_class_map.
    It never decides kind, filter_type, or SQL syntax.
    """
    col_name = suggestion["column"]

    # Look up authoritative column definition
    info    = tables_structure.get(table_name, {})
    col_def = next((c for c in info.get("columns", []) if c["name"] == col_name), None)

    if col_def is None:
        print(f"  [WARN] Column '{col_name}' not in tables_structure for '{table_name}' — skipping")
        return False

    # Derive kind from DB schema + real DB profile (never from LLM)
    profile  = col_profiles.get(col_name, {})
    db_kind  = _col_kind(col_def, profile)

    # Normalize tier names for downstream processing
    is_type_dispatch = db_kind in ("type_dispatch_t1", "type_dispatch_t2", "type_dispatch")

    # LLM nominated a column the heuristic classifies as "other":
    # accept as type_dispatch only if value_class_map was provided; else reject.
    if db_kind == "other":
        if suggestion.get("value_class_map"):
            db_kind = "type_dispatch"
            is_type_dispatch = True
        elif col_def.get("is_foreign_key"):
            db_kind = "fk_ref"
        else:
            print(f"  [WARN] '{table_name}.{col_name}' is kind=other with no value_class_map — skipping")
            return False

    # Write the DB-derived kind back so logging/cache is accurate
    suggestion["kind"] = db_kind

    if db_kind == "fk_ref":
        ref = col_def.get("foreign_key_reference") or {}
        suggestion["fk_ref_table"] = ref.get("table", "")
        suggestion["fk_ref_col"]   = ref.get("column", "id")

        # ── FK IS NOT NULL validation gate ──────────────────────────
        # Only create FK-based HIDDEN_SH when the LLM-assigned class is
        # DIFFERENT from both:
        #   (a) this table's own base class (e.g. :Abstract for abstracts)
        #   (b) the FK target table's class (e.g. :Person for person)
        # This prevents duplicate TriplesMaps (same class, different template)
        # which break R2RML engines.
        #
        # Valid example: committee.has_a_committee_chair FK→person
        #   table class = :Committee, FK target class = :Person,
        #   assigned class = :Chair → different from both → ALLOWED
        #
        # Invalid example: abstracts.th_part FK→conference_contributions
        #   table class = :Abstract, assigned class = :Abstract → SAME → BLOCKED
        assigned_cls = suggestion.get("assigned_class", "").lstrip(":")

        # Get this table's own base class from SE or SH mappings
        table_base_cls = ""
        for phase_data in (se_mappings, sh_mappings):
            e = phase_data.get(table_name)
            if e:
                table_base_cls = e.get("subject", {}).get("class", "").lstrip(":")
                break

        # Get the FK target table's base class
        fk_target_table = suggestion.get("fk_ref_table", "")
        fk_target_cls = ""
        for phase_data in (se_mappings, sh_mappings):
            e = phase_data.get(fk_target_table)
            if e:
                fk_target_cls = e.get("subject", {}).get("class", "").lstrip(":")
                break

        # Check: assigned class must differ from BOTH source and target base classes
        if assigned_cls == table_base_cls:
            print(f"  [SKIP-FK] {table_name}.{col_name} → :{assigned_cls} "
                  f"is SAME as table's own class :{table_base_cls} — duplicate, skipping")
            return False

        if assigned_cls == fk_target_cls:
            print(f"  [SKIP-FK] {table_name}.{col_name} → :{assigned_cls} "
                  f"is SAME as FK target class :{fk_target_cls} — no new typing, skipping")
            return False

        # Also check: is this class already mapped to an existing SE/SE_SH table?
        for phase_data in (se_mappings, sh_mappings):
            for t, e in phase_data.items():
                existing_cls = e.get("subject", {}).get("class", "").lstrip(":")
                if existing_cls == assigned_cls:
                    print(f"  [SKIP-FK] {table_name}.{col_name} → :{assigned_cls} "
                          f"already mapped to table '{t}' — skipping")
                    return False

        m = build_hidden_sh_entry(table_name, suggestion, base_iri,
                                   se_mappings, sh_mappings, tables_structure)
        if m:
            entry["hidden_sh"].append(m)
            return True

    elif db_kind == "bool_flag":
        m = build_bool_flag_entry(table_name, suggestion, col_def, base_iri,
                                   se_mappings, sh_mappings, tables_structure)
        if m:
            entry["hidden_sh"].append(m)
            return True

    elif is_type_dispatch:
        # Validate value_class_map keys against real DB values.
        # Strip surrounding quotes that the LLM sometimes wraps around values
        # (e.g. '"SLIDING SCALE"' → 'SLIDING SCALE') before matching against DB.
        raw_vcm   = suggestion.get("value_class_map") or {}
        # Sanitize keys using _sanitize_filter_value (handles multi-word quoted tokens)
        raw_vcm   = {_sanitize_filter_value(k): v for k, v in raw_vcm.items()}
        db_values = {str(v) for v in profile.get("values", [])}
        if db_values:
            clean_vcm = {k: v for k, v in raw_vcm.items() if str(k) in db_values}
            if not clean_vcm:
                print(f"  [WARN] All value_class_map keys invalid for {table_name}.{col_name} — skipping")
                return False
            suggestion["value_class_map"] = clean_vcm
        else:
            suggestion["value_class_map"] = raw_vcm
        m = build_type_dispatch_entry(table_name, suggestion, col_def, base_iri,
                                       se_mappings, sh_mappings, tables_structure)
        if m:
            entry["type_dispatch"].append(m)
            return True

    return False


# ============================================================
# Main
# ============================================================

def run_hidden_mapping():
    print("=" * 57)
    print("  ONTOLOGY MAPPER — Phase 6 (Hidden Patterns)")
    print("=" * 57)

    # ── Load inputs ────────────────────────────────────────
    table_patterns   = load_json_safe(PATTERNS_FILE)
    tables_structure = load_json_safe(TABLES_STRUCTURE_FILE)
    understanding    = load_json_safe(UNDERSTANDING_FILE)
    enrichment       = load_json_safe(ENRICHMENT_FILE)

    db_available = os.path.exists(SQLITE_DB_FILE)
    dump_available = any(os.path.exists(p) for p in (DUMP_NEW_FILE, DUMP_FILE))
    print(f"  Patterns    : {len(table_patterns)} tables")
    print(f"  Understood  : {len(understanding)} tables")
    print(f"  Enriched    : {len(enrichment)} tables")
    if db_available:
        print(f"  SQLite DB   : found → {SQLITE_DB_FILE}")
    elif dump_available:
        dp = DUMP_NEW_FILE if os.path.exists(DUMP_NEW_FILE) else DUMP_FILE
        print(f"  SQLite DB   : NOT FOUND — using dump fallback → {dp}")
    else:
        print(f"  SQLite DB   : NOT FOUND — NO dump file either — value sampling DISABLED")
    print(f"  Max distinct values for dispatch: {MAX_DISTINCT_FOR_DISP}")
    print(f"  Min LLM confidence to accept   : {MIN_CONFIDENCE}/5")

    # ── Load previous phase mappings ───────────────────────
    os.makedirs(MAPPINGS_DIR, exist_ok=True)
    se_mappings = load_json_optional(SE_MAPPINGS_FILE)
    sh_mappings = load_json_optional(SH_MAPPINGS_FILE)
    print(f"\n  SE mappings : {len(se_mappings)} tables")
    print(f"  SH mappings : {len(sh_mappings)} tables")

    # ── Ontology setup ──────────────────────────────────────
    prefixes = parse_ontology_prefixes(ONTOLOGY_FILE)
    base_iri = get_ontology_base_iri(prefixes)
    ontology_classes = ontology_explorer(mode="classes")["classes"]
    print(f"  Base IRI: {base_iri}  Classes: {len(ontology_classes)}")

    # Build subclass index once (used by build_hidden_sh_entry)
    global _subclass_of_index
    _subclass_of_index = _build_subclass_index(ONTOLOGY_FILE)

    # Load constraint metadata from Phase 0
    constraint_meta = {}
    if os.path.exists(CONSTRAINT_META_FILE):
        try:
            with open(CONSTRAINT_META_FILE, "r", encoding="utf-8") as f:
                constraint_meta = json.load(f)
            print(f"  Constraint metadata: {len(constraint_meta)} tables")
        except Exception:
            pass

    already_mapped   = get_all_mapped_classes(se_mappings, sh_mappings)
    target_tables    = {t: p for t, p in table_patterns.items()
                        if p in ("SE", "SE_SH")}
    print(f"  Target tables (SE + SE_SH) : {len(target_tables)}")

    # ── Skip metadata / config tables ──────────────────────
    skipped_meta = []
    clean_targets = {}
    for t, p in target_tables.items():
        if _is_metadata_table(t, se_mappings, sh_mappings, understanding):
            skipped_meta.append(t)
        else:
            clean_targets[t] = p
    if skipped_meta:
        print(f"  Skipped metadata tables: {len(skipped_meta)} — {skipped_meta}")
    target_tables = clean_targets

    # ── Load caches ─────────────────────────────────────────
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    process_cache: Dict = load_json_optional(PROCESS_FILE)
    # Always start fresh — never load old HIDDEN_mappings.json.
    # Stale sql_filter strings from previous runs would survive and corrupt TTL output.
    # The LLM process_cache avoids re-calling the LLM; only the JSON build is repeated.
    hidden_mappings: Dict = {}
    if process_cache:
        print(f"  Process cache loaded: {len(process_cache)} entries")

    agent = HiddenPatternAgent(provider=SELECTED_PROVIDER)

    # ────────────────────────────────────────────────────────
    # SWEEP 1 — Per-table sub-entity discovery
    # ────────────────────────────────────────────────────────
    print("\n" + "─" * 57)
    print("  SWEEP 1 — Per-table sub-entity discovery")
    print("─" * 57)

    # all_proposals: table_name -> list of accepted suggestion dicts
    all_proposals:    Dict[str, List[Dict]] = {}
    col_profiles_all: Dict[str, Dict]       = {}   # kept for apply phase
    total   = len(target_tables)
    errors  = []

    for idx, (table_name, pattern) in enumerate(target_tables.items(), 1):
        print(f"\n[{idx:>2}/{total}] {table_name}  [{pattern}]")

        cache_key = f"{table_name}.sweep1"

        # Always re-profile — cheap and must reflect real DB every run
        info   = tables_structure.get(table_name, {})
        pk_set = set(info.get("primary_keys", []))
        col_profiles = profile_table_columns(
            table_name, info.get("columns", []), pk_set, SQLITE_DB_FILE
        )
        col_profiles_all[table_name] = col_profiles

        # Always build evidence — needed for rescue blocks even on cached path
        evidence = build_table_evidence(
            table_name, tables_structure, understanding, enrichment,
            se_mappings, sh_mappings, col_profiles,
            constraint_meta=constraint_meta
        )

        if cache_key in process_cache:
            accepted = process_cache[cache_key]
            # Strip any stale kind/filter_type from old cache entries
            for s in accepted:
                s.pop("kind",        None)
                s.pop("filter_type", None)
            print(f"  cached: {len(accepted)} suggestion(s)")
        else:

            # Screen columns: keep all with ≤MAX_DISTINCT_FOR_DISP distinct values,
            # plus all bool_flag, fk_ref, and type_dispatch regardless of cardinality.
            # This ensures the LLM sees potential discriminator columns even if
            # _col_kind classified them as "other" (the LLM may still recognize them).
            screened_cols = []
            for c in evidence["columns"]:
                if c["kind"] in ("bool_flag", "fk_ref", "type_dispatch", "type_dispatch_t1", "type_dispatch_t2"):
                    screened_cols.append(c)
                else:
                    distinct = c.get("distinct_count", 9999)
                    if distinct <= MAX_DISTINCT_FOR_DISP or c.get("enum_labels"):
                        screened_cols.append(c)
            evidence["columns"] = screened_cols

            if not screened_cols:
                print(f"  no candidate columns after screening — skipping LLM call")
                accepted = []
            else:
                # Log screened column summary
                kinds_summary = {}
                for c in screened_cols:
                    kinds_summary[c["kind"]] = kinds_summary.get(c["kind"], 0) + 1
                print(f"  {len(screened_cols)} columns screened: {kinds_summary}")

                try:
                    accepted = agent.discover_table(
                        evidence, ontology_classes, already_mapped,
                        col_profiles=col_profiles,
                        tables_structure=tables_structure,
                    )
                    print(f"  -> {len(accepted)} suggestion(s) accepted (conf >= {MIN_CONFIDENCE})")
                    for s in accepted:
                        fv = s.get("filter_value", s.get("value_class_map", "?"))
                        print(f"     {s['column']} -> :{s['assigned_class']}  "
                              f"conf={s['confidence']}  filter_value={fv}")
                except Exception as e:
                    print(f"  ✗ LLM error: {e}")
                    errors.append(cache_key)
                    accepted = []

        # ── Rescue blocks — run for BOTH cached and fresh results ──────
        # ── Tier 1 rescue: force dispatch for CERTAIN columns the LLM missed ──
        # Also: if the LLM returned a partial value_class_map for a Tier 1 column,
        # re-call the LLM to fill in missing values.
        accepted_cols = {s.get("column", "") for s in accepted}
        base_class = ""
        for phase_data in (se_mappings, sh_mappings):
            e = phase_data.get(table_name)
            if e:
                base_class = e.get("subject", {}).get("class", "").lstrip(":")
                break

        # Log profiled dispatch/bool columns for debugging
        for c in evidence.get("columns", []):
            if c["kind"] in ("type_dispatch_t1", "type_dispatch_t2", "bool_flag"):
                col_name = c["name"]
                p = col_profiles.get(col_name, {})
                vals = p.get("values", [])
                vc = p.get("value_counts", {})
                avail = p.get("available", False)
                if vc:
                    vc_str = ", ".join(f"{v}={vc.get(v, vc.get(str(v), '?'))}" for v in vals)
                    print(f"  [PROFILE] {col_name} ({c['kind']}): {len(vals)} values [{vc_str}] avail={avail}")
                elif vals:
                    print(f"  [PROFILE] {col_name} ({c['kind']}): {len(vals)} values {vals[:10]} avail={avail}")
                else:
                    print(f"  [PROFILE] {col_name} ({c['kind']}): NO VALUES avail={avail}")

        for c in evidence.get("columns", []):
            col_name = c["name"]
            if c["kind"] != "type_dispatch_t1":
                continue  # Not a Tier 1 column

            profile_t1 = col_profiles.get(col_name, {})
            vals = profile_t1.get("values", [])
            if not vals:
                continue
            vcounts = profile_t1.get("value_counts", {})

            if col_name not in accepted_cols:
                # LLM completely missed this Tier 1 column
                print(f"  [TIER1-RESCUE] {col_name} is Tier 1 but LLM missed it — forcing dispatch")
                try:
                    forced = agent.force_dispatch_mapping(
                        table_name, base_class, col_name, vals,
                        ontology_classes, already_mapped,
                        value_counts=vcounts,
                    )
                    if forced:
                        accepted.append(forced)
                        accepted_cols.add(col_name)
                        fv = forced.get("value_class_map", {})
                        print(f"     {col_name} -> {fv}  [TIER1-FORCED]")
                    else:
                        print(f"     {col_name}: LLM returned no mapping even when forced")
                except Exception as e2:
                    print(f"     {col_name}: force_dispatch error: {e2}")
            else:
                # LLM covered this column — check if value_class_map is complete
                existing = next((s for s in accepted if s.get("column") == col_name), None)
                if existing:
                    vcm = existing.get("value_class_map", {})
                    db_vals = {str(v) for v in vals}
                    mapped_vals = set(vcm.keys())
                    missing_vals = db_vals - mapped_vals
                    if missing_vals and len(missing_vals) < len(db_vals):
                        print(f"  [TIER1-PARTIAL] {col_name} has unmapped values {missing_vals} — forcing")
                        try:
                            forced = agent.force_dispatch_mapping(
                                table_name, base_class, col_name, list(missing_vals),
                                ontology_classes, already_mapped,
                                value_counts=vcounts,
                            )
                            if forced:
                                new_vcm = forced.get("value_class_map", {})
                                vcm.update(new_vcm)
                                existing["value_class_map"] = vcm
                                print(f"     {col_name} updated: {vcm}  [TIER1-COMPLETED]")
                        except Exception as e3:
                            print(f"     {col_name}: partial rescue error: {e3}")

        # ── Bool flag rescue: force bool_flag for boolean columns the LLM missed ──
        for c in evidence.get("columns", []):
            col_name = c["name"]
            if c["kind"] != "bool_flag":
                continue
            if col_name in accepted_cols:
                continue  # LLM already covered this
            # The LLM missed a boolean flag column — add it with a targeted call
            candidate_name = c.get("flag_candidate_name", col_name)
            print(f"  [BOOL-RESCUE] {col_name} is bool_flag but LLM missed — forcing")
            try:
                forced_bf = agent.force_dispatch_mapping(
                    table_name, base_class, col_name, ["true"],
                    ontology_classes, already_mapped,
                )
                if forced_bf:
                    # Convert to bool_flag format
                    cls = forced_bf.get("assigned_class", candidate_name)
                    bf_suggestion = {
                        "column": col_name,
                        "assigned_class": cls,
                        "filter_value": "true",
                        "confidence": 4,
                        "reasoning": f"Boolean column {col_name} indicates {cls} membership",
                    }
                    accepted.append(bf_suggestion)
                    accepted_cols.add(col_name)
                    print(f"     {col_name} -> :{cls}  [BOOL-FORCED]")
                else:
                    print(f"     {col_name}: LLM returned no mapping for bool flag")
            except Exception as e4:
                print(f"     {col_name}: bool rescue error: {e4}")

        # Save to cache (includes rescue additions)
        process_cache[cache_key] = accepted
        save_json(PROCESS_FILE, process_cache)

        if accepted:
            all_proposals[table_name] = accepted
            # Track proposed classes (kind not yet known — use value_class_map as signal)
            for s in accepted:
                vcm = s.get("value_class_map") or {}
                if vcm:
                    for cls in vcm.values():
                        if cls: already_mapped.add(str(cls).lstrip(":"))
                else:
                    already_mapped.add(s["assigned_class"].lstrip(":"))

    print(f"\n  Sweep 1 complete: {sum(len(v) for v in all_proposals.values())} "
          f"proposals across {len(all_proposals)} tables")

    # ────────────────────────────────────────────────────────
    # SWEEP 2 — Global collision & consistency review
    # ────────────────────────────────────────────────────────
    print("\n" + "─" * 57)
    print("  SWEEP 2 — Global collision & consistency review")
    print("─" * 57)

    review_cache_key = "__sweep2_review__"
    if review_cache_key in process_cache:
        decisions = process_cache[review_cache_key]
        print(f"  cached: {len(decisions)} decisions")
    else:
        existing_classes = get_all_mapped_classes(se_mappings, sh_mappings)
        try:
            decisions = agent.review_all(all_proposals, existing_classes,
                                          ontology_classes)
            process_cache[review_cache_key] = decisions
            save_json(PROCESS_FILE, process_cache)
            print(f"  → {len(decisions)} decisions returned")
        except Exception as e:
            print(f"  ✗ LLM error in sweep 2: {e}")
            decisions = {}

    # Apply review decisions
    reviewed_proposals: Dict[str, List[Dict]] = {}
    kept = dropped = fixed = 0

    # Build parent→child class index for inheritance override
    parent_child_classes = {}
    for t, entry in sh_mappings.items():
        parent_table = entry.get("parent_table", "")
        if parent_table:
            child_cls = entry.get("subject", {}).get("class", "").lstrip(":")
            parent_child_classes.setdefault(parent_table, set()).add(child_cls)

    for table_name, suggestions in all_proposals.items():
        kept_sugg = []
        for s in suggestions:
            key    = f"{table_name}.{s['column']}"
            dec    = decisions.get(key, {})
            action = dec.get("action", "keep").lower()

            if action == "drop":
                # Override: if the dispatch class belongs to a CHILD table,
                # it's valid inheritance typing — don't drop
                assigned_cls = s.get("assigned_class", "").lstrip(":")
                child_classes = parent_child_classes.get(table_name, set())
                vcm = s.get("value_class_map", {})
                vcm_classes = set(str(c).lstrip(":") for c in vcm.values() if c)

                is_inheritance = (
                    assigned_cls in child_classes
                    or bool(vcm_classes & child_classes)
                )
                if is_inheritance:
                    print(f"  [keep-override] {key}: dispatch has child class of {table_name} — keeping")
                    kept_sugg.append(s)
                    kept += 1
                else:
                    print(f"  [drop] {key} — {dec.get('reason', '')}")
                    dropped += 1
            elif action == "fix":
                new_cls = str(dec.get("assigned_class", s["assigned_class"])).lstrip(":")
                print(f"  [fix]  {key}: :{s['assigned_class']} → :{new_cls}  "
                      f"({dec.get('reason', '')})")
                s["assigned_class"] = new_cls
                kept_sugg.append(s)
                fixed += 1
            else:
                kept_sugg.append(s)
                kept += 1

        if kept_sugg:
            reviewed_proposals[table_name] = kept_sugg

    print(f"  kept={kept}  fixed={fixed}  dropped={dropped}")

    # ────────────────────────────────────────────────────────
    # Build final HIDDEN_mappings.json
    # ────────────────────────────────────────────────────────
    print("\nBuilding HIDDEN_mappings.json ...")

    for table_name, pattern in target_tables.items():
        if table_name not in hidden_mappings:
            hidden_mappings[table_name] = {
                "source_table":  table_name,
                "pattern":       pattern,
                "hidden_sh":     [],
                "type_dispatch": [],
            }

    found_sh = 0
    found_td = 0

    for table_name, suggestions in reviewed_proposals.items():
        entry        = hidden_mappings.get(table_name)
        col_profiles = col_profiles_all.get(table_name, {})
        if not entry:
            continue

        for suggestion in suggestions:
            ok = apply_suggestion_to_entry(
                table_name, suggestion, tables_structure,
                col_profiles, base_iri, se_mappings, sh_mappings, entry
            )
            if ok:
                kind = suggestion.get("kind", "?")
                if kind in ("fk_ref", "bool_flag"):
                    found_sh += 1
                    print(f"  + {table_name}.{suggestion['column']} "
                          f"[{kind}] -> :{suggestion['assigned_class']}")
                else:
                    found_td += 1
                    print(f"  + {table_name}.{suggestion['column']} "
                          f"[type_dispatch] -> {suggestion.get('value_class_map', {})}")

    save_json(HIDDEN_MAPPINGS_FILE, hidden_mappings)

    total_with_hidden = sum(
        1 for v in hidden_mappings.values()
        if v.get("hidden_sh") or v.get("type_dispatch")
    )

    print(f"\n{'=' * 57}")
    print("  PHASE 6 COMPLETE")
    print(f"{'=' * 57}")
    print(f"  Tables scanned           : {total}")
    print(f"  Tables with hidden pats  : {total_with_hidden}")
    print(f"  Hidden subclass / flag   : {found_sh}")
    print(f"  Type dispatch            : {found_td}")
    print(f"  LLM errors               : {len(errors)}")
    if errors:
        print(f"  Failed tables            : {errors}")
    print(f"\n  Cache  → {PROCESS_FILE}")
    print(f"  Output → {HIDDEN_MAPPINGS_FILE}\n")


if __name__ == "__main__":
    try:
        run_hidden_mapping()
    except Exception as e:
        print(f"\nFatal error: {e}")
        import traceback
        traceback.print_exc()