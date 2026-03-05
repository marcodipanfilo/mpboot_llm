"""
Ontology Mapper Agent — Phase 6 (Hidden Patterns)
Discovers and maps two hidden patterns inside SE and SE_SH tables:

PATTERN A — Hidden Subclass (HIDDEN_SH):
  A non-PK FK column in a table points to another table whose mapped class
  exists in the ontology as a subclass concept. The column presence (non-null)
  implies the row also belongs to that subclass.
  Example: papers.author → authors.id  means this paper's author is also an Author.
  Mapping: same subject IRI as base table, additional rr:class, WHERE col IS NOT NULL.

PATTERN B — Type Dispatch (TYPE_DISPATCH):
  A non-FK column holds a discriminator value (integer, boolean, string) that
  determines which ontology subclass the row belongs to.
  Example: papers.type ∈ {0, 1, 2} → None / ConferenceDocument / AbstractDocument
  Mapping: one TriplesMap per non-null value, each with its own rr:class filter.
  The LLM interprets what each value means using column name, data type, understanding
  context, and available ontology classes.

Reads  : src2/memory/patterns_final.json
         src2/memory/understanding.json
         src2/memory/enrichment.json
         src2/outputs/DB_as_json/tables_structure.json
         src2/outputs/DB_as_json/column_samples.json   (optional — value distributions)
         src2/inputs/ontology/ontology.owl
         src2/outputs/mappings/SE_mappings.json
         src2/outputs/mappings/SH_mappings.json
Writes : src2/outputs/mappings_process_hidden.json
         src2/outputs/mappings/HIDDEN_mappings.json
"""

import json
import requests
import re
import xml.etree.ElementTree as ET
from typing import List, Dict, Optional, Any
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
COLUMN_SAMPLES_FILE   = os.path.join(DB_JSON_FOLDER, "column_samples.json")  # optional
ONTOLOGY_FILE         = "src2/inputs/ontology/ontology.owl"
OUTPUT_DIR            = "src2/outputs"
MAPPINGS_DIR          = os.path.join(OUTPUT_DIR, "mappings")
SE_MAPPINGS_FILE      = os.path.join(MAPPINGS_DIR, "SE_mappings.json")
SH_MAPPINGS_FILE      = os.path.join(MAPPINGS_DIR, "SH_mappings.json")
PROCESS_FILE          = os.path.join(OUTPUT_DIR, "mappings_process_hidden.json")
HIDDEN_MAPPINGS_FILE  = os.path.join(MAPPINGS_DIR, "HIDDEN_mappings.json")


# Column name keywords that strongly suggest a type discriminator
TYPE_KEYWORDS = {"type", "kind", "category", "role", "status", "mode",
                 "flag", "class", "subtype", "variant", "form"}

# Data types eligible as discriminator columns
DISCRIMINATOR_TYPES = {"integer", "int", "smallint", "bigint",
                       "boolean", "bool", "varchar", "text", "char", "character"}


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
# Candidate discovery
# ============================================================

def get_base_subject(table_name: str, se_mappings: Dict, sh_mappings: Dict) -> Optional[str]:
    """Return the subject template IRI for a table from any phase mapping."""
    for phase in (se_mappings, sh_mappings):
        if table_name in phase:
            return phase[table_name]["subject"]["template"]
    return None


def get_base_triple_map(table_name: str, se_mappings: Dict, sh_mappings: Dict) -> Optional[str]:
    """Return the TriplesMap IRI for a table from any phase mapping."""
    for phase in (se_mappings, sh_mappings):
        if table_name in phase:
            return phase[table_name]["triple_map_iri"]
    return None


def get_base_class(table_name: str, se_mappings: Dict, sh_mappings: Dict) -> Optional[str]:
    """Return the ontology class (without colon) for a mapped table."""
    for phase in (se_mappings, sh_mappings):
        if table_name in phase:
            raw = phase[table_name]["subject"]["class"]
            return raw.lstrip(":")
    return None


