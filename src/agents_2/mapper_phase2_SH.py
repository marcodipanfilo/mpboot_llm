"""
Ontology Mapper Agent — Phase 2 (SE_SH tables only)
Maps SE_SH tables (subclass/inherited strong entities) to their ontology class.
Writes SH_mappings.json in outputs/mappings/.

Subject IRI logic:
  - If the direct parent table was mapped in Phase 1 (found in SE_mappings.json),
    use the parent's subject template IRI (shared identity).
  - Otherwise use its own IRI template (parent will be resolved in a later phase).

Reads  : src2/memory/patterns_final.json
         src2/memory/understanding.json
         src2/memory/enrichment.json
         src2/outputs/DB_as_json/tables_structure.json
         src2/inputs/ontology/ontology.owl
         src2/outputs/mappings/SE_mappings.json     (Phase 1 output, for parent resolution)
Writes : src2/outputs/mappings_process_sh.json      (LLM cache, resumable)
         src2/outputs/mappings/SH_mappings.json     (final structured mapping)
"""

import json
import requests
import re
import xml.etree.ElementTree as ET
from typing import List, Dict, Optional
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from parsers.ontology_explorer import ontology_explorer
from config.llm_config import LLMConfig
from config.llm_config import SELECTED_PROVIDER

# ===== PATHS =====
MEMORY_FOLDER         = "src2/memory"
DB_JSON_FOLDER        = "src2/outputs/DB_as_json"
PATTERNS_FILE         = os.path.join(MEMORY_FOLDER, "patterns_final.json")
UNDERSTANDING_FILE    = os.path.join(MEMORY_FOLDER, "understanding.json")
ENRICHMENT_FILE       = os.path.join(MEMORY_FOLDER, "enrichment.json")
TABLES_STRUCTURE_FILE = os.path.join(DB_JSON_FOLDER, "tables_structure.json")
ONTOLOGY_FILE         = "src2/inputs/ontology/ontology.owl"
OUTPUT_DIR            = "src2/outputs"
MAPPINGS_DIR          = os.path.join(OUTPUT_DIR, "mappings")
SE_MAPPINGS_FILE      = os.path.join(MAPPINGS_DIR, "SE_mappings.json")
PROCESS_FILE          = os.path.join(OUTPUT_DIR, "mappings_process_sh.json")
SH_MAPPINGS_FILE      = os.path.join(MAPPINGS_DIR, "SH_mappings.json")



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
# SE_SH JSON mapping builder
# ============================================================

def build_sh_json_mapping(
    table_name: str,
    ontology_class: str,
    attributes: List[Dict],
    base_iri: str,
    tables_structure: Dict,
    se_mappings: Dict,
    sh_mappings: Dict,
    all_se_sh_tables: set,
) -> Dict:
    """
    Build the structured JSON mapping for one SE_SH table.

    Subject template:
      - Uses parent's template if parent is resolved (already mapped in SE or SH phase).
      - Uses own IRI if parent is not yet mapped (placeholder, resolved=False).

    Non-pk+fk columns (own attributes and FKs) are mapped as predicateObjectMaps.
    The pk+fk 'id' column is never a predicate — it only contributes to the subject.
    """
    triple_map_iri = f"urn:r2rml:SE_SH_{table_name}"

    # Resolve subject template from parent chain
    subject_template, parent_table, parent_triple_map_iri, parent_resolved = \
        resolve_subject_template(table_name, tables_structure, se_mappings, sh_mappings, base_iri)

    # Split attributes by role
    # pk+fk → subject identity only, never a predicate
    attr_cols = [a for a in attributes if a["role"] == "attribute"]
    fk_cols   = [a for a in attributes if a["role"] == "fk"]

    predicate_object_maps = []

    # Literal properties (own attributes)
    for attr in attr_cols:
        predicate_object_maps.append({
            "predicate": f":{_to_camel_case(attr['name'])}",
            "object": {
                "type":     "literal",
                "column":   attr["name"],
                "datatype": _xsd_type(attr["data_type"])
            }
        })

    # FK join properties (non-identity FKs)
    for fk in fk_cols:
        ref_table  = fk.get("fk_references", {}).get("table", "unknown")
        ref_col    = fk.get("fk_references", {}).get("column", "id")
        # Resolved if ref table is in SE or SH scope
        resolved   = ref_table in se_mappings or ref_table in sh_mappings or ref_table in all_se_sh_tables

        # Determine parent TriplesMap IRI for this FK
        if ref_table in se_mappings:
            fk_parent_iri = se_mappings[ref_table]["triple_map_iri"]
        elif ref_table in sh_mappings:
            fk_parent_iri = sh_mappings[ref_table]["triple_map_iri"]
        else:
            fk_parent_iri = f"urn:r2rml:SE_SH_{ref_table}"

        predicate_object_maps.append({
            "predicate": f":{_to_camel_case(fk['name'])}",
            "object": {
                "type":               "join",
                "parent_triples_map": fk_parent_iri,
                "resolved":           resolved,
                "join_condition": {
                    "child":  fk["name"],
                    "parent": ref_col
                }
            }
        })

    return {
        "pattern":         "SE_SH",
        "triple_map_iri":  triple_map_iri,
        "logical_table":   table_name,
        "parent_table":    parent_table,
        "parent_triple_map_iri": parent_triple_map_iri,
        "parent_resolved": parent_resolved,
        "subject": {
            "template": subject_template,
            "class":    f":{ontology_class}"
        },
        "predicate_object_maps": predicate_object_maps
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
        response = requests.post(self.config['api_url'], headers=headers, json=data)
        if response.status_code != 200:
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
        response = requests.post(self.config['api_url'], headers=headers, json=data)
        if response.status_code != 200:
            raise Exception(f"Claude API request failed: {response.status_code}")
        return response.json()["content"][0]["text"]

    def _get_gemini_response(self, prompt: str) -> str:
        url = f"{self.config['api_url']}/{self.config['model_name']}:generateContent?key={self.config['api_key']}"
        headers = {"Content-Type": "application/json"}
        data = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.3, "maxOutputTokens": 1024}
        }
        response = requests.post(url, headers=headers, json=data)
        if response.status_code != 200:
            raise Exception(f"Gemini API request failed: {response.status_code}")
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

        return f"""You are an ontology mapping expert. Find the SINGLE best matching ontology class for this database table.
This table is a SUBCLASS — it extends a parent table via a shared primary key.
{parent_hint}

TABLE: {table_name}
TABLE MEANING: {table_meaning}
ENTITY TYPE: {entity_type}

COLUMNS (with meanings and roles):
{columns_block}
{enums_block}
AVAILABLE ONTOLOGY CLASSES:
{', '.join(ontology_classes)}

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

        prompt   = self._build_mapping_prompt(
            table_name, table_meaning, entity_type, parent_table,
            column_meanings, column_enrichment, enum_interpretations,
            ontology_classes
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
                    ontology_classes
                )
                mappings_process[table_name] = record
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
            sh_mappings[table_name] = build_sh_json_mapping(
                table_name, ont_class, attributes,
                base_iri, tables_structure,
                se_mappings, sh_mappings, all_se_sh_set
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