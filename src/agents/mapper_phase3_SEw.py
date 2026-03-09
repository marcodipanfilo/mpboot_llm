"""
Ontology Mapper Agent — Phase 3 (SEw tables only)
Maps SEw (Weak Entity) tables to their ontology class.
Writes SEw_mappings.json in outputs/mappings/.

SEw mapping rules:
  - Subject template combines owner FK key + local own key: {owner_fk}/{local_pk}
    This makes each weak entity instance globally unique via its owner context.
  - The FK PK column → rr:parentTriplesMap join to the owner entity
    (resolved=True if owner already mapped in SE/SH phases, else placeholder)
  - The own PK column → appears in template only, never as a predicate
  - Attribute columns → datatype properties (rr:column + rr:datatype)
  - No extra columns in these tables in this schema, but code handles them generically

Reads  : src/memory/patterns_final.json
         src/memory/understanding.json
         src/memory/enrichment.json
         src/outputs/DB_as_json/tables_structure.json
         src/inputs/ontology/ontology.owl
         src/outputs/mappings/SE_mappings.json      (Phase 1, for owner resolution)
         src/outputs/mappings/SH_mappings.json      (Phase 2, for owner resolution)
Writes : src/outputs/mappings_process_sew.json      (LLM cache, resumable)
         src/outputs/mappings/SEw_mappings.json     (final structured mapping)
"""

import json
import requests
import re
import xml.etree.ElementTree as ET
from typing import List, Dict, Optional
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
OUTPUT_DIR            = "src/outputs"
MAPPINGS_DIR          = os.path.join(OUTPUT_DIR, "mappings")
SE_MAPPINGS_FILE      = os.path.join(MAPPINGS_DIR, "SE_mappings.json")
SH_MAPPINGS_FILE      = os.path.join(MAPPINGS_DIR, "SH_mappings.json")
PROCESS_FILE          = os.path.join(OUTPUT_DIR, "mappings_process_sew.json")
SEW_MAPPINGS_FILE     = os.path.join(MAPPINGS_DIR, "SEw_mappings.json")


# ============================================================
# Ontology prefix parser
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
# XSD helpers
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
# Safe JSON loaders
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
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        print(f"  [WARN] Could not parse '{path}' — starting fresh")
        return {}


# ============================================================
# Attribute builder
# ============================================================

def build_attributes(table_name: str, tables_structure: Dict) -> List[Dict]:
    """
    Classify each column into one of four roles:
      pk+fk  — part of composite PK AND is a FK (the owner link)
      pk     — part of composite PK but NOT a FK (the local own key)
      fk     — FK but not part of PK (pure non-key reference)
      attribute — plain data column
    """
    info    = tables_structure.get(table_name, {})
    pk_set  = set(info.get("primary_keys", []))
    columns = info.get("columns", [])
    result  = []

    for col in columns:
        is_pk  = col.get("is_primary_key", False)
        is_fk  = col.get("is_foreign_key", False)
        fk_ref = col.get("foreign_key_reference")

        if is_pk and is_fk:
            role = "pk+fk"    # owner key — used in template AND as join predicate
        elif is_pk:
            role = "pk"       # local own key — used in template only
        elif is_fk:
            role = "fk"       # non-key FK reference
        else:
            role = "attribute"

        attr = {
            "name":      col["name"],
            "data_type": col.get("data_type", "unknown"),
            "role":      role,
            "nullable":  col.get("is_nullable", True),
        }
        if is_fk and fk_ref:
            attr["fk_references"] = {
                "table":  fk_ref["table"],
                "column": fk_ref["column"]
            }
        result.append(attr)

    return result


# ============================================================
# Owner resolution helper
# ============================================================

def resolve_owner(
    ref_table: str,
    se_mappings: Dict,
    sh_mappings: Dict,
) -> tuple:
    """
    Find the TriplesMap IRI for the owner table across all previously mapped phases.
    Returns (owner_triple_map_iri, resolved).
    """
    if ref_table in se_mappings:
        return se_mappings[ref_table]["triple_map_iri"], True
    if ref_table in sh_mappings:
        return sh_mappings[ref_table]["triple_map_iri"], True
    # Not yet mapped — use placeholder with SEw_ prefix guess
    # (could be SR/SRR mapped later, but most owners are SE/SE_SH)
    return f"urn:r2rml:SE_{ref_table}", False


# ============================================================
# SEw JSON mapping builder
# ============================================================

