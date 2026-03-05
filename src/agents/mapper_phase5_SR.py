"""
Ontology Mapper Agent — Phase 5 (SR tables only)
Maps SR (Simple Relationship / bridge) tables to an ontology object property.

SR mapping rules:
  - No new class is created — SR is a pure relationship between two entities.
  - The table is mapped as a predicate linking participant A to participant B.
  - Subject: instances of the first participant entity (reuses its TriplesMap).
  - Object property: chosen from the ontology by the LLM.
  - Object: instances of the second participant entity (join via parentTriplesMap).
  - If more than 2 participants exist, each pair is mapped as a separate predicateObjectMap.
  - No subject template is generated for the SR table itself — it has no identity of its own.

Output JSON structure per table:
  {
    "pattern": "SR",
    "triple_map_iri": "urn:r2rml:SR_{table}",
    "logical_table": "{table}",
    "participants": [
      { "column": "aid", "ref_table": "administrators", "ref_col": "id" },
      { "column": "cid", "ref_table": "conferences",    "ref_col": "id" }
    ],
    "mappings": [
      {
        "subject_triples_map":  "urn:r2rml:SE_SH_administrators",
        "subject_resolved":     true,
        "predicate":            ":hasConference",
        "object_triples_map":   "urn:r2rml:SE_conferences",
        "object_resolved":      true,
        "join_condition": { "child": "aid", "parent": "id" }  ← subject side join
        "object_join": { "child": "cid",  "parent": "id" }    ← object side join
      }
    ]
  }

Reads  : src2/memory/patterns_final.json
         src2/memory/understanding.json
         src2/memory/enrichment.json
         src2/outputs/DB_as_json/tables_structure.json
         src2/inputs/ontology/ontology.owl
         src2/outputs/mappings/SE_mappings.json
         src2/outputs/mappings/SH_mappings.json
         src2/outputs/mappings/SEw_mappings.json
         src2/outputs/mappings/SRR_mappings.json
Writes : src2/outputs/mappings_process_sr.json
         src2/outputs/mappings/SR_mappings.json
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
SH_MAPPINGS_FILE      = os.path.join(MAPPINGS_DIR, "SH_mappings.json")
SEW_MAPPINGS_FILE     = os.path.join(MAPPINGS_DIR, "SEw_mappings.json")
SRR_MAPPINGS_FILE     = os.path.join(MAPPINGS_DIR, "SRR_mappings.json")
PROCESS_FILE          = os.path.join(OUTPUT_DIR, "mappings_process_sr.json")
SR_MAPPINGS_FILE      = os.path.join(MAPPINGS_DIR, "SR_mappings.json")


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
    """For SR tables every column is pk+fk — no attributes, no own pk."""
    info   = tables_structure.get(table_name, {})
    pk_set = set(info.get("primary_keys", []))
    result = []
    for col in info.get("columns", []):
        is_pk  = col.get("is_primary_key", False)
        is_fk  = col.get("is_foreign_key", False)
        fk_ref = col.get("foreign_key_reference")
        role   = "pk+fk" if is_pk and is_fk else \
                 "fk"    if is_fk else "attribute"
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
# Participant resolution
# ============================================================

def resolve_participant(
    ref_table: str,
    all_phase_mappings: List[Dict],
) -> tuple:
    """
    Find the TriplesMap IRI for a participant across all previous phases.
    Returns (triple_map_iri, resolved).
    """
    for phase_map in all_phase_mappings:
        if ref_table in phase_map:
            return phase_map[ref_table]["triple_map_iri"], True
    return f"urn:r2rml:SE_{ref_table}", False


# ============================================================
# SR JSON mapping builder
# ============================================================

def build_sr_json_mapping(
    table_name: str,
    object_property: str,
    attributes: List[Dict],
    all_phase_mappings: List[Dict],
) -> Dict:
    """
    Build the structured JSON mapping for one SR table.

    For a 2-participant SR table (the common case):
      - Subject side  : first participant's TriplesMap
      - Predicate     : ontology object property chosen by LLM
      - Object side   : second participant's TriplesMap via join

    For N-participant SR tables (3+ entities):
      - One mapping entry per participant pair is generated,
        using each participant as subject in turn.

    The SR table itself has no subject template — it is dissolved
    into predicate links between the participant entities.
    """
    triple_map_iri = f"urn:r2rml:SR_{table_name}"
    pk_fk_cols     = [a for a in attributes if a["role"] == "pk+fk"]

    # Build participants list with resolution
    participants = []
    for col in pk_fk_cols:
        ref_table = col["fk_references"]["table"]
        ref_col   = col["fk_references"]["column"]
        iri, resolved = resolve_participant(ref_table, all_phase_mappings)
        participants.append({
            "column":           col["name"],
            "ref_table":        ref_table,
            "ref_col":          ref_col,
            "triple_map_iri":   iri,
            "resolved":         resolved
        })

    # Generate one mapping per ordered pair (A→B)
    # For 2 participants: one mapping A→B
    # For N participants: N*(N-1) directed pairs — but typically just 2
    mappings = []
    for i, subj in enumerate(participants):
        for j, obj in enumerate(participants):
            if i == j:
                continue
            mappings.append({
                "subject_triples_map": subj["triple_map_iri"],
                "subject_resolved":    subj["resolved"],
                "subject_join": {
                    "child":  subj["column"],
                    "parent": subj["ref_col"]
                },
                "predicate":           f":{object_property}",
                "object_triples_map":  obj["triple_map_iri"],
                "object_resolved":     obj["resolved"],
                "object_join": {
                    "child":  obj["column"],
                    "parent": obj["ref_col"]
                }
            })

    return {
        "pattern":       "SR",
        "triple_map_iri": triple_map_iri,
        "logical_table": table_name,
        "participants":  [
            {"column": p["column"], "ref_table": p["ref_table"], "ref_col": p["ref_col"]}
            for p in participants
        ],
        "mappings": mappings
    }



# ============================================================
# Participant class resolution & property fetching
# ============================================================

def get_participant_classes(
    participants: List[str],
    all_phase_mappings: List[Dict],
) -> Dict[str, str]:
    """
    For each participant table name, find the ontology class it was mapped to
    in any of the previous phase mapping files.
    Returns { table_name: ontology_class_name (without colon prefix) }
    """
    result = {}
    for table in participants:
        for phase_map in all_phase_mappings:
            if table in phase_map:
                raw = phase_map[table].get("subject", {}).get("class", "")
                # Strip leading colon: ":Person" → "Person"
                cls = raw.lstrip(":")
                if cls:
                    result[table] = cls
                break
    return result


def fetch_properties_for_participants(
    participant_classes: Dict[str, str],
) -> List[str]:
    """
    Fetch ontology object properties that link the participant classes.
    Uses class_properties mode for each class and collects the union,
    then filters to keep only properties that appear in at least one class.
    If only one class is resolved, fetches its properties alone.
    If no classes resolved, returns empty list.
    """
    classes = list(participant_classes.values())
    if not classes:
        return []

    properties_set: set = set()
    for cls in classes:
        try:
            result = ontology_explorer(mode="class_properties", class_name=cls)
            for prop in result.get("properties", []):
                properties_set.add(prop["property"])
        except Exception:
            pass

    return sorted(properties_set)


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
            return '"object_property"' in cleaned

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
        participants: List[str],
        column_meanings: Dict[str, str],
        column_enrichment: Dict[str, Dict],
        ontology_properties: List[str],
        participant_classes: Dict[str, str] = None,
    ) -> str:
        col_lines = []
        for col_name, meaning in column_meanings.items():
            role_hint = column_enrichment.get(col_name, {}).get("role", "")
            line = f"  - {col_name}: {meaning}"
            if role_hint:
                line += f"  [role: {role_hint}]"
            col_lines.append(line)
        columns_block = "\n".join(col_lines) if col_lines else "  (none)"

        participants_str = " ↔ ".join(participants) if participants else "unknown"
        classes_str = "  \n".join(
            f"  {tbl} → :{cls}" for tbl, cls in (participant_classes or {}).items()
        )

        prop_list = ', '.join(ontology_properties) if ontology_properties                     else "(no direct properties found between these classes — pick the closest available)"

        return f"""You are an ontology mapping expert. Find the SINGLE best matching object property for this bridge table.
