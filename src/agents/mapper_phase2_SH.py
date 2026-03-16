"""
Ontology Mapper Agent — Phase 2 (SE_SH tables only)
Maps SE_SH tables (subclass/inherited strong entities) to their ontology class.
Writes SH_mappings.json in outputs/mappings/.

Subject IRI logic:
  - If the direct parent table was mapped in Phase 1 (found in SE_mappings.json),
    use the parent's subject template IRI (shared identity).
  - Otherwise use its own IRI template (parent will be resolved in a later phase).

Column → predicate mapping strategy (in build_sh_json_mapping):
  1. Fetch all data/object properties of the mapped class via ontology_explorer.
  2. For each non-FK attribute column:
       a. rdfs:label  — LLM label call (meaning-aware, name OR meaning pre-filter)
       b. String similarity match against data properties (fast)
       c. LLM semantic selection — sends column meaning from understanding.json
          + unused data properties; LLM picks best semantic fit
       d. camelCase fallback
  3. For each FK column:
       a. String similarity match against object properties
       b. LLM semantic selection (same mechanism, for object properties)
       c. camelCase fallback
  4. Duplicate predicate prevention via used_predicates set throughout.
  5. Retry with exponential backoff on transient API errors (500, 502, 503, 504, 429).

Reads  : src/memory/patterns_final.json
         src/memory/understanding.json
         src/memory/enrichment.json
         src/outputs/DB_as_json/tables_structure.json
         src/inputs/ontology/ontology.owl
         src/outputs/mappings/SE_mappings.json     (Phase 1 output, for parent resolution)
Writes : src/outputs/mappings_process_sh.json      (LLM cache, resumable)
         src/outputs/mappings/SH_mappings.json     (final structured mapping)
"""

import json
import requests
import re
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from typing import List, Dict, Optional, Set, Tuple
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from parsers.ontology_explorer import ontology_explorer
from config.llm_config import LLMConfig
from config.llm_config import SELECTED_PROVIDER

# ===== PATHS =====
MEMORY_FOLDER         = "src/memory"
DB_JSON_FOLDER        = "src/outputs/DB_as_json"
PATTERNS_FILE         = os.path.join(MEMORY_FOLDER, "patterns_final.json")
UNDERSTANDING_FILE    = os.path.join(MEMORY_FOLDER, "understanding.json")
ENRICHMENT_FILE       = os.path.join(MEMORY_FOLDER, "enrichment.json")
TABLES_STRUCTURE_FILE = os.path.join(DB_JSON_FOLDER, "tables_structure.json")
ONTOLOGY_FILE         = "src/inputs/ontology/ontology.owl"
OUTPUT_DIR            = "src/outputs"
MAPPINGS_DIR          = os.path.join(OUTPUT_DIR, "mappings")
SE_MAPPINGS_FILE      = os.path.join(MAPPINGS_DIR, "SE_mappings.json")
PROCESS_FILE          = os.path.join(OUTPUT_DIR, "mappings_process_sh.json")
SH_MAPPINGS_FILE      = os.path.join(MAPPINGS_DIR, "SH_mappings.json")

RDFS_LABEL = "rdfs:label"

# Column name fragments that suggest a human-readable label
LABEL_HINTS = {
    "label", "name", "title", "display", "caption",
    "text", "heading", "description", "summary", "short"
}

# Meaning-text fragments that also suggest a human-readable label
LABEL_MEANING_HINTS = {
    "name", "title", "label", "caption", "display",
    "text", "heading", "description", "summary",
    "human-readable", "identifier", "short"
}



# ============================================================
# Ontology prefix parser (same as phase 1)
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
            base = root.get("{http://www.w3.org/XML/1998/namespace}base") or root.get("ontologyIRI", "")
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
# XSD helpers (same as phase 1)
# ============================================================

XSD_MAP = {
    "integer":   "xsd:integer",  "int":       "xsd:integer",
    "bigint":    "xsd:integer",  "smallint":  "xsd:integer",
    "boolean":   "xsd:boolean",  "bool":      "xsd:boolean",
    "float":     "xsd:decimal",  "double":    "xsd:decimal",
    "numeric":   "xsd:decimal",  "decimal":   "xsd:decimal",
    "real":      "xsd:decimal",  "date":      "xsd:date",
    "timestamp": "xsd:dateTime", "datetime":  "xsd:dateTime",
    "time":      "xsd:time",     "character": "xsd:string",
    "varchar":   "xsd:string",   "text":      "xsd:string",
    "char":      "xsd:string",
}

def _xsd_type(data_type: str) -> str:
    return XSD_MAP.get(data_type.lower().split("(")[0].strip(), "xsd:string")

def _to_camel_case(name: str) -> str:
    parts = name.split("_")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


# ============================================================
# Safe JSON loader
# ============================================================

def load_json_safe(path: str) -> Dict:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Required file not found: {path}")
    if os.path.getsize(path) == 0:
        raise ValueError(f"File is empty (0 bytes): {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in '{path}': {e}")


def load_json_optional(path: str) -> Dict:
    """Load JSON file — returns empty dict if missing, empty, or corrupt."""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        print(f"  [WARN] Could not parse '{path}' — starting fresh")
        return {}


# ============================================================
# Parent resolution
# ============================================================

def get_direct_parent(table_name: str, tables_structure: Dict):
    """
    Return (parent_table, parent_pk_col) for a SE_SH table
    by finding the 'id' column that is a FK.
    Returns (None, None) if not found.
    """
    info = tables_structure.get(table_name, {})
    for col in info.get("columns", []):
        if col["name"] == "id" and col.get("is_foreign_key"):
            ref = col.get("foreign_key_reference", {})
            return ref.get("table"), ref.get("column", "id")
    return None, None


def resolve_subject_template(
    table_name: str,
    tables_structure: Dict,
    se_mappings: Dict,
    sh_mappings: Dict,
    base_iri: str,
) -> tuple:
    """
    Determine the subject template and whether the parent is resolved.

    Logic:
      1. Find the direct parent table via the id FK.
      2. If parent is in SE_mappings (Phase 1) → use parent's template, resolved=True.
      3. If parent is in SH_mappings (Phase 2, already processed) → use parent's template, resolved=True.
      4. Otherwise → use own IRI template, resolved=False (placeholder).

    Returns:
        (template_str, parent_table, parent_triple_map_iri, resolved)
    """
    parent_table, _ = get_direct_parent(table_name, tables_structure)

    if parent_table is None:
        # No parent found — fall back to own IRI
        own_template = f"{base_iri}{table_name}/{{id}}"
        return own_template, None, None, False

    # Check Phase 1 SE mappings
    if parent_table in se_mappings:
        parent_iri      = se_mappings[parent_table]["triple_map_iri"]
        parent_template = se_mappings[parent_table]["subject"]["template"]
        return parent_template, parent_table, parent_iri, True

    # Check Phase 2 SH mappings (already processed in this run)
    if parent_table in sh_mappings:
        parent_iri      = sh_mappings[parent_table]["triple_map_iri"]
        parent_template = sh_mappings[parent_table]["subject"]["template"]
        return parent_template, parent_table, parent_iri, True

    # Parent not yet mapped — use own IRI as placeholder
    own_template    = f"{base_iri}{table_name}/{{id}}"
    parent_iri      = f"urn:r2rml:SE_SH_{parent_table}"
    return own_template, parent_table, parent_iri, False