def discover_hidden_subclass_candidates(
    table_name: str,
    tables_structure: Dict,
    se_mappings: Dict,
    sh_mappings: Dict,
) -> List[Dict]:
    """
    Find non-PK FK columns whose referenced table has a mapped class.
    Each such column is a hidden subclass candidate.
    Returns list of candidate dicts.
    """
    info    = tables_structure.get(table_name, {})
    pk_set  = set(info.get("primary_keys", []))
    results = []

    for col in info.get("columns", []):
        if not col["is_foreign_key"]:
            continue
        if col["name"] in pk_set:
            continue  # skip identity FKs — those are SE_SH, already handled

        ref = col.get("foreign_key_reference", {})
        ref_table = ref.get("table")
        ref_col   = ref.get("column", "id")
        if not ref_table:
            continue

        # Only include if referenced table has a mapped class
        ref_class = get_base_class(ref_table, se_mappings, sh_mappings)
        results.append({
            "column":      col["name"],
            "ref_table":   ref_table,
            "ref_col":     ref_col,
            "ref_class":   ref_class,          # None if not yet mapped
            "is_nullable": col["is_nullable"],
            "data_type":   col["data_type"],
        })

    return results


def discover_type_dispatch_candidates(
    table_name: str,
    tables_structure: Dict,
    enrichment: Dict,
    column_samples: Dict,
) -> List[Dict]:
    """
    Find non-FK, non-PK columns that could be type discriminators.
    Scoring criteria:
      +2  column name contains a TYPE_KEYWORD
      +1  data type is boolean (almost certainly a flag/dispatch)
      +1  column has enum_interpretations in enrichment
      +1  column name ends in _id but is not a FK (unlikely but possible)
    Only columns with score >= 1 are returned.
    Returns list of candidate dicts with value samples if available.
    """
    info   = tables_structure.get(table_name, {})
    pk_set = set(info.get("primary_keys", []))
    enr    = enrichment.get(table_name, {})
    enums  = enr.get("enum_interpretations", {})
    table_samples = column_samples.get(table_name, {})
    results = []

    for col in info.get("columns", []):
        if col["is_foreign_key"] or col["name"] in pk_set:
            continue

        dt         = col["data_type"].lower().split("(")[0].strip()
        name_lower = col["name"].lower()

        if dt not in DISCRIMINATOR_TYPES:
            continue

        score = 0
        if any(kw in name_lower for kw in TYPE_KEYWORDS):
            score += 2
        if dt in ("boolean", "bool"):
            score += 1
        if col["name"] in enums:
            score += 1

        if score < 1:
            continue

        # Gather value samples (from column_samples.json if available)
        samples = table_samples.get(col["name"], {})
        enum_vals = enums.get(col["name"], {})

        results.append({
            "column":       col["name"],
            "data_type":    col["data_type"],
            "score":        score,
            "enum_values":  enum_vals,    # from enrichment agent
            "samples":      samples,      # from column_samples.json (may be empty)
            "is_nullable":  col["is_nullable"],
        })

    return sorted(results, key=lambda x: -x["score"])


# ============================================================
# JSON mapping builders
# ============================================================