def build_sew_json_mapping(
    table_name: str,
    ontology_class: str,
    attributes: List[Dict],
    base_iri: str,
    se_mappings: Dict,
    sh_mappings: Dict,
) -> Dict:
    """
    Build the structured JSON mapping for one SEw table.

    Subject template: {base_iri}{table}/{owner_fk_col}/{local_pk_col}
    e.g. http://cmt#conference_members/{conference}/{conference_member}

    predicate_object_maps:
      - owner FK col  → rr:parentTriplesMap join to owner entity
      - attribute cols → rr:column literal
      - pure FK cols  → rr:parentTriplesMap join (resolved if already mapped)
      - local own PK  → template only, never a predicate
    """
    triple_map_iri = f"urn:r2rml:SEw_{table_name}"

    # Separate column roles
    pk_fk_cols  = [a for a in attributes if a["role"] == "pk+fk"]   # owner key(s)
    pk_own_cols = [a for a in attributes if a["role"] == "pk"]       # local key(s)
    attr_cols   = [a for a in attributes if a["role"] == "attribute"]
    fk_cols     = [a for a in attributes if a["role"] == "fk"]       # non-key FKs

    # Build subject template: owner_fk_parts / local_pk_parts
    owner_parts = "/".join(f"{{{c['name']}}}" for c in pk_fk_cols)
    local_parts = "/".join(f"{{{c['name']}}}" for c in pk_own_cols)
    pk_template = f"{owner_parts}/{local_parts}" if owner_parts and local_parts \
                  else owner_parts or local_parts or "{id}"

    subject_template = f"{base_iri}{table_name}/{pk_template}"

    predicate_object_maps = []

    # Owner FK columns → object property join to owner entity
    for col in pk_fk_cols:
        ref_table = col["fk_references"]["table"]
        ref_col   = col["fk_references"]["column"]
        owner_iri, resolved = resolve_owner(ref_table, se_mappings, sh_mappings)

        predicate_object_maps.append({
            "predicate": f":{_to_camel_case(col['name'])}",
            "object": {
                "type":               "join",
                "parent_triples_map": owner_iri,
                "resolved":           resolved,
                "join_condition": {
                    "child":  col["name"],
                    "parent": ref_col
                }
            }
        })

    # Literal attribute columns
    for attr in attr_cols:
        predicate_object_maps.append({
            "predicate": f":{_to_camel_case(attr['name'])}",
            "object": {
                "type":     "literal",
                "column":   attr["name"],
                "datatype": _xsd_type(attr["data_type"])
            }
        })

    # Pure FK columns (non-key references)
    for fk in fk_cols:
        ref_table = fk["fk_references"]["table"]
        ref_col   = fk["fk_references"]["column"]
        fk_iri, resolved = resolve_owner(ref_table, se_mappings, sh_mappings)

        predicate_object_maps.append({
            "predicate": f":{_to_camel_case(fk['name'])}",
            "object": {
                "type":               "join",
                "parent_triples_map": fk_iri,
                "resolved":           resolved,
                "join_condition": {
                    "child":  fk["name"],
                    "parent": ref_col
                }
            }
        })

    return {
        "pattern":       "SEw",
        "triple_map_iri": triple_map_iri,
        "logical_table": table_name,
        "owner_columns": [c["name"] for c in pk_fk_cols],
        "local_pk_columns": [c["name"] for c in pk_own_cols],
        "subject": {
            "template": subject_template,
            "class":    f":{ontology_class}"
        },
        "predicate_object_maps": predicate_object_maps
    }