# ============================================================
# Ontology Property Index (subclass hierarchy)
# ============================================================

class OntologyPropertyIndex:

    def __init__(self, owl_file: str):
        self.data_props:  Dict = {}
        self.obj_props:   Dict = {}
        self.subclass_of: Dict = defaultdict(set)
        self._parse(owl_file)
        self._close_subclass()

    def _local(self, iri: str) -> str:
        return iri.split("#")[-1] if "#" in iri else iri.split("/")[-1]

    def _parse(self, owl_file: str):
        root = ET.parse(owl_file).getroot()
        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag != "Declaration":
                continue
            ch = list(elem)
            if not ch:
                continue
            child_tag = ch[0].tag.split("}")[-1] if "}" in ch[0].tag else ch[0].tag
            iri = ch[0].get("IRI", "") or ch[0].get("abbreviatedIRI", "")
            if not iri:
                continue
            if child_tag == "DataProperty":
                self.data_props[self._local(iri)] = {"domain": None, "domain_union": None, "range": None}
            elif child_tag == "ObjectProperty":
                self.obj_props[self._local(iri)] = {"domain": None, "range": None}

        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag == "DataPropertyDomain":
                ch = list(elem)
                if len(ch) < 2:
                    continue
                p  = self._local(ch[0].get("IRI", "") or ch[0].get("abbreviatedIRI", ""))
                de = ch[1]
                dt = de.tag.split("}")[-1] if "}" in de.tag else de.tag
                if p not in self.data_props:
                    continue
                if dt == "ObjectUnionOf":
                    members = [
                        self._local(c.get("IRI", "") or c.get("abbreviatedIRI", ""))
                        for c in de if c.get("IRI", "") or c.get("abbreviatedIRI", "")
                    ]
                    if members:
                        self.data_props[p]["domain"]       = members[0]
                        self.data_props[p]["domain_union"]  = set(members)
                else:
                    d = self._local(de.get("IRI", ""))
                    if d:
                        self.data_props[p]["domain"] = d
            elif tag == "DataPropertyRange":
                ch = list(elem)
                if len(ch) < 2:
                    continue
                p = self._local(ch[0].get("IRI", "") or ch[0].get("abbreviatedIRI", ""))
                r = ch[1].get("IRI", "") or ch[1].get("abbreviatedIRI", "")
                if p in self.data_props:
                    self.data_props[p]["range"] = r
            elif tag == "ObjectPropertyDomain":
                ch = list(elem)
                if len(ch) < 2:
                    continue
                p = self._local(ch[0].get("IRI", "") or ch[0].get("abbreviatedIRI", ""))
                d = self._local(ch[1].get("IRI", "") or ch[1].get("abbreviatedIRI", ""))
                if p in self.obj_props and d:
                    self.obj_props[p]["domain"] = d
            elif tag == "ObjectPropertyRange":
                ch = list(elem)
                if len(ch) < 2:
                    continue
                p = self._local(ch[0].get("IRI", "") or ch[0].get("abbreviatedIRI", ""))
                r = self._local(ch[1].get("IRI", "") or ch[1].get("abbreviatedIRI", ""))
                if p in self.obj_props and r:
                    self.obj_props[p]["range"] = r

        for elem in root.iter():
            if not elem.tag.endswith("}SubClassOf"):
                continue
            ch = list(elem)
            if len(ch) != 2:
                continue
            sub_iri = ch[0].get("IRI", "") or ch[0].get("abbreviatedIRI", "")
            sup_iri = ch[1].get("IRI", "") or ch[1].get("abbreviatedIRI", "")
            sub = self._local(sub_iri)
            sup = self._local(sup_iri)
            if sub and sup and sup not in ("Thing", ""):
                self.subclass_of[sub].add(sup)

    def _close_subclass(self):
        changed = True
        while changed:
            changed = False
            for cls, parents in list(self.subclass_of.items()):
                for parent in list(parents):
                    new = self.subclass_of.get(parent, set()) - parents
                    if new:
                        self.subclass_of[cls].update(new)
                        changed = True

    def get_ancestors(self, cls: str) -> Set[str]:
        return {cls} | self.subclass_of.get(cls, set())


# ============================================================
# Property name similarity matcher — duplicate-aware
# ============================================================

_OWL_STRIP_PREFIXES = (
    "has_an_", "has_a_", "has_the_", "has_",
    "is_an_",  "is_a_",  "is_the_",  "is_",
)

def _strip_owl_prefix(name: str) -> str:
    pl = name.lower()
    for pfx in _OWL_STRIP_PREFIXES:
        if pl.startswith(pfx) and len(name) > len(pfx):
            return name[len(pfx):]
    return name


def _norm(s: str) -> str:
    return s.lower().replace("_", "").replace("-", "")


def _match_property(
    col_name:        str,
    prop_list:       List[str],
    used_predicates: Set[str],
) -> Optional[str]:
    col_norm = _norm(col_name)

    def _already_used(prop: str) -> bool:
        return f":{prop}" in used_predicates

    # Pass 1 — exact normalised match
    for p in prop_list:
        if _norm(p) == col_norm or _norm(_strip_owl_prefix(p)) == col_norm:
            if _already_used(p):
                print(f"        [SKIP P1-exact — already used] col={col_name!r} prop={p!r}")
                continue
            print(f"        [MATCH P1-exact]    col={col_name!r}  →  prop={p!r}")
            return p

    # Pass 2 — col contained in prop
    for p in prop_list:
        if col_norm in _norm(p) or col_norm in _norm(_strip_owl_prefix(p)):
            if _already_used(p):
                print(f"        [SKIP P2-col⊂prop — already used] col={col_name!r} prop={p!r}")
                continue
            print(f"        [MATCH P2-col⊂prop] col={col_name!r}  →  prop={p!r}")
            return p

    # Pass 3 — prop contained in col
    for p in prop_list:
        if _norm(p) in col_norm or _norm(_strip_owl_prefix(p)) in col_norm:
            if _already_used(p):
                print(f"        [SKIP P3-prop⊂col — already used] col={col_name!r} prop={p!r}")
                continue
            print(f"        [MATCH P3-prop⊂col] col={col_name!r}  →  prop={p!r}")
            return p

    print(f"        [NO MATCH] col={col_name!r}  (checked {len(prop_list)} props, "
          f"{len(used_predicates)} already used)")
    return None