def build_hidden_sh_entry(
    table_name: str,
    candidate: Dict,
    llm_decision: Dict,
    base_iri: str,
    se_mappings: Dict,
    sh_mappings: Dict,
) -> Optional[Dict]:
    """Build a HIDDEN_SH mapping entry for one FK column."""
    base_subject    = get_base_subject(table_name, se_mappings, sh_mappings)
    base_triple_map = get_base_triple_map(table_name, se_mappings, sh_mappings)
    ref_table       = candidate["ref_table"]
    ref_triple_map  = get_base_triple_map(ref_table, se_mappings, sh_mappings)
    ref_resolved    = ref_triple_map is not None

    if not ref_triple_map:
        ref_triple_map = f"urn:r2rml:SE_{ref_table}"

    assigned_class = llm_decision.get("assigned_class")
    if not assigned_class:
        return None

    triple_map_iri = f"urn:r2rml:HIDDEN_SH_{table_name}_{candidate['column']}"

    return {
        "hidden_pattern":   "HIDDEN_SH",
        "triple_map_iri":   triple_map_iri,
        "source_table":     table_name,
        "trigger_column":   candidate["column"],
        "sql_filter":       f"{candidate['column']} IS NOT NULL",
        "base_triple_map":  base_triple_map,
        "subject": {
            "template":         base_subject or f"{base_iri}{table_name}/{{id}}",
            "class":            f":{assigned_class}",
            "reuses_iri_from":  base_triple_map
        },
        "predicate_object_maps": [
            {
                "predicate": f":{_to_camel_case(candidate['column'])}",
                "object": {
                    "type":               "join",
                    "parent_triples_map": ref_triple_map,
                    "resolved":           ref_resolved,
                    "join_condition": {
                        "child":  candidate["column"],
                        "parent": candidate["ref_col"]
                    }
                }
            }
        ],
        "llm_decision": llm_decision
    }


def build_type_dispatch_entry(
    table_name: str,
    candidate: Dict,
    llm_decision: Dict,
    base_iri: str,
    se_mappings: Dict,
    sh_mappings: Dict,
) -> Optional[Dict]:
    """Build a TYPE_DISPATCH mapping entry for one discriminator column."""
    base_subject    = get_base_subject(table_name, se_mappings, sh_mappings)
    base_triple_map = get_base_triple_map(table_name, se_mappings, sh_mappings)
    value_map       = llm_decision.get("value_class_map", {})

    if not value_map:
        return None

    dispatch = []
    for val, cls in value_map.items():
        if cls is None:
            continue  # skip "no subclass" values (e.g. type=0 → None)
        sql_val = f"'{val}'" if candidate["data_type"].lower() not in (
            "integer", "int", "smallint", "bigint", "boolean", "bool"
        ) else val
        dispatch.append({
            "triple_map_iri": f"urn:r2rml:HIDDEN_TD_{table_name}_{candidate['column']}_{val}",
            "filter_value":   val,
            "sql_filter":     f"{candidate['column']} = {sql_val}",
            "subject": {
                "template":        base_subject or f"{base_iri}{table_name}/{{id}}",
                "class":           f":{cls}",
                "reuses_iri_from": base_triple_map
            },
            "predicate_object_maps": []
        })

    if not dispatch:
        return None

    return {
        "hidden_pattern":        "TYPE_DISPATCH",
        "source_table":          table_name,
        "discriminator_column":  candidate["column"],
        "discriminator_type":    candidate["data_type"],
        "base_triple_map":       base_triple_map,
        "dispatch":              dispatch,
        "llm_decision":          llm_decision
    }