# ============================================================
# OntologyMapper (LLM agent)
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

    def _build_mapping_prompt(
        self,
        table_name: str,
        table_meaning: str,
        entity_type: str,
        owner_table: Optional[str],
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

        owner_hint = (
            f"\nOWNER TABLE: {owner_table} (this is a weak entity — its identity depends on this owner)"
            if owner_table else ""
        )

        return f"""You are an ontology mapping expert. Find the SINGLE best matching ontology class for this database table.
This table is a WEAK ENTITY — its identifier depends on an owner entity (composite key).
{owner_hint}

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
        owner_table: Optional[str],
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

        print(f"  entity_type={entity_type}  owner={owner_table}  columns={len(column_meanings)}")

        attributes = build_attributes(table_name, tables_structure)

        prompt   = self._build_mapping_prompt(
            table_name, table_meaning, entity_type, owner_table,
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
            "pattern":       "SEw",
            "owner_table":   owner_table,
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
# Main entry point
# ============================================================

def run_sew_mapping():
    """Map all SEw tables and write SEw_mappings.json."""

    print("=" * 55)
    print("  ONTOLOGY MAPPER — Phase 3 (SEw tables)")
    print("=" * 55)

    # Load required inputs
    table_patterns   = load_json_safe(PATTERNS_FILE)
    tables_structure = load_json_safe(TABLES_STRUCTURE_FILE)
    understanding    = load_json_safe(UNDERSTANDING_FILE)
    enrichment       = load_json_safe(ENRICHMENT_FILE)

    print(f"  Patterns   : {len(table_patterns)} tables")
    print(f"  Understood : {len(understanding)} tables")
    print(f"  Enriched   : {len(enrichment)} tables")

    # Load previous phase mappings for owner resolution
    os.makedirs(MAPPINGS_DIR, exist_ok=True)
    se_mappings = load_json_optional(SE_MAPPINGS_FILE)
    sh_mappings = load_json_optional(SH_MAPPINGS_FILE)
    print(f"\n  SE mappings (Phase 1) : {len(se_mappings)} tables")
    print(f"  SH mappings (Phase 2) : {len(sh_mappings)} tables")

    if not se_mappings and not sh_mappings:
        print(f"  [WARN] No previous phase mappings found — all owner references will be placeholders")

    # Parse ontology base IRI
    print(f"\nParsing ontology prefixes from '{ONTOLOGY_FILE}' ...")
    prefixes = parse_ontology_prefixes(ONTOLOGY_FILE)
    base_iri = get_ontology_base_iri(prefixes)
    print(f"  ✓ Base IRI: {base_iri}")

    # Filter SEw tables
    sew_tables = {t: p for t, p in table_patterns.items() if p == "SEw"}
    print(f"\n  SEw tables : {len(sew_tables)}")

    # Load ontology classes
    print("\nLoading ontology classes...")
    ontology_classes = ontology_explorer(mode="classes")["classes"]
    print(f"  ✓ {len(ontology_classes)} classes loaded")

    # Load existing caches for resumable runs
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    mappings_process: Dict = load_json_optional(PROCESS_FILE)
    sew_mappings:     Dict = load_json_optional(SEW_MAPPINGS_FILE)

    if mappings_process:
        print(f"\n  Loaded existing mappings_process_sew.json ({len(mappings_process)} entries)")
    if sew_mappings:
        print(f"  Loaded existing SEw_mappings.json ({len(sew_mappings)} entries)")

    mapper  = OntologyMapper(provider=SELECTED_PROVIDER)
    success = 0
    errors  = []
    total   = len(sew_tables)

    for idx, (table_name, pattern) in enumerate(sew_tables.items(), 1):
        print(f"\n[{idx:>2}/{total}] {table_name}  [{pattern}]")

        # Find owner table (the pk+fk column's referenced table)
        attributes   = build_attributes(table_name, tables_structure)
        pk_fk_cols   = [a for a in attributes if a["role"] == "pk+fk"]
        owner_table  = pk_fk_cols[0]["fk_references"]["table"] if pk_fk_cols else None

        already_mapped = table_name in mappings_process

        if not already_mapped:
            try:
                record = mapper.map_table(
                    table_name, owner_table,
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

        # Build SEw JSON mapping entry
        record    = mappings_process[table_name]
        ont_class = record.get("ontology_mapping", {}).get("ontology_class")
        attributes = record.get("attributes", [])

        if ont_class:
            sew_mappings[table_name] = build_sew_json_mapping(
                table_name, ont_class, attributes,
                base_iri, se_mappings, sh_mappings
            )
            with open(SEW_MAPPINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(sew_mappings, f, indent=2)

            # Report owner resolution status
            owner_maps = [
                p for p in sew_mappings[table_name]["predicate_object_maps"]
                if p["object"]["type"] == "join"
                and p["object"].get("parent_triples_map", "").endswith(owner_table or "")
            ]
            owner_resolved = owner_maps[0]["object"]["resolved"] if owner_maps else False
            status = "✓ resolved" if owner_resolved else "⚠ placeholder"
            print(f"  ✓ JSON mapping built → :{ont_class}  (owner: {status})")
        else:
            print(f"  ⚠ No ontology class, skipping JSON mapping for this table")

    # Summary
    resolved_count   = sum(
        1 for m in sew_mappings.values()
        if any(p["object"].get("resolved") for p in m.get("predicate_object_maps", [])
               if p["object"]["type"] == "join")
    )
    unresolved_count = len(sew_mappings) - resolved_count

    print(f"\n{'='*55}")
    print("  PHASE 3 MAPPING COMPLETE")
    print(f"{'='*55}")
    print(f"  Mapped successfully  : {success}")
    print(f"  Owner resolved       : {resolved_count}")
    print(f"  Owner placeholder    : {unresolved_count}")
    print(f"  Errors               : {len(errors)}")
    if errors:
        print(f"  Failed tables        : {errors}")
    print(f"\n  Cache  → {PROCESS_FILE}")
    print(f"  Output → {SEW_MAPPINGS_FILE}\n")


if __name__ == "__main__":
    try:
        run_sew_mapping()
    except Exception as e:
        print(f"\nFatal error: {e}")
        import traceback
        traceback.print_exc()