# ============================================================
# Used-predicate tracker (seeded from disk)
# ============================================================

def _load_used_predicates(table_name: str) -> Set[str]:
    if not os.path.exists(SH_MAPPINGS_FILE):
        return set()
    try:
        with open(SH_MAPPINGS_FILE, "r", encoding="utf-8") as f:
            existing = json.load(f)
        entry = existing.get(table_name, {})
        return {
            pm["predicate"]
            for pm in entry.get("predicate_object_maps", [])
            if pm.get("predicate")
        }
    except Exception:
        return set()


# ============================================================
# Class properties fetcher — via ontology_explorer
# ============================================================

def _fetch_class_properties(ontology_class: str) -> Tuple[List[str], List[str]]:
    try:
        result = ontology_explorer(mode="class_properties", class_name=ontology_class)
    except Exception as e:
        print(f"    [WARN] ontology_explorer failed for {ontology_class!r}: {e}")
        return [], []

    print(f"\n    ┌── ontology_explorer — class_properties({ontology_class!r}) ──┐")

    data_props: List[str] = []
    obj_props:  List[str] = []

    if isinstance(result, dict):
        dp_raw = (
            result.get("data_properties")
            or result.get("dataProperties")
            or result.get("data_props")
            or (result.get("properties") if isinstance(result.get("properties"), dict) else {}).get("data", [])
            or []
        )
        op_raw = (
            result.get("object_properties")
            or result.get("objectProperties")
            or result.get("object_props")
            or (result.get("properties") if isinstance(result.get("properties"), dict) else {}).get("object", [])
            or []
        )

        if not dp_raw and not op_raw and "properties" in result:
            flat = result["properties"]
            if isinstance(flat, list):
                for item in flat:
                    if isinstance(item, dict):
                        ptype = item.get("type", "").lower()
                        pname = item.get("name") or item.get("iri", "")
                        pname = pname.split("#")[-1] if "#" in pname else pname.split("/")[-1]
                        if "object" in ptype:
                            op_raw.append(pname)
                        else:
                            dp_raw.append(pname)
                    elif isinstance(item, str):
                        dp_raw.append(item)

        def _to_local(entry) -> str:
            if isinstance(entry, str):
                return entry.split("#")[-1] if "#" in entry else entry.split("/")[-1]
            if isinstance(entry, dict):
                n = entry.get("name") or entry.get("local_name") or entry.get("iri", "")
                return n.split("#")[-1] if "#" in n else n.split("/")[-1]
            return str(entry)

        data_props = [_to_local(e) for e in dp_raw if e]
        obj_props  = [_to_local(e) for e in op_raw if e]

    print(f"    │  data_properties   ({len(data_props)}): {data_props}")
    print(f"    │  object_properties ({len(obj_props)}): {obj_props}")
    print(f"    └────────────────────────────────────────\n")
    return data_props, obj_props


# ============================================================
# LLM-assisted column → predicate selection
# ============================================================

def _build_column_predicate_prompt(
    table_name:      str,
    table_meaning:   str,
    ontology_class:  str,
    col_name:        str,
    col_meaning:     str,
    col_db_type:     str,
    available_props: List[str],
    used_predicates: Set[str],
    prop_kind:       str,
) -> str:
    props_block = (
        "\n".join(f"  - {p}" for p in available_props)
        if available_props else "  (none available)"
    )
    used_block = (
        "\n".join(f"  - {p}" for p in sorted(used_predicates))
        if used_predicates else "  (none yet)"
    )
    kind_instruction = (
        "Choose the BEST data property to represent this column's literal value."
        if prop_kind == "data"
        else "Choose the BEST object property to represent this foreign-key relationship."
    )

    return f"""You are an ontology mapping expert.

TABLE: {table_name}
TABLE MEANING: {table_meaning}
ONTOLOGY CLASS MAPPED TO THIS TABLE: :{ontology_class}

COLUMN TO MAP:
  name     : {col_name}
  meaning  : {col_meaning}
  db type  : {col_db_type}

AVAILABLE ONTOLOGY {prop_kind.upper()} PROPERTIES (already-used ones excluded):
{props_block}

PREDICATES ALREADY ASSIGNED TO OTHER COLUMNS IN THIS TABLE:
{used_block}

TASK: {kind_instruction}

Rules:
  - Use the column's SEMANTIC MEANING as the primary guide, not just its name.
  - Only choose from the AVAILABLE properties listed above.
  - Do NOT choose a predicate already listed under "PREDICATES ALREADY ASSIGNED".
  - If none of the available properties semantically fits this column, return null.
  - Return ONLY a JSON object, no markdown, no extra text:

{{
  "chosen_property": "PropertyName"
}}

If no property fits, return: {{"chosen_property": null}}"""


def _llm_select_predicate(
    table_name:      str,
    table_meaning:   str,
    ontology_class:  str,
    col_name:        str,
    col_meaning:     str,
    col_db_type:     str,
    available_props: List[str],
    used_predicates: Set[str],
    prop_kind:       str,
    mapper:          "OntologyMapper",
) -> Optional[str]:
    if not available_props:
        print(f"        [LLM-PRED] No available props — skipping for {col_name!r}")
        return None

    prompt = _build_column_predicate_prompt(
        table_name, table_meaning, ontology_class,
        col_name, col_meaning, col_db_type,
        available_props, used_predicates, prop_kind
    )
    print(f"        [LLM-PRED] Asking LLM for {prop_kind} property for "
          f"{col_name!r} (meaning: {col_meaning!r})")
    raw = mapper.get_llm_response(prompt)
    print(f"        [LLM-PRED] Raw: {raw[:200]!r}")

    try:
        cleaned = re.sub(r'```json\s*', '', raw)
        cleaned = re.sub(r'```\s*', '', cleaned).strip()
        j_start = cleaned.find("{")
        j_end   = cleaned.rfind("}") + 1
        if j_start != -1 and j_end > 0:
            obj    = json.loads(cleaned[j_start:j_end])
            chosen = obj.get("chosen_property")
            if chosen and chosen in available_props:
                print(f"        [LLM-PRED] ✓ chose: {chosen!r}")
                return chosen
            elif chosen is None or chosen == "null":
                print(f"        [LLM-PRED] LLM returned null — no suitable property")
                return None
            else:
                print(f"        [LLM-PRED] {chosen!r} not in available props — ignoring")
                return None
    except Exception as e:
        print(f"        [LLM-PRED] Parse error: {e}")

    # Regex fallback
    m = re.search(r'"chosen_property"\s*:\s*"([^"]+)"', raw)
    if m:
        chosen = m.group(1)
        if chosen in available_props:
            print(f"        [LLM-PRED] ✓ chose (regex fallback): {chosen!r}")
            return chosen
    return None