def _to_camel_case(name: str) -> str:
    parts = name.split("_")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


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
            return False

    def get_llm_response(self, prompt: str) -> str:
        if self.provider == "claude":
            return self._get_claude_response(prompt)
        elif self.provider == "gemini":
            return self._get_gemini_response(prompt)
        else:
            return self._get_openai_compatible_response(prompt)

    def _get_openai_compatible_response(self, prompt: str) -> str:
        headers = {
            "Content-Type":  "application/json",
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
            print(f"\n  [RETRY] requesting JSON output...", end="", flush=True)
            retry_data = {
                "model": self.config['model_name'],
                "messages": [
                    {"role": "user",      "content": prompt},
                    {"role": "assistant", "content": raw},
                    {"role": "user",      "content": (
                        "You only produced reasoning but no JSON output. "
                        "Now output ONLY the JSON object. No thinking, no explanation."
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
            "Content-Type":      "application/json",
            "x-api-key":         self.config['api_key'],
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

    def _parse_json_response(self, response: str) -> Optional[Dict]:
        try:
            cleaned = re.sub(r'```json\s*', '', response)
            cleaned = re.sub(r'```\s*', '', cleaned).strip()
            j_start = cleaned.find("{")
            j_end   = cleaned.rfind("}") + 1
            if j_start != -1 and j_end > 0:
                return json.loads(cleaned[j_start:j_end])
        except (json.JSONDecodeError, ValueError):
            pass
        print(f"  [WARN] Could not parse JSON response")
        print(f"  [WARN] Raw: {response[:300]}")
        return None

    # ------------------------------------------------------------------
    # Prompt A: Hidden Subclass
    # ------------------------------------------------------------------

    def prompt_hidden_subclass(
        self,
        table_name: str,
        base_class: str,
        column_name: str,
        ref_table: str,
        ref_class: Optional[str],
        col_meaning: str,
        table_meaning: str,
        ontology_classes: List[str],
    ) -> str:
        ref_class_hint = f"The referenced table '{ref_table}' is mapped to ontology class :{ref_class}." \
                         if ref_class else f"The referenced table '{ref_table}' has no mapped class yet."

        return f"""You are an ontology mapping expert analyzing a hidden subclass pattern.

SITUATION:
  Table '{table_name}' (mapped to :{base_class}) has a non-primary FK column '{column_name}'
  that references table '{ref_table}'.
  {ref_class_hint}
  Column meaning: {col_meaning}
  Table meaning: {table_meaning}

QUESTION:
  When column '{column_name}' is NOT NULL, does the row from '{table_name}' also belong
  to an additional ontology subclass? If yes, which class best represents this hidden role?
  Only choose a class if it makes semantic sense as an additional type for '{table_name}' rows.

AVAILABLE ONTOLOGY CLASSES: {', '.join(ontology_classes)}

Return ONLY a JSON object:
{{
  "is_hidden_subclass": true or false,
  "assigned_class": "ClassName or null",
  "confidence": <1-5>,
  "reasoning": "One sentence explaining your decision."
}}"""

    # ------------------------------------------------------------------
    # Prompt B: Type Dispatch
    # ------------------------------------------------------------------

    def prompt_type_dispatch(
        self,
        table_name: str,
        base_class: str,
        column_name: str,
        data_type: str,
        col_meaning: str,
        table_meaning: str,
        enum_values: Dict,
        samples: Dict,
        ontology_classes: List[str],
    ) -> str:
        # Build value context — combine enum interpretations + sample distributions
        value_context_lines = []
        all_values = set(str(k) for k in enum_values.keys()) | \
                     set(str(k) for k in samples.keys())

        for v in sorted(all_values):
            parts = []
            if str(v) in enum_values:
                parts.append(f"label='{enum_values[str(v)]}'")
            if str(v) in samples:
                s = samples[str(v)]
                if isinstance(s, dict):
                    count = s.get("count", s.get("frequency", ""))
                    pct   = s.get("percentage", s.get("pct", ""))
                    if count:
                        parts.append(f"count={count}")
                    if pct:
                        parts.append(f"({pct}%)")
                else:
                    parts.append(f"count={s}")
            value_context_lines.append(f"  value={v}  {', '.join(parts)}")

        value_block = "\n".join(value_context_lines) if value_context_lines \
                      else "  (no value distribution available — reason from column name and context)"

        return f"""You are an ontology mapping expert analyzing a type-dispatch pattern.

SITUATION:
  Table '{table_name}' (mapped to :{base_class}) has a column '{column_name}' (type: {data_type})
  that appears to be a discriminator — different values may indicate different ontology subclasses.
  Column meaning: {col_meaning}
  Table meaning: {table_meaning}

KNOWN VALUES AND DISTRIBUTION:
{value_block}

TASK:
  For each distinct value of '{column_name}', decide which ontology class (if any) that value
  maps to. Use semantic reasoning:
  - Column name and meaning as primary clue
  - Value labels from enrichment if available
  - Value frequency as a secondary hint (more frequent values → more common types)
  - Null or "none" values → null (no additional subclass)
  Only assign a class if it makes clear semantic sense. Use null for values with no clear match.

AVAILABLE ONTOLOGY CLASSES: {', '.join(ontology_classes)}

Return ONLY a JSON object:
{{
  "is_type_dispatch": true or false,
  "discriminator_column": "{column_name}",
  "value_class_map": {{
    "<value1>": "ClassName or null",
    "<value2>": "ClassName or null"
  }},
  "confidence": <1-5>,
  "reasoning": "One sentence explaining the overall mapping logic."
}}"""

    # ------------------------------------------------------------------
    # Classifier: should we investigate a non-PK FK as hidden subclass?
    # ------------------------------------------------------------------

    def classify_hidden_subclass(
        self,
        table_name: str,
        candidate: Dict,
        understanding: Dict,
        enrichment: Dict,
        se_mappings: Dict,
        sh_mappings: Dict,
        ontology_classes: List[str],
    ) -> Dict:
        base_class  = get_base_class(table_name, se_mappings, sh_mappings) or "Unknown"
        col_meaning = understanding.get(table_name, {}).get("columns", {}).get(
                          candidate["column"], "No meaning available")
        table_meaning = understanding.get(table_name, {}).get("table_meaning", "")

        prompt   = self.prompt_hidden_subclass(
            table_name, base_class,
            candidate["column"], candidate["ref_table"], candidate["ref_class"],
            col_meaning, table_meaning, ontology_classes
        )
        response = self.get_llm_response(prompt)
        result   = self._parse_json_response(response)

        if result is None:
            result = {
                "is_hidden_subclass": False,
                "assigned_class":     None,
                "confidence":         0,
                "reasoning":          "LLM response could not be parsed"
            }
        return result

    # ------------------------------------------------------------------
    # Classifier: is a non-FK column a type discriminator?
    # ------------------------------------------------------------------

    def classify_type_dispatch(
        self,
        table_name: str,
        candidate: Dict,
        understanding: Dict,
        enrichment: Dict,
        se_mappings: Dict,
        sh_mappings: Dict,
        ontology_classes: List[str],
    ) -> Dict:
        base_class    = get_base_class(table_name, se_mappings, sh_mappings) or "Unknown"
        col_meaning   = understanding.get(table_name, {}).get("columns", {}).get(
                            candidate["column"], "No meaning available")
        table_meaning = understanding.get(table_name, {}).get("table_meaning", "")

        # Merge enum values from enrichment with any sample data
        enr_enums  = enrichment.get(table_name, {}).get("enum_interpretations", {})
        enum_vals  = enr_enums.get(candidate["column"], {})
        samples    = candidate.get("samples", {})

        prompt   = self.prompt_type_dispatch(
            table_name, base_class,
            candidate["column"], candidate["data_type"],
            col_meaning, table_meaning,
            enum_vals, samples, ontology_classes
        )
        response = self.get_llm_response(prompt)
        result   = self._parse_json_response(response)

        if result is None:
            result = {
                "is_type_dispatch":     False,
                "discriminator_column": candidate["column"],
                "value_class_map":      {},
                "confidence":           0,
                "reasoning":            "LLM response could not be parsed"
            }
        return result


# ============================================================
# Main entry point
# ============================================================

def run_hidden_mapping():
    """Discover and map hidden subclass and type-dispatch patterns."""

    print("=" * 55)
    print("  ONTOLOGY MAPPER — Phase 6 (Hidden Patterns)")
    print("=" * 55)

    # Load required inputs
    table_patterns   = load_json_safe(PATTERNS_FILE)
    tables_structure = load_json_safe(TABLES_STRUCTURE_FILE)
    understanding    = load_json_safe(UNDERSTANDING_FILE)
    enrichment       = load_json_safe(ENRICHMENT_FILE)
    column_samples   = load_json_optional(COLUMN_SAMPLES_FILE)

    print(f"  Patterns    : {len(table_patterns)} tables")
    print(f"  Understood  : {len(understanding)} tables")
    print(f"  Enriched    : {len(enrichment)} tables")
    print(f"  Col samples : {'loaded' if column_samples else 'not available'}")

    # Load previous phase mappings
    os.makedirs(MAPPINGS_DIR, exist_ok=True)
    se_mappings = load_json_optional(SE_MAPPINGS_FILE)
    sh_mappings = load_json_optional(SH_MAPPINGS_FILE)
    print(f"\n  SE mappings (Phase 1) : {len(se_mappings)} tables")
    print(f"  SH mappings (Phase 2) : {len(sh_mappings)} tables")

    # Parse ontology base IRI + all classes
    print(f"\nParsing ontology from '{ONTOLOGY_FILE}' ...")
    prefixes = parse_ontology_prefixes(ONTOLOGY_FILE)
    base_iri = get_ontology_base_iri(prefixes)
    print(f"  ✓ Base IRI: {base_iri}")

    print("Loading ontology classes...")
    ontology_classes = ontology_explorer(mode="classes")["classes"]
    print(f"  ✓ {len(ontology_classes)} classes loaded")

    # Only scan SE and SE_SH tables
    target_tables = {t: p for t, p in table_patterns.items() if p in ("SE", "SE_SH")}
    print(f"\n  Target tables (SE + SE_SH) : {len(target_tables)}")

    # Load existing caches
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    mappings_process: Dict = load_json_optional(PROCESS_FILE)
    hidden_mappings:  Dict = load_json_optional(HIDDEN_MAPPINGS_FILE)

    if mappings_process:
        print(f"  Loaded existing process cache ({len(mappings_process)} entries)")
    if hidden_mappings:
        print(f"  Loaded existing HIDDEN_mappings.json ({len(hidden_mappings)} entries)")

    mapper       = OntologyMapper(provider=SELECTED_PROVIDER)
    total        = len(target_tables)
    found_sh     = 0
    found_td     = 0
    errors       = []

    for idx, (table_name, pattern) in enumerate(target_tables.items(), 1):
        print(f"\n[{idx:>2}/{total}] {table_name}  [{pattern}]")

        if table_name not in hidden_mappings:
            hidden_mappings[table_name] = {
                "source_table":  table_name,
                "pattern":       pattern,
                "hidden_sh":     [],
                "type_dispatch": []
            }

        entry = hidden_mappings[table_name]

        # ── Pattern A: Hidden Subclass candidates ──────────────────
        sh_candidates = discover_hidden_subclass_candidates(
            table_name, tables_structure, se_mappings, sh_mappings
        )

        already_sh_cols = {h["trigger_column"] for h in entry.get("hidden_sh", [])}

        for cand in sh_candidates:
            col = cand["column"]
            if col in already_sh_cols:
                print(f"  [A] {col} → already processed, skipping")
                continue

            cache_key = f"{table_name}.{col}.hidden_sh"
            if cache_key in mappings_process:
                decision = mappings_process[cache_key]
                print(f"  [A] {col} → cached: is_hidden_subclass={decision.get('is_hidden_subclass')}")
            else:
                print(f"  [A] {col} → {cand['ref_table']} (asking LLM...)", end="", flush=True)
                try:
                    decision = mapper.classify_hidden_subclass(
                        table_name, cand, understanding, enrichment,
                        se_mappings, sh_mappings, ontology_classes
                    )
                    mappings_process[cache_key] = decision
                    with open(PROCESS_FILE, "w", encoding="utf-8") as f:
                        json.dump(mappings_process, f, indent=2)
                    print(f" → {decision.get('is_hidden_subclass')} / {decision.get('assigned_class')}")
                except Exception as e:
                    print(f" ✗ Error: {e}")
                    errors.append(cache_key)
                    continue

            if decision.get("is_hidden_subclass") and decision.get("assigned_class"):
                mapping_entry = build_hidden_sh_entry(
                    table_name, cand, decision, base_iri, se_mappings, sh_mappings
                )
                if mapping_entry:
                    entry["hidden_sh"].append(mapping_entry)
                    found_sh += 1
                    print(f"  ✓ HIDDEN_SH: {col} → :{decision['assigned_class']}"
                          f"  (conf={decision.get('confidence')}/5)")
            else:
                print(f"  ✗ {col} → not a hidden subclass"
                      f" ({decision.get('reasoning', '')[:60]})")

        # ── Pattern B: Type Dispatch candidates ────────────────────
        td_candidates = discover_type_dispatch_candidates(
            table_name, tables_structure, enrichment, column_samples
        )

        already_td_cols = {t["discriminator_column"] for t in entry.get("type_dispatch", [])}

        for cand in td_candidates:
            col = cand["column"]
            if col in already_td_cols:
                print(f"  [B] {col} → already processed, skipping")
                continue

            cache_key = f"{table_name}.{col}.type_dispatch"
            if cache_key in mappings_process:
                decision = mappings_process[cache_key]
                print(f"  [B] {col} → cached: is_type_dispatch={decision.get('is_type_dispatch')}")
            else:
                print(f"  [B] {col} (score={cand['score']}) → asking LLM...", end="", flush=True)
                try:
                    decision = mapper.classify_type_dispatch(
                        table_name, cand, understanding, enrichment,
                        se_mappings, sh_mappings, ontology_classes
                    )
                    mappings_process[cache_key] = decision
                    with open(PROCESS_FILE, "w", encoding="utf-8") as f:
                        json.dump(mappings_process, f, indent=2)
                    print(f" → {decision.get('is_type_dispatch')}")
                except Exception as e:
                    print(f" ✗ Error: {e}")
                    errors.append(cache_key)
                    continue

            if decision.get("is_type_dispatch") and decision.get("value_class_map"):
                mapping_entry = build_type_dispatch_entry(
                    table_name, cand, decision, base_iri, se_mappings, sh_mappings
                )
                if mapping_entry:
                    entry["type_dispatch"].append(mapping_entry)
                    found_td += 1
                    vmap = decision.get("value_class_map", {})
                    print(f"  ✓ TYPE_DISPATCH: {col} → {vmap}"
                          f"  (conf={decision.get('confidence')}/5)")
            else:
                print(f"  ✗ {col} → not a type discriminator"
                      f" ({decision.get('reasoning', '')[:60]})")

        # Save after each table
        with open(HIDDEN_MAPPINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(hidden_mappings, f, indent=2)

    # Final summary
    total_with_hidden = sum(
        1 for v in hidden_mappings.values()
        if v.get("hidden_sh") or v.get("type_dispatch")
    )

    print(f"\n{'='*55}")
    print("  PHASE 6 MAPPING COMPLETE")
    print(f"{'='*55}")
    print(f"  Tables scanned          : {total}")
    print(f"  Tables with hidden pat. : {total_with_hidden}")
    print(f"  Hidden subclass (A)     : {found_sh}")
    print(f"  Type dispatch (B)       : {found_td}")
    print(f"  Errors                  : {len(errors)}")
    if errors:
        print(f"  Failed keys             : {errors}")
    print(f"\n  Cache  → {PROCESS_FILE}")
    print(f"  Output → {HIDDEN_MAPPINGS_FILE}\n")


if __name__ == "__main__":
    try:
        run_hidden_mapping()
    except Exception as e:
        print(f"\nFatal error: {e}")
        import traceback
        traceback.print_exc()