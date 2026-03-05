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

Reads  : src2/memory/patterns_final.json
         src2/memory/understanding.json
         src2/memory/enrichment.json
         src2/outputs/DB_as_json/tables_structure.json
         src2/inputs/ontology/ontology.owl
         src2/inputs/database/database.sqlite    (optional — value sampling)
         src2/outputs/mappings/SE_mappings.json
         src2/outputs/mappings/SH_mappings.json
Writes : src2/outputs/mappings_process_hidden.json   (per-table LLM cache)
         src2/outputs/mappings/HIDDEN_mappings.json
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
MEMORY_FOLDER         = "src2/memory"
DB_JSON_FOLDER        = "src2/outputs/DB_as_json"
PATTERNS_FILE         = os.path.join(MEMORY_FOLDER, "patterns_final.json")
UNDERSTANDING_FILE    = os.path.join(MEMORY_FOLDER, "understanding.json")
ENRICHMENT_FILE       = os.path.join(MEMORY_FOLDER, "enrichment.json")
TABLES_STRUCTURE_FILE = os.path.join(DB_JSON_FOLDER, "tables_structure.json")
ONTOLOGY_FILE         = "src2/inputs/ontology/ontology.owl"
SQLITE_DB_FILE        = "src2/inputs/database/database.sqlite"
OUTPUT_DIR            = "src2/outputs"
MAPPINGS_DIR          = os.path.join(OUTPUT_DIR, "mappings")
SE_MAPPINGS_FILE      = os.path.join(MAPPINGS_DIR, "SE_mappings.json")
SH_MAPPINGS_FILE      = os.path.join(MAPPINGS_DIR, "SH_mappings.json")
PROCESS_FILE          = os.path.join(OUTPUT_DIR, "mappings_process_hidden.json")
HIDDEN_MAPPINGS_FILE  = os.path.join(MAPPINGS_DIR, "HIDDEN_mappings.json")

# ===== THRESHOLDS =====
SAMPLE_LIMIT          = 40    # max rows queried per column for distinct value discovery
MAX_DISTINCT_FOR_DISP = 4     # type discriminator: skip if more distinct values than this
MIN_CONFIDENCE        = 3     # minimum LLM confidence (1-5) to accept a suggestion

# ===== COLUMN CLASSIFICATION =====
# Boolean flag prefixes — the column name encodes the sub-entity
BOOL_FLAG_PREFIXES    = ("is_", "has_", "was_", "can_", "did_", "will_")
BOOL_TYPES            = {"boolean", "bool"}
BOOL_OR_INT_TYPES     = {"boolean", "bool", "integer", "int", "smallint", "tinyint"}
TYPE_KEYWORDS         = {"type", "kind", "category", "role", "status", "mode",
                         "flag", "class", "subtype", "variant", "form"}