# ============================================================
# rdfs:label column detector — LLM call, duplicate-aware
# ============================================================

def _format_all_col_meanings(col_meanings: Dict[str, str]) -> str:
    if not col_meanings:
        return "  (none available)"
    return "\n".join(f"  - {col}: {meaning}" for col, meaning in col_meanings.items())


def _build_label_prompt(
    table_name:      str,
    ontology_class:  str,
    candidates:      List[str],
    col_meanings:    Dict[str, str],
    table_meaning:   str,
    used_predicates: Set[str],
) -> str:
    col_lines = []
    for col in candidates:
        meaning = col_meanings.get(col, "(no description available)")
        col_lines.append(f"  - {col}: {meaning}")
    col_block  = "\n".join(col_lines)
    used_block = (
        "\n".join(f"  - {p}" for p in sorted(used_predicates))
        if used_predicates else "  (none yet)"
    )
    rdfs_already = RDFS_LABEL in used_predicates
    rdfs_note = (
        "\nNOTE: rdfs:label has already been assigned to an earlier column in this "
        "table. Only add more columns here if they represent a genuinely distinct "
        "human-readable label (rare). When in doubt, return an empty list."
        if rdfs_already else ""
    )

    return f"""You are an ontology mapping expert.

TABLE: {table_name}
TABLE MEANING: {table_meaning}
ONTOLOGY CLASS: {ontology_class}

FULL COLUMN DESCRIPTIONS (from understanding.json):
{_format_all_col_meanings(col_meanings)}

CANDIDATE COLUMNS FOR rdfs:label:
{col_block}

PREDICATES ALREADY ASSIGNED TO OTHER COLUMNS IN THIS TABLE:
{used_block}
{rdfs_note}

Decide which of the CANDIDATE columns (if any) should be mapped to rdfs:label —
the standard RDF property for a short, human-readable string that identifies an
individual instance (e.g. a person's display name, a paper title, an award name).

Rules:
  - Only include a column if it clearly represents a human-readable label or name.
  - Do NOT include purely technical identifiers, codes, or numeric fields.
  - Do NOT repeat a predicate already listed under "PREDICATES ALREADY ASSIGNED".
  - A table can have zero, one, or more rdfs:label columns.

Return ONLY a JSON object, no markdown, no extra text:

{{
  "rdfs_label_columns": ["col1", "col2"]
}}

If no column qualifies, return: {{"rdfs_label_columns": []}}"""


def decide_label_columns(
    table_name:      str,
    ontology_class:  str,
    attr_cols:       List[Dict],
    col_meanings:    Dict[str, str],
    table_meaning:   str,
    used_predicates: Set[str],
    mapper:          "OntologyMapper",
) -> Set[str]:
    """
    Ask the LLM which non-FK attribute columns should use rdfs:label.
    Pre-filters by column name OR by column meaning (Fix: meaning-aware pre-filter).
    """
    candidates = [
        a["name"] for a in attr_cols
        if any(hint in _norm(a["name"]) for hint in LABEL_HINTS)
        or any(hint in col_meanings.get(a["name"], "").lower() for hint in LABEL_MEANING_HINTS)
    ]

    if not candidates:
        print(f"    [LABEL] No label-hint candidates — skipping LLM label call")
        return set()

    prompt = _build_label_prompt(
        table_name, ontology_class, candidates,
        col_meanings, table_meaning, used_predicates
    )
    print(f"    [LABEL] Candidates: {candidates}")

    raw = mapper.get_llm_response(prompt)

    label_cols: List[str] = []
    try:
        cleaned = re.sub(r'```json\s*', '', raw)
        cleaned = re.sub(r'```\s*', '', cleaned).strip()
        j_start = cleaned.find("{")
        j_end   = cleaned.rfind("}") + 1
        if j_start != -1 and j_end > 0:
            obj = json.loads(cleaned[j_start:j_end])
            label_cols = obj.get("rdfs_label_columns", [])
    except Exception:
        m = re.search(r'"rdfs_label_columns"\s*:\s*\[([^\]]*)\]', raw)
        if m:
            label_cols = [s.strip().strip('"') for s in m.group(1).split(",") if s.strip()]

    valid      = set(candidates)
    label_cols = [c for c in label_cols if c in valid]
    print(f"    [LABEL] rdfs:label columns decided: {label_cols}")
    return set(label_cols)


# ============================================================
# SE_SH JSON mapping builder
# ============================================================