This is a SIMPLE RELATIONSHIP table — it connects entities with no attributes of its own.
No new class should be created. You must pick an existing object property from the ontology.

TABLE: {table_name}
TABLE MEANING: {table_meaning}
CONNECTED ENTITIES: {participants_str}
MAPPED ONTOLOGY CLASSES:
{classes_str}

COLUMNS (FK links to participant entities):
{columns_block}

AVAILABLE ONTOLOGY OBJECT PROPERTIES (between these classes):
{prop_list}

Return ONLY a JSON object, no markdown, no extra text:

{{
  "object_property": "bestMatchPropertyName",
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
                if "object_property" in obj:
                    return obj
        except (json.JSONDecodeError, ValueError):
            pass

        prop_match  = re.search(r'"object_property"\s*:\s*"([^"]+)"', response)
        score_match = re.search(r'"score"\s*:\s*(\d)', response)
        why_match   = re.search(r'"why"\s*:\s*"([^"]*)"', response)
        if prop_match:
            print(f"  [INFO] Mapping recovered via regex fallback")
            return {
                "object_property": prop_match.group(1),
                "score": int(score_match.group(1)) if score_match else None,
                "why":   why_match.group(1) if why_match else "extracted via regex fallback"
            }

        print(f"  [WARN] Could not parse mapping response")
        print(f"  [WARN] Raw: {response[:400]}")
        return None

    def map_table(
        self,
        table_name: str,
        participants: List[str],
        participant_classes: Dict[str, str],
        tables_structure: Dict,
        understanding: Dict,
        enrichment: Dict,
    ) -> Dict:
        table_und         = understanding.get(table_name, {})
        table_meaning     = table_und.get("table_meaning", "Not available")
        column_meanings   = table_und.get("columns", {})
        table_enr         = enrichment.get(table_name, {})
        column_enrichment = table_enr.get("column_enrichment", {})

        print(f"  participants={participants}  classes={list(participant_classes.values())}")

        attributes = build_attributes(table_name, tables_structure)

        # Fetch object properties linking the participant classes
        ontology_properties = fetch_properties_for_participants(participant_classes)

        prompt   = self._build_mapping_prompt(
            table_name, table_meaning, participants,
            column_meanings, column_enrichment,
            ontology_properties, participant_classes
        )
        response = self.get_llm_response(prompt)
        mapping  = self._parse_mapping_response(response)

        if mapping:
            print(f"  ✓ → {mapping['object_property']}  (score {mapping.get('score')}/5)")
        else:
            print(f"  ✗ mapping failed")

        return {
            "table":            table_name,
            "pattern":          "SR",
            "participants":     participants,
            "table_meaning":    table_meaning,
            "attributes":       attributes,
            "ontology_mapping": {
                "object_property": mapping["object_property"] if mapping else None,
                "score":           mapping.get("score")       if mapping else None,
                "why":             mapping.get("why")         if mapping else "mapping failed"
            }
        }


# ============================================================
# Main entry point
# ============================================================

def run_sr_mapping():
    """Map all SR tables and write SR_mappings.json."""

    print("=" * 55)
    print("  ONTOLOGY MAPPER — Phase 5 (SR tables)")
    print("=" * 55)

    # Load required inputs
    table_patterns   = load_json_safe(PATTERNS_FILE)
    tables_structure = load_json_safe(TABLES_STRUCTURE_FILE)
    understanding    = load_json_safe(UNDERSTANDING_FILE)
    enrichment       = load_json_safe(ENRICHMENT_FILE)

    print(f"  Patterns   : {len(table_patterns)} tables")
    print(f"  Understood : {len(understanding)} tables")
    print(f"  Enriched   : {len(enrichment)} tables")

    # Load all previous phase mappings for participant resolution
    os.makedirs(MAPPINGS_DIR, exist_ok=True)
    se_mappings  = load_json_optional(SE_MAPPINGS_FILE)
    sh_mappings  = load_json_optional(SH_MAPPINGS_FILE)
    sew_mappings = load_json_optional(SEW_MAPPINGS_FILE)
    srr_mappings = load_json_optional(SRR_MAPPINGS_FILE)
    all_phase_mappings = [se_mappings, sh_mappings, sew_mappings, srr_mappings]

    print(f"\n  SE  mappings (Phase 1) : {len(se_mappings)} tables")
    print(f"  SH  mappings (Phase 2) : {len(sh_mappings)} tables")
    print(f"  SEw mappings (Phase 3) : {len(sew_mappings)} tables")
    print(f"  SRR mappings (Phase 4) : {len(srr_mappings)} tables")

    # Parse ontology base IRI
    print(f"\nParsing ontology prefixes from '{ONTOLOGY_FILE}' ...")
    prefixes = parse_ontology_prefixes(ONTOLOGY_FILE)
    base_iri = get_ontology_base_iri(prefixes)
    print(f"  ✓ Base IRI: {base_iri}")

    # Filter SR tables
    sr_tables = {t: p for t, p in table_patterns.items() if p == "SR"}
    print(f"\n  SR tables : {len(sr_tables)}")

    if len(sr_tables) == 0:
        print("  No SR tables found in patterns — nothing to map.")
        return

    # Object properties are fetched per-table using the participant mapped classes
    # (see fetch_properties_for_participants helper below)

    # Load existing caches for resumable runs
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    mappings_process: Dict = load_json_optional(PROCESS_FILE)
    sr_mappings:      Dict = load_json_optional(SR_MAPPINGS_FILE)

    if mappings_process:
        print(f"\n  Loaded existing mappings_process_sr.json ({len(mappings_process)} entries)")
    if sr_mappings:
        print(f"  Loaded existing SR_mappings.json ({len(sr_mappings)} entries)")

    mapper  = OntologyMapper(provider=SELECTED_PROVIDER)
    success = 0
    errors  = []
    total   = len(sr_tables)

    for idx, (table_name, pattern) in enumerate(sr_tables.items(), 1):
        print(f"\n[{idx:>2}/{total}] {table_name}  [{pattern}]")

        # Extract participant entity names from pk+fk columns
        attributes   = build_attributes(table_name, tables_structure)
        pk_fk_cols   = [a for a in attributes if a["role"] == "pk+fk"]
        participants = [a["fk_references"]["table"] for a in pk_fk_cols]

        already_mapped = table_name in mappings_process

        # Resolve participant ontology classes from previous phase mappings
        participant_classes = get_participant_classes(participants, all_phase_mappings)

        if not already_mapped:
            try:
                record = mapper.map_table(
                    table_name, participants, participant_classes,
                    tables_structure, understanding, enrichment,
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

        # Build SR JSON mapping entry
        record      = mappings_process[table_name]
        obj_prop    = record.get("ontology_mapping", {}).get("object_property")
        attributes  = record.get("attributes", [])

        if obj_prop:
            sr_mappings[table_name] = build_sr_json_mapping(
                table_name, obj_prop, attributes, all_phase_mappings
            )
            with open(SR_MAPPINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(sr_mappings, f, indent=2)

            all_resolved = all(
                m["subject_resolved"] and m["object_resolved"]
                for m in sr_mappings[table_name]["mappings"]
            )
            status = "✓ all resolved" if all_resolved else "⚠ some placeholders"
            print(f"  ✓ JSON mapping built → :{obj_prop}  ({status})")
        else:
            print(f"  ⚠ No object property, skipping JSON mapping for this table")

    # Summary
    fully_resolved   = sum(
        1 for m in sr_mappings.values()
        if all(e["subject_resolved"] and e["object_resolved"] for e in m["mappings"])
    )
    has_placeholders = len(sr_mappings) - fully_resolved

    print(f"\n{'='*55}")
    print("  PHASE 5 MAPPING COMPLETE")
    print(f"{'='*55}")
    print(f"  Mapped successfully       : {success}")
    print(f"  All participants resolved : {fully_resolved}")
    print(f"  Has placeholders          : {has_placeholders}")
    print(f"  Errors                    : {len(errors)}")
    if errors:
        print(f"  Failed tables             : {errors}")
    print(f"\n  Cache  → {PROCESS_FILE}")
    print(f"  Output → {SR_MAPPINGS_FILE}\n")


if __name__ == "__main__":
    try:
        run_sr_mapping()
    except Exception as e:
        print(f"\nFatal error: {e}")
        import traceback
        traceback.print_exc()