DISCRIMINATOR_TYPES   = {"integer", "int", "smallint", "bigint", "tinyint",
                         "boolean", "bool", "varchar", "text", "char", "character"}


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
                prefixes[""] = base.rstrip("/") + "#"
    except ET.ParseError:
        with open(owl_file, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        for m in re.finditer(r'<Prefix\s+name="([^"]*)"\s+IRI="([^"]+)"', content):
            prefixes[m.group(1)] = m.group(2)
        if "" not in prefixes:
            m = re.search(r'ontologyIRI="([^"]+)"', content)
            if m:
                prefixes[""] = m.group(1).rstrip("/") + "#"
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
      - distinct non-null values (up to SAMPLE_LIMIT)
      - count of non-null rows
      - total row count

    Returns a dict:
      { "values": [...], "non_null_count": N, "total_count": N,
        "distinct_count": N, "available": True/False }
    """
    result = {"values": [], "non_null_count": 0, "total_count": 0,
              "distinct_count": 0, "available": False}
    if not os.path.exists(db_path):
        return result
    try:
        conn = sqlite3.connect(db_path)
        cur  = conn.cursor()

        cur.execute(f'SELECT COUNT(*) FROM "{table_name}"')
        result["total_count"] = cur.fetchone()[0]

        cur.execute(
            f'SELECT DISTINCT "{col_name}" FROM "{table_name}" '
            f'WHERE "{col_name}" IS NOT NULL LIMIT {SAMPLE_LIMIT}'
        )
        rows = [r[0] for r in cur.fetchall()]
        result["values"] = rows

        cur.execute(
            f'SELECT COUNT(*) FROM "{table_name}" '
            f'WHERE "{col_name}" IS NOT NULL'
        )
        result["non_null_count"] = cur.fetchone()[0]

        cur.execute(
            f'SELECT COUNT(DISTINCT "{col_name}") FROM "{table_name}" '
            f'WHERE "{col_name}" IS NOT NULL'
        )
        result["distinct_count"] = cur.fetchone()[0]

        result["available"] = True
        conn.close()
    except Exception as e:
        print(f"  [WARN] SQLite profile failed for {table_name}.{col_name}: {e}")
    return result


def profile_table_columns(table_name: str, columns: List[Dict],
                           pk_set: set, db_path: str) -> Dict[str, Dict]:
    """
    Profile every non-PK column.  Returns col_name → profile dict.
    Skips long-text columns (varchar with many values) at profiling stage.
    """
    profiles = {}
    for col in columns:
        name = col["name"]
        if name in pk_set:
            continue
        dt = col["data_type"].lower().split("(")[0].strip()
        # Always profile boolean/integer/enum-like. For text, only profile if small.
        if dt in ("text", "varchar", "character varying", "char"):
            # Still profile — we'll filter by distinct count downstream
            pass
        profiles[name] = profile_column(table_name, name, db_path)
    return profiles


# ============================================================
# Mapping helpers
# ============================================================

def get_base_subject(table_name: str, se_mappings: Dict,
                     sh_mappings: Dict) -> Optional[str]:
    for phase in (se_mappings, sh_mappings):
        if table_name in phase:
            return phase[table_name]["subject"]["template"]
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
        → col = '<value>'
    """
    dt = data_type.lower().split("(")[0].strip()

    if dt in BOOL_TYPES:
        # Real SQL boolean column — use boolean literals
        bool_val = "true" if str(value).lower() in ("1", "true", "t", "yes") else "false"
        return f"{column_name} = {bool_val}"

    if force_boolean:
        # Integer column storing 0/1 — use INTEGER comparison (not boolean literal)
        # PostgreSQL raises "operator does not exist: integer = boolean" for = true/false
        int_val = 1 if str(value).lower() in ("1", "true", "t", "yes") else 0
        return f"{column_name} = {int_val}"

    if dt in ("varchar", "text", "char", "character", "character varying"):
        return f"{column_name} = '{value}'"

    # integer / bigint / smallint / numeric — emit raw value
    return f"{column_name} = {value}"


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
    Classify a column into one of: bool_flag | type_dispatch | fk_ref | other.

    bool_flag requires BOTH:
      a) column name starts with a boolean-flag prefix (is_, has_, was_, …)
      b) data type is a real SQL boolean  OR  data type is integer AND the
         DB profile confirms only {0, 1} values are stored

    This prevents integer columns like was_a_program_committee_of ∈ {0,1,2}
    from being wrongly classified as bool_flag.
    """
    name  = col["name"]
    dt    = col["data_type"].lower().split("(")[0].strip()
    nl    = name.lower()

    if col.get("is_foreign_key"):
        return "fk_ref"

    has_flag_prefix = any(nl.startswith(pfx) for pfx in BOOL_FLAG_PREFIXES)
    if has_flag_prefix:
        if dt in BOOL_TYPES:
            # Real SQL boolean — always a bool_flag
            return "bool_flag"
        elif dt in BOOL_OR_INT_TYPES:
            # Integer with flag-like name — only accept if DB data confirms 0/1 only
            if _is_binary_integer(col_profile):
                return "bool_flag"
            # Otherwise fall through — might be a type_dispatch or just "other"

    distinct = col_profile.get("distinct_count", 0)
    if dt in DISCRIMINATOR_TYPES and distinct <= MAX_DISTINCT_FOR_DISP:
        if any(kw in nl for kw in TYPE_KEYWORDS) or dt in BOOL_TYPES:
            return "type_dispatch"

    return "other"


def build_table_evidence(
    table_name: str,
    tables_structure: Dict,
    understanding: Dict,
    enrichment: Dict,
    se_mappings: Dict,
    sh_mappings: Dict,
    col_profiles: Dict[str, Dict],
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

    columns_evidence = []
    for col in info.get("columns", []):
        name  = col["name"]
        if name in pk_set:
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

        if profile.get("available"):
            entry["distinct_count"]  = profile["distinct_count"]
            entry["non_null_count"]  = profile["non_null_count"]
            entry["sample_values"]   = profile["values"][:10]  # cap at 10 for prompt
            if name in enums:
                entry["enum_labels"] = enums[name]

        # Flag the bool_flag candidate class name derived from column name
        if kind == "bool_flag":
            for pfx in BOOL_FLAG_PREFIXES:
                if name.lower().startswith(pfx):
                    entry["flag_candidate_name"] = name[len(pfx):]
                    break

        columns_evidence.append(entry)

    return {
        "table_name":    table_name,
        "base_class":    base_class,
        "table_meaning": table_meaning,
        "columns":       columns_evidence,
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

        # Render columns evidence as readable block
        col_lines = []
        for c in evidence["columns"]:
            line = f"  • {c['name']}  type={c['data_type']}  kind={c['kind']}"
            if c.get("meaning"):
                line += f"  meaning=\"{c['meaning']}\""
            if c.get("fk_ref_table"):
                ref_cls = c.get("fk_ref_class") or "not-yet-mapped"
                line += f"  FK→{c['fk_ref_table']}(:{ref_cls})"
            if c.get("flag_candidate_name"):
                line += f"  ⚑ flag-for={c['flag_candidate_name']}"
            if c.get("sample_values") is not None:
                sv = c["sample_values"]
                labels = c.get("enum_labels", {})
                sv_str = ", ".join(
                    f"{v}({labels[str(v)]})" if str(v) in labels else str(v)
                    for v in sv
                )
                line += f"  values=[{sv_str}]  distinct={c.get('distinct_count', '?')}"
            col_lines.append(line)

        col_block = "\n".join(col_lines) if col_lines else "  (no candidate columns)"

        already_str = ", ".join(sorted(already_mapped)) if already_mapped else "none"

        return f"""You are an ontology mapping expert discovering hidden sub-entity patterns.