def build_sh_json_mapping(
    table_name:     str,
    ontology_class: str,
    attributes:     List[Dict],
    base_iri:       str,
    tables_structure: Dict,
    se_mappings:    Dict,
    sh_mappings:    Dict,
    all_se_sh_tables: set,
    prop_index:     OntologyPropertyIndex,
    mapper:         "OntologyMapper",
    col_meanings:   Dict[str, str],
    table_meaning:  str,
) -> Dict:
    """
    Build the structured JSON mapping for one SE_SH table.

    Subject template:
      - Uses parent's template if parent is resolved (already mapped in SE or SH phase).
      - Uses own IRI if parent is not yet mapped (placeholder, resolved=False).

    Column → predicate resolution:
      Non-FK attribute columns:
        1. rdfs:label  — LLM label call (meaning-aware pre-filter)
        2. String similarity match against data properties
        3. LLM semantic selection (uses column meaning from understanding.json)
        4. camelCase fallback
      FK columns:
        1. String similarity match against object properties
        2. LLM semantic selection
        3. camelCase fallback

    pk+fk 'id' column → subject identity only, never a predicate.
    """
    triple_map_iri = f"urn:r2rml:SE_SH_{table_name}"

    # Resolve subject template from parent chain
    subject_template, parent_table, parent_triple_map_iri, parent_resolved = \
        resolve_subject_template(table_name, tables_structure, se_mappings, sh_mappings, base_iri)

    # Split attributes by role
    attr_cols = [a for a in attributes if a["role"] == "attribute"]
    fk_cols   = [a for a in attributes if a["role"] == "fk"]

    print(f"\n    [BUILD] table={table_name!r}  class={ontology_class!r}")
    print(f"            attr: {[c['name'] for c in attr_cols]}")
    print(f"            fk  : {[c['name'] for c in fk_cols]}")

    # ── Step 1: seed used_predicates from disk ─────────────────────────────
    used_predicates: Set[str] = _load_used_predicates(table_name)
    if used_predicates:
        print(f"    [DEDUP] Seeded {len(used_predicates)} used predicate(s) from disk: "
              f"{sorted(used_predicates)}")

    # ── Step 2: fetch class properties from ontology_explorer ─────────────
    data_prop_names, obj_prop_names = _fetch_class_properties(ontology_class)

    # ── Step 3: LLM decides which attribute columns → rdfs:label ──────────
    label_columns = decide_label_columns(
        table_name      = table_name,
        ontology_class  = ontology_class,
        attr_cols       = attr_cols,
        col_meanings    = col_meanings,
        table_meaning   = table_meaning,
        used_predicates = used_predicates,
        mapper          = mapper,
    )

    predicate_object_maps = []

    # ── Step 4: map non-FK attribute columns ──────────────────────────────
    print(f"\n    ── Attribute (non-FK) column mapping ──")
    for attr in attr_cols:
        col         = attr["name"]
        col_meaning = col_meanings.get(col, "(no description)")
        col_db_type = attr.get("data_type", "unknown")
        print(f"\n      Processing col={col!r}  meaning={col_meaning!r}  db_type={col_db_type!r}")
        print(f"      used_predicates so far: {sorted(used_predicates)}")

        # ── rdfs:label is ADDITIVE — resolve the real data property first,
        #    then append a second rdfs:label entry for the same column.
        emit_rdfs_label = col in label_columns

        if data_prop_names:
            # Priority 1: string similarity (fast)
            matched = _match_property(col, data_prop_names, used_predicates)
            if matched:
                predicate = f":{matched}"
                datatype  = _xsd_type(col_db_type)
                print(f"      → {col!r}  :  {predicate}  [string-match data property]")
            else:
                # Priority 2: LLM semantic selection
                unused_data_props = [p for p in data_prop_names if f":{p}" not in used_predicates]
                chosen = _llm_select_predicate(
                    table_name, table_meaning, ontology_class,
                    col, col_meaning, col_db_type,
                    unused_data_props, used_predicates, "data", mapper
                ) if unused_data_props else None
                if chosen:
                    predicate = f":{chosen}"
                    datatype  = _xsd_type(col_db_type)
                    print(f"      → {col!r}  :  {predicate}  [LLM semantic data property]")
                else:
                    predicate = f":{_to_camel_case(col)}"
                    datatype  = _xsd_type(col_db_type)
                    print(f"      → {col!r}  :  {predicate}  [FALLBACK camelCase]")
        else:
            predicate = f":{_to_camel_case(col)}"
            datatype  = _xsd_type(col_db_type)
            print(f"      → {col!r}  :  {predicate}  [FALLBACK camelCase — no class properties]")

        used_predicates.add(predicate)
        predicate_object_maps.append({
            "predicate": predicate,
            "object": {
                "type":     "literal",
                "column":   col,
                "datatype": datatype,
            }
        })

        # ── Additive rdfs:label entry ────────────────────────────────────────
        if emit_rdfs_label and predicate != RDFS_LABEL:
            print(f"      + adding additive rdfs:label for {col!r} "
                  f"(in addition to {predicate!r})")
            predicate_object_maps.append({
                "predicate": RDFS_LABEL,
                "object": {
                    "type":     "literal",
                    "column":   col,
                    "datatype": "xsd:string",
                }
            })

    # ── Step 5: map FK columns ────────────────────────────────────────────
    print(f"\n    ── FK column mapping ──")
    for fk in fk_cols:
        col         = fk["name"]
        col_meaning = col_meanings.get(col, "(no description)")
        col_db_type = fk.get("data_type", "unknown")
        ref_table   = fk.get("fk_references", {}).get("table", "unknown")
        ref_col     = fk.get("fk_references", {}).get("column", "id")
        resolved    = ref_table in se_mappings or ref_table in sh_mappings or ref_table in all_se_sh_tables

        if ref_table in se_mappings:
            fk_parent_iri = se_mappings[ref_table]["triple_map_iri"]
        elif ref_table in sh_mappings:
            fk_parent_iri = sh_mappings[ref_table]["triple_map_iri"]
        else:
            fk_parent_iri = f"urn:r2rml:SE_SH_{ref_table}"

        print(f"\n      Processing FK col={col!r}  ref={ref_table!r}  meaning={col_meaning!r}")
        print(f"      used_predicates so far: {sorted(used_predicates)}")

        if obj_prop_names:
            matched = _match_property(col, obj_prop_names, used_predicates)
            if matched:
                predicate = f":{matched}"
                print(f"      → {col!r} (FK→{ref_table})  :  {predicate}  [string-match object property]")
            else:
                unused_obj_props = [p for p in obj_prop_names if f":{p}" not in used_predicates]
                chosen = _llm_select_predicate(
                    table_name, table_meaning, ontology_class,
                    col, col_meaning, col_db_type,
                    unused_obj_props, used_predicates, "object", mapper
                ) if unused_obj_props else None
                if chosen:
                    predicate = f":{chosen}"
                    print(f"      → {col!r} (FK→{ref_table})  :  {predicate}  [LLM semantic object property]")
                else:
                    predicate = f":{_to_camel_case(col)}"
                    print(f"      → {col!r} (FK→{ref_table})  :  {predicate}  [FALLBACK camelCase]")
        else:
            predicate = f":{_to_camel_case(col)}"
            print(f"      → {col!r} (FK→{ref_table})  :  {predicate}  [FALLBACK camelCase — no obj properties]")

        used_predicates.add(predicate)
        predicate_object_maps.append({
            "predicate": predicate,
            "object": {
                "type":               "join",
                "parent_triples_map": fk_parent_iri,
                "resolved":           resolved,
                "join_condition": {
                    "child":  col,
                    "parent": ref_col,
                }
            }
        })

    print(f"\n    [BUILD DONE] {table_name!r} — total predicates: {len(used_predicates)}")
    print(f"                 final predicate set: {sorted(used_predicates)}")

    return {
        "pattern":               "SE_SH",
        "triple_map_iri":        triple_map_iri,
        "logical_table":         table_name,
        "parent_table":          parent_table,
        "parent_triple_map_iri": parent_triple_map_iri,
        "parent_resolved":       parent_resolved,
        "subject": {
            "template": subject_template,
            "class":    f":{ontology_class}"
        },
        "predicate_object_maps": predicate_object_maps,
    }


# ============================================================
# OntologyMapper (LLM agent) — same as phase 1
# ============================================================

class OntologyMapper:

    def __init__(self, provider: str = SELECTED_PROVIDER):
        self.provider = provider
        self.config   = LLMConfig.get_config(provider)
        print(f"Initialized OntologyMapper with provider: {provider}")
        print(f"Model: {self.config['model_name']}")

    def strip_thinking_tags(self, text: str) -> str:
        return re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()

    def _has_json(self, text: str) -> bool:
        cleaned = self.strip_thinking_tags(text)
        j_start = cleaned.find('{')
        j_end   = cleaned.rfind('}') + 1
        if j_start == -1 or j_end == 0:
            return False
        try:
            json.loads(cleaned[j_start:j_end])
            return True
        except Exception:
            return '"ontology_class"' in cleaned

    def get_llm_response(self, prompt: str) -> str:
        if self.provider == "claude":
            return self._get_claude_response(prompt)
        elif self.provider == "gemini":
            return self._get_gemini_response(prompt)
        else:
            return self._get_openai_compatible_response(prompt)

    _RETRYABLE_STATUS = {429, 500, 502, 503, 504}
    _MAX_RETRIES      = 5
    _BACKOFF_BASE     = 2   # wait = base * 2^attempt  (2, 4, 8, 16, 32s)

    def _should_retry(self, status_code: int) -> bool:
        return status_code in self._RETRYABLE_STATUS

    def _wait(self, attempt: int, status_code: int) -> None:
        wait = self._BACKOFF_BASE * (2 ** attempt)
        print(f"\n  [RETRY {attempt + 1}/{self._MAX_RETRIES}] HTTP {status_code} — "
              f"retrying in {wait}s ...", flush=True)
        time.sleep(wait)

    def _get_openai_compatible_response(self, prompt: str) -> str:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config['api_key']}"
        }
        data = {
            "model":       self.config['model_name'],
            "messages":    [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens":  1024
        }

        for attempt in range(self._MAX_RETRIES):
            response = requests.post(self.config['api_url'], headers=headers, json=data)
            if response.status_code == 200:
                break
            if self._should_retry(response.status_code) and attempt < self._MAX_RETRIES - 1:
                self._wait(attempt, response.status_code)
                continue
            raise Exception(f"API request failed: {response.status_code} - {response.text}")

        raw = response.json()["choices"][0]["message"]["content"]

        if self.provider == "groq" and not self._has_json(raw):
            print(f"\n  [RETRY] Model only returned thinking, requesting JSON output...", end="", flush=True)
            retry_data = {
                "model": self.config['model_name'],
                "messages": [
                    {"role": "user",      "content": prompt},
                    {"role": "assistant", "content": raw},
                    {"role": "user",      "content": (
                        "You only produced reasoning but no JSON output. "
                        "Now output ONLY the JSON object as instructed. "
                        "No thinking, no explanation, just the raw JSON."
                    )}
                ],
                "temperature": 0.1,
                "max_tokens":  1024
            }
            retry_resp = requests.post(self.config['api_url'], headers=headers, json=retry_data)
            if retry_resp.status_code == 200:
                raw = retry_resp.json()["choices"][0]["message"]["content"]

        if self.provider == "groq":
            raw = self.strip_thinking_tags(raw)
        return raw

    def _get_claude_response(self, prompt: str) -> str:
        headers = {
            "Content-Type": "application/json",
            "x-api-key":    self.config['api_key'],
            "anthropic-version": "2023-06-01"
        }
        data = {
            "model":       self.config['model_name'],
            "max_tokens":  1024,
            "messages":    [{"role": "user", "content": prompt}],
            "temperature": 0.3
        }
        for attempt in range(self._MAX_RETRIES):
            response = requests.post(self.config['api_url'], headers=headers, json=data)
            if response.status_code == 200:
                break
            if self._should_retry(response.status_code) and attempt < self._MAX_RETRIES - 1:
                self._wait(attempt, response.status_code)
                continue
            raise Exception(f"Claude API request failed: {response.status_code} - {response.text}")
        return response.json()["content"][0]["text"]

    def _get_gemini_response(self, prompt: str) -> str:
        url = f"{self.config['api_url']}/{self.config['model_name']}:generateContent?key={self.config['api_key']}"
        headers = {"Content-Type": "application/json"}
        data = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.3, "maxOutputTokens": 1024}
        }
        for attempt in range(self._MAX_RETRIES):
            response = requests.post(url, headers=headers, json=data)
            if response.status_code == 200:
                break
            if self._should_retry(response.status_code) and attempt < self._MAX_RETRIES - 1:
                self._wait(attempt, response.status_code)
                continue
            raise Exception(f"Gemini API request failed: {response.status_code} - {response.text}")
        return response.json()["candidates"][0]["content"]["parts"][0]["text"]

    def _build_attributes(self, table_name: str, tables_structure: Dict) -> List[Dict]:
        info    = tables_structure.get(table_name, {})
        pk_set  = set(info.get("primary_keys", []))
        columns = info.get("columns", [])

        # Implicit pk+fk: no declared PKs but "id" is FK
        has_explicit_pk  = len(pk_set) > 0
        implicit_pk_cols: set = set()
        if not has_explicit_pk:
            for col in columns:
                if col["name"] == "id" and col.get("is_foreign_key"):
                    implicit_pk_cols.add("id")

        result = []
        for col in columns:
            is_pk  = col.get("is_primary_key", False) or col["name"] in implicit_pk_cols
            is_fk  = col.get("is_foreign_key", False)
            fk_ref = col.get("foreign_key_reference")

            if is_pk and is_fk:
                role = "pk+fk"
            elif is_pk:
                role = "pk"
            elif is_fk:
                role = "fk"
            else:
                role = "attribute"

            attr = {
                "name":      col["name"],
                "data_type": col.get("data_type", "unknown"),
                "role":      role,
                "nullable":  col.get("is_nullable", True),
            }
            if is_fk and fk_ref:
                attr["fk_references"] = {"table": fk_ref["table"], "column": fk_ref["column"]}
            result.append(attr)
        return result

    def _build_mapping_prompt(
        self,
        table_name: str,
        table_meaning: str,
        entity_type: str,
        parent_table: Optional[str],
        column_meanings: Dict[str, str],
        column_enrichment: Dict[str, Dict],
        enum_interpretations: Dict[str, Dict],
        ontology_classes: List[str],
        used_classes: Optional[Set[str]] = None,
    ) -> str:
        col_lines = []
        for col_name, meaning in column_meanings.items():
            role_hint = column_enrichment.get(col_name, {}).get("role", "")
            line = f"  - {col_name}: {meaning}"
            if role_hint:
                line += f"  [role: {role_hint}]"
            col_lines.append(line)
        columns_block = "\n".join(col_lines) if col_lines else "  (none)"

        enums_block = ""
        if enum_interpretations:
            lines = [f"  {col}: {vals}" for col, vals in enum_interpretations.items()]
            enums_block = "\nENUM / TYPE COLUMNS:\n" + "\n".join(lines)

        parent_hint = f"\nPARENT TABLE: {parent_table} (this table inherits/extends it)" if parent_table else ""

        # Filter out already-used classes so LLM can't double-assign
        available = [c for c in ontology_classes
                     if not (used_classes and c in used_classes)]

        return f"""You are an ontology mapping expert. Find the SINGLE best matching ontology class for this database table. Important: Prefer a match that is very high both syntactically AND semantically. Prefer the class whose local name most directly matches the table name — do NOT pick a subclass when the parent class name is a better syntactic match.
This table is a SUBCLASS — it extends a parent table via a shared primary key.
{parent_hint}

TABLE: {table_name}
TABLE MEANING: {table_meaning}
ENTITY TYPE: {entity_type}

COLUMNS (with meanings and roles):
{columns_block}
{enums_block}
AVAILABLE ONTOLOGY CLASSES (classes already assigned to other tables are excluded):
{', '.join(available)}

IMPORTANT: If the table name closely matches a class name (e.g. 'abstracts' → 'Abstract',
'papers' → 'Paper'), prefer that class even if a subclass seems more specific.
Subclasses (e.g. 'Extended_abstract') should only be chosen if the table name
directly suggests them (e.g. 'extended_abstracts' → 'Extended_abstract').

Return ONLY a JSON object, no markdown, no extra text:

{{
  "ontology_class": "BestMatchClassName",
  "score": <integer 1-5>,
  "why": "One concise sentence explaining the match."
}}"""

    def _parse_mapping_response(self, response: str) -> Optional[Dict]:
        try:
            cleaned = re.sub(r'```json\s*', '', response)
            cleaned = re.sub(r'```\s*', '', cleaned).strip()
            j_start = cleaned.find("{")
            j_end   = cleaned.rfind("}") + 1
            if j_start != -1 and j_end > 0:
                obj = json.loads(cleaned[j_start:j_end])
                if "ontology_class" in obj:
                    return obj
        except (json.JSONDecodeError, ValueError):
            pass

        cls_match   = re.search(r'"ontology_class"\s*:\s*"([^"]+)"', response)
        score_match = re.search(r'"score"\s*:\s*(\d)', response)
        why_match   = re.search(r'"why"\s*:\s*"([^"]*)"', response)
        if cls_match:
            print(f"  [INFO] Mapping recovered via regex fallback")
            return {
                "ontology_class": cls_match.group(1),
                "score": int(score_match.group(1)) if score_match else None,
                "why":   why_match.group(1) if why_match else "extracted via regex fallback"
            }

        print(f"  [WARN] Could not parse mapping response")
        print(f"  [WARN] Raw: {response[:400]}")
        return None

    def map_table(
        self,
        table_name: str,
        pattern: str,
        parent_table: Optional[str],
        tables_structure: Dict,
        understanding: Dict,
        enrichment: Dict,
        ontology_classes: List[str],
        used_classes: Optional[Set[str]] = None,
    ) -> Dict:
        table_und            = understanding.get(table_name, {})
        table_meaning        = table_und.get("table_meaning", "Not available")
        column_meanings      = table_und.get("columns", {})
        table_enr            = enrichment.get(table_name, {})
        entity_type          = table_enr.get("entity_type", "unknown")
        column_enrichment    = table_enr.get("column_enrichment", {})
        enum_interpretations = table_enr.get("enum_interpretations", {})

        print(f"  entity_type={entity_type}  parent={parent_table}  columns={len(column_meanings)}")

        attributes = self._build_attributes(table_name, tables_structure)

        # ── Canonical name-match: skip LLM if table name ≡ class name ────────
        # Normalise both to lowercase, strip underscores/hyphens, and remove
        # a trailing 's' (plural). If any class matches, use it directly.
        # This prevents the LLM from picking a subclass (e.g. Extended_abstract)
        # when the superclass is the correct match (e.g. Abstract for 'abstracts').
        def _canonical_norm(s: str) -> str:
            s = s.lower().replace("_", "").replace("-", "")
            return s.rstrip("s")  # strip trailing plural 's'

        table_norm = _canonical_norm(table_name)
        canonical_class = None
        for cls in ontology_classes:
            if _canonical_norm(cls) == table_norm:
                canonical_class = cls
                print(f"  ✓ [CANONICAL] table={table_name!r} → class={cls!r}  (no LLM needed)")
                break

        if canonical_class:
            mapping = {
                "ontology_class": canonical_class,
                "score":          5,
                "why":            f"Canonical name match: '{table_name}' ≡ '{canonical_class}'"
            }
        else:
            prompt   = self._build_mapping_prompt(
                table_name, table_meaning, entity_type, parent_table,
                column_meanings, column_enrichment, enum_interpretations,
                ontology_classes, used_classes=used_classes
            )
            response = self.get_llm_response(prompt)
            mapping  = self._parse_mapping_response(response)

            if mapping:
                print(f"  ✓ → {mapping['ontology_class']}  (score {mapping.get('score')}/5)")
            else:
                print(f"  ✗ mapping failed")

        return {
            "table":         table_name,
            "pattern":       pattern,
            "parent_table":  parent_table,
            "table_meaning": table_meaning,
            "column_meanings": column_meanings,
            "entity_type":   entity_type,
            "attributes":    attributes,
            "ontology_mapping": {
                "ontology_class": mapping["ontology_class"] if mapping else None,
                "score":          mapping.get("score")      if mapping else None,
                "why":            mapping.get("why")        if mapping else "mapping failed"
            }
        }