TABLE: {table_name}  (already mapped to ontology class :{base_class})
TABLE MEANING: {table_meaning or 'not available'}

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
    # Sweep 1: discover sub-entities for one table
    # ----------------------------------------------------------

    def discover_table(
        self,
        evidence: Dict,
        ontology_classes: List[str],
        already_mapped: set,
    ) -> List[Dict]:
        """
        Call the discovery prompt for one table.
        Returns list of accepted suggestion dicts (confidence >= MIN_CONFIDENCE).
        """
        prompt   = self.prompt_discover_subentities(evidence, ontology_classes, already_mapped)
        raw      = self._call(prompt, max_tokens=1500)
        result   = self._parse(raw)

        if not result or not isinstance(result, dict):
            print(f"  [WARN] Could not parse LLM response for {evidence['table_name']}")
            print(f"  [WARN] Raw[:300]: {raw[:300]}")
            return []

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

            if conf < MIN_CONFIDENCE:
                print(f"  [skip] {col} -> :{cls}  conf={conf} < {MIN_CONFIDENCE}")
                continue

            # Normalise
            s["assigned_class"] = cls.lstrip(":")
            s["confidence"]     = conf
            # Remove any kind/filter_type the LLM may have hallucinated —
            # kind is derived from DB schema in apply_suggestion_to_entry
            s.pop("kind",        None)
            s.pop("filter_type", None)
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
) -> Optional[Dict]:
    """
    Build HIDDEN_SH entry for a FK-based hidden subclass.
    Filter: column IS NOT NULL (the ONLY pattern that uses IS NOT NULL).
    """
    col_name       = suggestion["column"]
    assigned_class = suggestion["assigned_class"]
    base_subject   = get_base_subject(table_name, se_mappings, sh_mappings)
    base_tm        = get_base_triple_map(table_name, se_mappings, sh_mappings)

    # FK reference target (may be embedded in suggestion from evidence)
    fk_ref_table   = suggestion.get("fk_ref_table", "")
    ref_tm         = (get_base_triple_map(fk_ref_table, se_mappings, sh_mappings)
                      if fk_ref_table else None)
    ref_resolved   = ref_tm is not None
    if not ref_tm:
        ref_tm = f"urn:r2rml:SE_{fk_ref_table}" if fk_ref_table else None

    return {
        "hidden_pattern":  "HIDDEN_SH",
        "triple_map_iri":  f"urn:r2rml:HIDDEN_SH_{table_name}_{col_name}",
        "source_table":    table_name,
        "trigger_column":  col_name,
        "sql_filter":      f"{col_name} IS NOT NULL",
        "base_triple_map": base_tm,
        "subject": {
            "template":        base_subject or f"{base_iri}{table_name}/{{id}}",
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
                    "parent": suggestion.get("fk_ref_col", "id"),
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
) -> Optional[Dict]:
    """
    Build BOOL_FLAG entry.

    Filter rules (PostgreSQL-safe):
      Real SQL boolean (boolean/bool):   col = true
      Integer stored as 0/1:             col = 1   (NOT col = true — type mismatch error)
    """
    col_name       = suggestion["column"]
    assigned_class = suggestion["assigned_class"]
    base_subject   = get_base_subject(table_name, se_mappings, sh_mappings)
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
            "template":        base_subject or f"{base_iri}{table_name}/{{id}}",
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
) -> Optional[Dict]:
    """
    Build TYPE_DISPATCH entry.
    One dispatch item per non-null value in value_class_map.
    """
    col_name    = suggestion["column"]
    value_map   = suggestion.get("value_class_map", {})
    base_subject = get_base_subject(table_name, se_mappings, sh_mappings)
    base_tm      = get_base_triple_map(table_name, se_mappings, sh_mappings)
    data_type    = col.get("data_type", "integer")

    if not value_map:
        return None

    dispatch = []
    for val, cls in value_map.items():
        if not cls or str(cls).lower() == "null":
            continue
        cls = str(cls).lstrip(":")
        dispatch.append({
            "triple_map_iri":     f"urn:r2rml:HIDDEN_TD_{table_name}_{col_name}_{val}",
            "filter_value":       str(val),
            "filter_column_type": data_type,   # authoritative — read by phase8
            "sql_filter":         _make_sql_filter(col_name, val, data_type),
            "subject": {
                "template":        base_subject or f"{base_iri}{table_name}/{{id}}",
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
        "discriminator_type":   data_type,
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

    # LLM nominated a column the heuristic classifies as "other":
    # accept as type_dispatch only if value_class_map was provided; else reject.
    if db_kind == "other":
        if suggestion.get("value_class_map"):
            db_kind = "type_dispatch"
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
        m = build_hidden_sh_entry(table_name, suggestion, base_iri,
                                   se_mappings, sh_mappings)
        if m:
            entry["hidden_sh"].append(m)
            return True

    elif db_kind == "bool_flag":
        m = build_bool_flag_entry(table_name, suggestion, col_def, base_iri,
                                   se_mappings, sh_mappings)
        if m:
            entry["hidden_sh"].append(m)
            return True

    elif db_kind == "type_dispatch":
        # Validate value_class_map keys against real DB values
        raw_vcm   = suggestion.get("value_class_map") or {}
        db_values = {str(v) for v in profile.get("values", [])}
        if db_values:
            clean_vcm = {k: v for k, v in raw_vcm.items() if str(k) in db_values}
            if not clean_vcm:
                print(f"  [WARN] All value_class_map keys invalid for {table_name}.{col_name} — skipping")
                return False
            suggestion["value_class_map"] = clean_vcm
        m = build_type_dispatch_entry(table_name, suggestion, col_def, base_iri,
                                       se_mappings, sh_mappings)
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
    print(f"  Patterns    : {len(table_patterns)} tables")
    print(f"  Understood  : {len(understanding)} tables")
    print(f"  Enriched    : {len(enrichment)} tables")
    print(f"  SQLite DB   : {'found → ' + SQLITE_DB_FILE if db_available else 'NOT FOUND — value sampling disabled'}")
    print(f"  Max distinct values for dispatch: {MAX_DISTINCT_FOR_DISP}")
    print(f"  Min LLM confidence to accept   : {MIN_CONFIDENCE}/5")

    # ── Load previous phase mappings ───────────────────────
    os.makedirs(MAPPINGS_DIR, exist_ok=True)
    se_mappings = load_json_optional(SE_MAPPINGS_FILE)
    sh_mappings = load_json_optional(SH_MAPPINGS_FILE)
    print(f"\n  SE mappings : {len(se_mappings)} tables")
    print(f"  SH mappings : {len(sh_mappings)} tables")

    # ── Ontology setup ──────────────────────────────────────
    print(f"\nParsing ontology from '{ONTOLOGY_FILE}' ...")
    prefixes = parse_ontology_prefixes(ONTOLOGY_FILE)
    base_iri = get_ontology_base_iri(prefixes)
    ontology_classes = ontology_explorer(mode="classes")["classes"]
    print(f"  Base IRI  : {base_iri}")
    print(f"  Classes   : {len(ontology_classes)}")

    already_mapped   = get_all_mapped_classes(se_mappings, sh_mappings)
    target_tables    = {t: p for t, p in table_patterns.items()
                        if p in ("SE", "SE_SH")}
    print(f"\n  Target tables (SE + SE_SH) : {len(target_tables)}")

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

        if cache_key in process_cache:
            accepted = process_cache[cache_key]
            # Strip any stale kind/filter_type from old cache entries
            for s in accepted:
                s.pop("kind",        None)
                s.pop("filter_type", None)
            print(f"  cached: {len(accepted)} suggestion(s)")
        else:

            # Build evidence dict for LLM
            evidence = build_table_evidence(
                table_name, tables_structure, understanding, enrichment,
                se_mappings, sh_mappings, col_profiles
            )

            # Screen out columns the LLM doesn't need to see:
            # - pure text columns with many distinct values (not useful signals)
            # - columns with 0 evidence
            screened_cols = []
            for c in evidence["columns"]:
                dt = c["data_type"].lower().split("(")[0].strip()
                if c["kind"] == "other":
                    # Only include if it has enum labels or few distinct values
                    distinct = c.get("distinct_count", 9999)
                    if distinct > MAX_DISTINCT_FOR_DISP and not c.get("enum_labels"):
                        continue
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
                        evidence, ontology_classes, already_mapped
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
    for table_name, suggestions in all_proposals.items():
        kept_sugg = []
        for s in suggestions:
            key    = f"{table_name}.{s['column']}"
            dec    = decisions.get(key, {})
            action = dec.get("action", "keep").lower()

            if action == "drop":
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