# ============================================================
# Topological sort — process parents before children
# ============================================================

def topological_sort(se_sh_tables: Dict[str, str], tables_structure: Dict) -> List[str]:
    """
    Sort SE_SH tables so that parent tables are processed before their children.
    This ensures that when we process a child, its parent's mapping is already
    in sh_mappings and can be resolved immediately.
    """
    from collections import deque

    # Build dependency graph: table → direct parent (if also SE_SH)
    se_sh_set  = set(se_sh_tables.keys())
    dependents = {t: set() for t in se_sh_set}   # table → tables that depend on it
    in_degree  = {t: 0 for t in se_sh_set}

    for table_name in se_sh_set:
        info = tables_structure.get(table_name, {})
        for col in info.get("columns", []):
            if col["name"] == "id" and col.get("is_foreign_key"):
                parent = col.get("foreign_key_reference", {}).get("table")
                if parent in se_sh_set:
                    # table_name depends on parent
                    dependents[parent].add(table_name)
                    in_degree[table_name] += 1
                break

    # Kahn's algorithm
    queue  = deque(t for t in se_sh_set if in_degree[t] == 0)
    result = []
    while queue:
        node = queue.popleft()
        result.append(node)
        for child in dependents[node]:
            in_degree[child] -= 1
            if in_degree[child] == 0:
                queue.append(child)

    # Append any remaining (cycles, shouldn't happen in a well-formed schema)
    remaining = [t for t in se_sh_set if t not in result]
    if remaining:
        print(f"  [WARN] Could not sort: {remaining} — appending at end")
        result.extend(remaining)

    return result


# ============================================================
# Main entry point
# ============================================================

def run_sh_mapping():
    """Map all SE_SH tables and write SH_mappings.json."""

    print("=" * 55)
    print("  ONTOLOGY MAPPER — Phase 2 (SE_SH tables)")
    print("=" * 55)

    # Load required inputs
    table_patterns   = load_json_safe(PATTERNS_FILE)
    tables_structure = load_json_safe(TABLES_STRUCTURE_FILE)
    understanding    = load_json_safe(UNDERSTANDING_FILE)
    enrichment       = load_json_safe(ENRICHMENT_FILE)

    print(f"  Patterns   : {len(table_patterns)} tables")
    print(f"  Understood : {len(understanding)} tables")
    print(f"  Enriched   : {len(enrichment)} tables")

    # Load Phase 1 SE mappings (optional — warn if missing)
    os.makedirs(MAPPINGS_DIR, exist_ok=True)
    if not os.path.exists(SE_MAPPINGS_FILE) or os.path.getsize(SE_MAPPINGS_FILE) == 0:
        print(f"\n  [WARN] SE_mappings.json not found or empty.")
        print(f"         Parent resolution will use placeholders for all SE parents.")
        se_mappings = {}
    else:
        se_mappings = load_json_safe(SE_MAPPINGS_FILE)
        print(f"\n  SE mappings loaded : {len(se_mappings)} tables")

    # Parse ontology base IRI
    print(f"\nParsing ontology prefixes from '{ONTOLOGY_FILE}' ...")
    prefixes = parse_ontology_prefixes(ONTOLOGY_FILE)
    base_iri = get_ontology_base_iri(prefixes)
    print(f"  ✓ Base IRI: {base_iri}")

    print("  Building ontology property index ...")
    prop_index = OntologyPropertyIndex(ONTOLOGY_FILE)
    print(f"  ✓ {len(prop_index.data_props)} data properties, "
          f"{len(prop_index.obj_props)} object properties")

    # Filter SE_SH tables
    se_sh_tables  = {t: p for t, p in table_patterns.items() if p == "SE_SH"}
    all_se_sh_set = set(se_sh_tables.keys())
    print(f"\n  SE_SH tables : {len(se_sh_tables)}")

    # Sort topologically — parents before children
    sorted_tables = topological_sort(se_sh_tables, tables_structure)
    print(f"  Processing order: {sorted_tables}\n")

    # Load ontology classes
    print("Loading ontology classes...")
    ontology_classes = ontology_explorer(mode="classes")["classes"]
    print(f"  ✓ {len(ontology_classes)} classes loaded")

    # Load existing caches for resumable runs
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    mappings_process: Dict = load_json_optional(PROCESS_FILE)
    sh_mappings:      Dict = load_json_optional(SH_MAPPINGS_FILE)

    if mappings_process:
        print(f"\n  Loaded existing mappings_process_sh.json ({len(mappings_process)} entries)")
    if sh_mappings:
        print(f"  Loaded existing SH_mappings.json ({len(sh_mappings)} entries)")

    mapper  = OntologyMapper(provider=SELECTED_PROVIDER)
    success = 0
    errors  = []
    total   = len(sorted_tables)

    # Track classes already assigned to tables processed in this run
    # so we never assign the same class to two different tables
    used_classes: Set[str] = {
        m["ontology_mapping"]["ontology_class"]
        for m in mappings_process.values()
        if m.get("ontology_mapping", {}).get("ontology_class")
    }
    if used_classes:
        print(f"  Pre-existing used classes (from cache): {sorted(used_classes)}")

    for idx, table_name in enumerate(sorted_tables, 1):
        pattern = se_sh_tables[table_name]
        print(f"\n[{idx:>2}/{total}] {table_name}  [{pattern}]")

        # Get direct parent for prompt context
        parent_table, _ = get_direct_parent(table_name, tables_structure)

        already_mapped = table_name in mappings_process

        if not already_mapped:
            try:
                record = mapper.map_table(
                    table_name, pattern, parent_table,
                    tables_structure, understanding, enrichment,
                    ontology_classes, used_classes=used_classes
                )
                mappings_process[table_name] = record
                # Track the newly assigned class so subsequent tables can't reuse it
                assigned = record.get("ontology_mapping", {}).get("ontology_class")
                if assigned:
                    used_classes.add(assigned)
                success += 1
                with open(PROCESS_FILE, "w", encoding="utf-8") as f:
                    json.dump(mappings_process, f, indent=2)
            except Exception as e:
                print(f"  ✗ Error: {e}")
                errors.append(table_name)
                import traceback
                traceback.print_exc()
                continue
        else:
            print(f"  → already mapped, skipping LLM call")

        # Build SH JSON mapping entry
        record    = mappings_process[table_name]
        ont_class = record.get("ontology_mapping", {}).get("ontology_class")
        attributes = record.get("attributes", [])

        if ont_class:
            # col_meanings / table_meaning may be absent in older cache records
            col_meanings: Dict[str, str] = (
                record.get("column_meanings")
                or understanding.get(table_name, {}).get("columns", {})
            )
            table_meaning: str = (
                record.get("table_meaning")
                or understanding.get(table_name, {}).get("table_meaning", "Not available")
            )

            sh_mappings[table_name] = build_sh_json_mapping(
                table_name, ont_class, attributes,
                base_iri, tables_structure,
                se_mappings, sh_mappings, all_se_sh_set,
                prop_index  = prop_index,
                mapper      = mapper,
                col_meanings  = col_meanings,
                table_meaning = table_meaning,
            )
            with open(SH_MAPPINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(sh_mappings, f, indent=2)

            parent_status = "✓ resolved" if sh_mappings[table_name]["parent_resolved"] else "⚠ placeholder"
            print(f"  ✓ JSON mapping built → :{ont_class}  (parent: {parent_status})")
        else:
            print(f"  ⚠ No ontology class, skipping JSON mapping for this table")

    # Summary
    resolved_count   = sum(1 for m in sh_mappings.values() if m.get("parent_resolved"))
    unresolved_count = sum(1 for m in sh_mappings.values() if not m.get("parent_resolved"))

    print(f"\n{'='*55}")
    print("  PHASE 2 MAPPING COMPLETE")
    print(f"{'='*55}")
    print(f"  Mapped successfully  : {success}")
    print(f"  Parent resolved      : {resolved_count}")
    print(f"  Parent placeholder   : {unresolved_count}")
    print(f"  Errors               : {len(errors)}")
    if errors:
        print(f"  Failed tables        : {errors}")
    if unresolved_count > 0:
        unresolved = [t for t, m in sh_mappings.items() if not m.get("parent_resolved")]
        print(f"  Unresolved parents   : {unresolved}")
    print(f"\n  Cache  → {PROCESS_FILE}")
    print(f"  Output → {SH_MAPPINGS_FILE}\n")


if __name__ == "__main__":
    try:
        run_sh_mapping()
    except Exception as e:
        print(f"\nFatal error: {e}")
        import traceback
        traceback.print_exc()