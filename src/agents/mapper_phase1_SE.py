"""
Ontology Mapper Agent — Phase 1 (SE tables only)
Maps SE tables (concrete strong entities only) to their single best ontology class candidate.
Writes a structured JSON mapping file (SE_mappings.json) with resolved/unresolved FK references.

Key improvement over original:
  Column → predicate mapping now uses the ONTOLOGY PROPERTY INDEX instead of
  blindly camelCasing the column name.  This prevents incorrect predicates like
  :title being assigned to a 'name' column, or :siteUrl instead of :siteURL.
  Falls back to camelCase only when no ontology property matches.

Reads  : src2/memory/patterns_final.json
         src2/memory/understanding.json
         src2/memory/enrichment.json
         src2/outputs/DB_as_json/tables_structure.json
         src2/inputs/ontology/ontology.owl
Writes : src2/outputs/mappings_process.json         (LLM results cache, resumable)
         src2/outputs/mappings/SE_mappings.json     (final structured mapping)
"""

import json
import requests
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from typing import List, Dict, Any, Optional, Set, Tuple
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
PROCESS_FILE          = os.path.join(OUTPUT_DIR, "mappings_process.json")
SE_MAPPINGS_FILE      = os.path.join(MAPPINGS_DIR, "SE_mappings.json")


# ============================================================
# Ontology index — property lookup (mirrors verifier logic)
# ============================================================

class OntologyPropertyIndex:
    """
    Parses the OWL file and builds a lookup index so that
    build_se_json_mapping can find the exact property IRI for each
    column instead of inventing a camelCase name.

    Handles:
      - DataPropertyDomain (explicit)
      - ObjectUnionOf domains
      - Cardinality restrictions as implied domains (DataExactCardinality, etc.)
    """

    def __init__(self, owl_file: str):
        self.data_props: Dict  = {}   # name → {domain, domain_union, range}
        self.obj_props:  Dict  = {}   # name → {domain, range}
        self.subclass_of: Dict = defaultdict(set)
        self._parse(owl_file)
        self._close_subclass()

    def _local(self, iri: str) -> str:
        return iri.split("#")[-1] if "#" in iri else iri.split("/")[-1]

    def _parse(self, owl_file: str):
        root = ET.parse(owl_file).getroot()

        # ── Declarations ─────────────────────────────────────
        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag != "Declaration":
                continue
            ch = list(elem)
            if not ch:
                continue
            child_tag = ch[0].tag.split("}")[-1] if "}" in ch[0].tag else ch[0].tag
            iri = ch[0].get("IRI", "")
            if not iri:
                continue
            if child_tag == "DataProperty":
                self.data_props[self._local(iri)] = {
                    "domain": None, "domain_union": None, "range": None
                }
            elif child_tag == "ObjectProperty":
                self.obj_props[self._local(iri)] = {
                    "domain": None, "range": None
                }

        # ── Data property domains ─────────────────────────────
        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag == "DataPropertyDomain":
                ch = list(elem)
                if len(ch) < 2:
                    continue
                p = self._local(ch[0].get("IRI", ""))
                de = ch[1]
                dt = de.tag.split("}")[-1] if "}" in de.tag else de.tag
                if p not in self.data_props:
                    continue
                if dt == "ObjectUnionOf":
                    members = [
                        self._local(c.get("IRI", "") or c.get("abbreviatedIRI", ""))
                        for c in de
                        if c.get("IRI", "") or c.get("abbreviatedIRI", "")
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
                p = self._local(ch[0].get("IRI", ""))
                r = ch[1].get("IRI", "") or ch[1].get("abbreviatedIRI", "")
                if p in self.data_props:
                    self.data_props[p]["range"] = r

            elif tag == "ObjectPropertyDomain":
                ch = list(elem)
                if len(ch) < 2:
                    continue
                p = self._local(ch[0].get("IRI", ""))
                d = self._local(ch[1].get("IRI", ""))
                if p in self.obj_props and d:
                    self.obj_props[p]["domain"] = d

            elif tag == "ObjectPropertyRange":
                ch = list(elem)
                if len(ch) < 2:
                    continue
                p = self._local(ch[0].get("IRI", ""))
                r = self._local(ch[1].get("IRI", ""))
                if p in self.obj_props and r:
                    self.obj_props[p]["range"] = r

        # ── SubClassOf: hierarchy + implied domains ───────────
        for elem in root.iter():
            if not elem.tag.endswith("}SubClassOf"):
                continue
            ch = list(elem)
            if len(ch) != 2:
                continue

            # Hierarchy
            sub_iri = ch[0].get("IRI", "") or ch[0].get("abbreviatedIRI", "")
            sup_iri = ch[1].get("IRI", "") or ch[1].get("abbreviatedIRI", "")
            sub = self._local(sub_iri)
            sup = self._local(sup_iri)
            if sub and sup and sup not in ("Thing", ""):
                self.subclass_of[sub].add(sup)

            # Cardinality restriction → implied domain
            class_iri = ch[0].get("IRI", "")
            if not class_iri:
                continue
            class_name = self._local(class_iri)
            restr_tag  = ch[1].tag.split("}")[-1] if "}" in ch[1].tag else ch[1].tag
            if not restr_tag.startswith("Data"):
                continue
            for child in ch[1]:
                child_tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if child_tag == "DataProperty":
                    prop_name = self._local(child.get("IRI", ""))
                    if (prop_name in self.data_props
                            and not self.data_props[prop_name]["domain"]):
                        self.data_props[prop_name]["domain"] = class_name

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

    def _domain_matches(self, info: dict, ancestors: Set[str]) -> bool:
        d = info.get("domain")
        if not d:
            return True   # no domain declared → wildcard
        if d in ancestors:
            return True
        union = info.get("domain_union")
        if union and union & ancestors:
            return True
        return False

    def find_data_property(self, domain_class: str,
                           col_name: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Find the best matching data property for (domain_class, col_name).
        Returns (property_local_name, range_datatype) or (None, None).

        Search order (stricter first to avoid false positives):
          1. Exact case-insensitive match on property local name, domain-filtered
          2. Exact match ignoring underscores, domain-filtered
          3. Substring match, domain-filtered
          4. Exact match on any property (no domain filter) — last resort
        """
        ancestors = self.get_ancestors(domain_class)
        col_norm  = col_name.lower().replace("_", "")
        col_lower = col_name.lower()

        # Pass 1: exact match (case-insensitive), domain-aware
        for name, info in self.data_props.items():
            if self._domain_matches(info, ancestors):
                if name.lower() == col_norm or name.lower() == col_lower:
                    return name, info["range"]

        # Pass 2: underscore-stripped match, domain-aware
        for name, info in self.data_props.items():
            if self._domain_matches(info, ancestors):
                if name.lower().replace("_", "") == col_norm:
                    return name, info["range"]

        # Pass 3: substring match, domain-aware
        for name, info in self.data_props.items():
            if self._domain_matches(info, ancestors):
                n = name.lower()
                if col_norm in n or n in col_norm:
                    return name, info["range"]

        # Pass 4: exact match ignoring domain (e.g. :name with union domain not parsed)
        for name, info in self.data_props.items():
            if name.lower() == col_norm or name.lower() == col_lower:
                return name, info["range"]

        return None, None

    def find_obj_property(self, subject_class: str,
                          object_class: str) -> Optional[str]:
        """Find the object property linking subject_class → object_class."""
        subj_anc = self.get_ancestors(subject_class)
        obj_anc  = self.get_ancestors(object_class)
        for sa in subj_anc:
            for oa in obj_anc:
                for name, info in self.obj_props.items():
                    if info["domain"] == sa and info["range"] == oa:
                        return name
        return None


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
# XSD type map
# ============================================================

XSD_MAP = {
    "integer":   "xsd:integer",
    "int":       "xsd:integer",
    "bigint":    "xsd:integer",
    "smallint":  "xsd:integer",
    "boolean":   "xsd:boolean",
    "bool":      "xsd:boolean",
    "float":     "xsd:decimal",
    "double":    "xsd:decimal",
    "numeric":   "xsd:decimal",
    "decimal":   "xsd:decimal",
    "real":      "xsd:decimal",
    "date":      "xsd:date",
    "timestamp": "xsd:dateTime",
    "datetime":  "xsd:dateTime",
    "time":      "xsd:time",
    "character": "xsd:string",
    "varchar":   "xsd:string",
    "text":      "xsd:string",
    "char":      "xsd:string",
}


def _xsd_type(data_type: str) -> str:
    dt = data_type.lower().split("(")[0].strip()
    return XSD_MAP.get(dt, "xsd:string")


def _to_camel_case(name: str) -> str:
    parts = name.split("_")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


# ============================================================
# SE JSON mapping builder — ontology-aware predicate lookup
# ============================================================

def build_se_json_mapping(
    table_name:    str,
    ontology_class: str,
    attributes:    List[Dict],
    base_iri:      str,
    all_se_tables: set,
    prop_index:    OntologyPropertyIndex,
) -> Dict:
    """
    Build the structured JSON mapping for one SE table.

    Predicate assignment priority for each column:
      1. Ontology property lookup via OntologyPropertyIndex
         (exact match on property name, domain-filtered to ontology_class)
      2. camelCase fallback (original behaviour — used when no ontology
         property matches, e.g. internal FK columns not in the ontology)

    For FK join properties, we additionally try find_obj_property to get
    the correct object property IRI instead of a camelCased column name.
    """
    triple_map_iri = f"urn:r2rml:SE_{table_name}"

    pk_cols   = [a for a in attributes if a["role"] in ("pk", "pk+fk")]
    attr_cols = [a for a in attributes if a["role"] == "attribute"]
    fk_cols   = [a for a in attributes if a["role"] == "fk"]

    pk_template = (
        "/".join(f"{{{c['name']}}}" for c in pk_cols)
        if pk_cols else "{id}"
    )

    predicate_object_maps = []

    # ── Literal properties ────────────────────────────────────
    for attr in attr_cols:
        col      = attr["name"]
        ont_prop, ont_range = prop_index.find_data_property(ontology_class, col)

        if ont_prop:
            predicate = f":{ont_prop}"
            datatype  = ont_range if ont_range else _xsd_type(attr["data_type"])
        else:
            # Fallback: camelCase column name, XSD type from DB
            predicate = f":{_to_camel_case(col)}"
            datatype  = _xsd_type(attr["data_type"])
            print(f"    [WARN] No ontology property found for "
                  f"{ontology_class}.{col} → using fallback :{_to_camel_case(col)}")

        predicate_object_maps.append({
            "predicate": predicate,
            "object": {
                "type":     "literal",
                "column":   col,
                "datatype": datatype
            }
        })

    # ── FK join properties ────────────────────────────────────
    for fk in fk_cols:
        col       = fk["name"]
        ref_table = fk.get("fk_references", {}).get("table", "unknown")
        ref_col   = fk.get("fk_references", {}).get("column", "id")
        parent_iri = f"urn:r2rml:SE_{ref_table}"
        resolved   = ref_table in all_se_tables

        # Try to find the correct object property from ontology
        # We don't know the ref_table's class here, so we fall back to
        # camelCase for the predicate — the verifier will correct it later
        # using the full class map.  This is intentional: FK predicates
        # require knowing both subject AND object class, which the verifier
        # has access to but phase1 does not (ref_table may be SE_SH/SEw).
        predicate = f":{_to_camel_case(col)}"

        predicate_object_maps.append({
            "predicate": predicate,
            "object": {
                "type":               "join",
                "parent_triples_map": parent_iri,
                "resolved":           resolved,
                "join_condition": {
                    "child":  col,
                    "parent": ref_col
                }
            }
        })

    return {
        "pattern":               "SE",
        "triple_map_iri":        triple_map_iri,
        "logical_table":         table_name,
        "subject": {
            "template": f"{base_iri}{table_name}/{pk_template}",
            "class":    f":{ontology_class}"
        },
        "predicate_object_maps": predicate_object_maps
    }


# ============================================================
# Safe JSON loader
# ============================================================

def load_json_safe(path: str, label: str = "") -> Dict:
    label = label or path
    if not os.path.exists(path):
        raise FileNotFoundError(f"Required file not found: {path}")
    if os.path.getsize(path) == 0:
        raise ValueError(
            f"File is empty (0 bytes): {path}\n"
            f"Please regenerate it before running this agent."
        )
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in '{path}': {e}")


# ============================================================
# OntologyMapper (LLM agent — table → class only)
# ============================================================

class OntologyMapper:
    """Agent for mapping SE database tables to their best ontology class candidate."""

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
            print(f"\n  [RETRY] Model only returned thinking, requesting JSON...", end="", flush=True)
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
        url = (f"{self.config['api_url']}/{self.config['model_name']}"
               f":generateContent?key={self.config['api_key']}")
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

        has_explicit_pk   = len(pk_set) > 0
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
                attr["fk_references"] = {
                    "table":  fk_ref["table"],
                    "column": fk_ref["column"]
                }
            result.append(attr)
        return result

    def _build_mapping_prompt(
        self,
        table_name:           str,
        table_meaning:        str,
        entity_type:          str,
        column_meanings:      Dict[str, str],
        column_enrichment:    Dict[str, Dict],
        enum_interpretations: Dict[str, Dict],
        ontology_classes:     List[str],
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

        return f"""You are an ontology mapping expert. Find the SINGLE best matching ontology class for this database table.

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
        original = response
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
        print(f"  [WARN] Raw: {original[:400]}")
        return None

    def map_table(
        self,
        table_name:       str,
        pattern:          str,
        tables_structure: Dict,
        understanding:    Dict,
        enrichment:       Dict,
        ontology_classes: List[str],
    ) -> Dict:
        table_und            = understanding.get(table_name, {})
        table_meaning        = table_und.get("table_meaning", "Not available")
        column_meanings      = table_und.get("columns", {})
        table_enr            = enrichment.get(table_name, {})
        entity_type          = table_enr.get("entity_type", "unknown")
        column_enrichment    = table_enr.get("column_enrichment", {})
        enum_interpretations = table_enr.get("enum_interpretations", {})

        print(f"  entity_type={entity_type}  columns={len(column_meanings)}")

        attributes = self._build_attributes(table_name, tables_structure)

        prompt   = self._build_mapping_prompt(
            table_name, table_meaning, entity_type,
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

def run_se_mapping():
    """Map all concrete SE tables and write SE_mappings.json."""

    print("=" * 55)
    print("  ONTOLOGY MAPPER — Phase 1 (SE tables)")
    print("=" * 55)

    # Load all inputs
    table_patterns   = load_json_safe(PATTERNS_FILE,         "patterns_final.json")
    tables_structure = load_json_safe(TABLES_STRUCTURE_FILE, "tables_structure.json")
    understanding    = load_json_safe(UNDERSTANDING_FILE,     "understanding.json")
    enrichment       = load_json_safe(ENRICHMENT_FILE,        "enrichment.json")

    print(f"  Patterns   : {len(table_patterns)} tables")
    print(f"  Understood : {len(understanding)} tables")
    print(f"  Enriched   : {len(enrichment)} tables")

    # Parse ontology
    print(f"\nParsing ontology from '{ONTOLOGY_FILE}' ...")
    prefixes = parse_ontology_prefixes(ONTOLOGY_FILE)
    base_iri = get_ontology_base_iri(prefixes)
    print(f"  ✓ Base IRI: {base_iri}")

    # Build property index — used for column→predicate lookup
    print("  Building ontology property index...")
    prop_index = OntologyPropertyIndex(ONTOLOGY_FILE)
    print(f"  ✓ {len(prop_index.data_props)} data properties indexed")
    print(f"  ✓ {len(prop_index.obj_props)} object properties indexed")

    # Filter to concrete SE only
    se_tables     = {t: p for t, p in table_patterns.items() if p == "SE"}
    all_se_tables = set(se_tables.keys())
    print(f"\n  SE (concrete) : {len(se_tables)} tables")

    # Load ontology classes for LLM
    print("\nLoading ontology classes...")
    ontology_classes = ontology_explorer(mode="classes")["classes"]
    print(f"  ✓ {len(ontology_classes)} classes loaded")

    # Load existing caches for resumable runs
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(MAPPINGS_DIR, exist_ok=True)

    mappings_process: Dict = {}
    if os.path.exists(PROCESS_FILE) and os.path.getsize(PROCESS_FILE) > 0:
        try:
            with open(PROCESS_FILE, "r", encoding="utf-8") as f:
                mappings_process = json.load(f)
            print(f"\n  Loaded mappings_process.json ({len(mappings_process)} entries)")
        except json.JSONDecodeError:
            print(f"\n  [WARN] mappings_process.json corrupt — starting fresh")

    se_mappings: Dict = {}
    if os.path.exists(SE_MAPPINGS_FILE) and os.path.getsize(SE_MAPPINGS_FILE) > 0:
        try:
            with open(SE_MAPPINGS_FILE, "r", encoding="utf-8") as f:
                se_mappings = json.load(f)
            print(f"  Loaded SE_mappings.json ({len(se_mappings)} entries)")
        except json.JSONDecodeError:
            print(f"  [WARN] SE_mappings.json corrupt — starting fresh")

    mapper  = OntologyMapper(provider=SELECTED_PROVIDER)
    success = 0
    errors  = []
    total   = len(se_tables)

    for idx, (table_name, pattern) in enumerate(se_tables.items(), 1):
        print(f"\n[{idx:>2}/{total}] {table_name}  [{pattern}]")

        if table_name not in mappings_process:
            try:
                record = mapper.map_table(
                    table_name, pattern,
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

        record     = mappings_process[table_name]
        ont_class  = record.get("ontology_mapping", {}).get("ontology_class")
        attributes = record.get("attributes", [])

        if ont_class:
            se_mappings[table_name] = build_se_json_mapping(
                table_name, ont_class, attributes,
                base_iri, all_se_tables, prop_index   # ← pass index
            )
            with open(SE_MAPPINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(se_mappings, f, indent=2)
            print(f"  ✓ JSON mapping built → :{ont_class}")
        else:
            print(f"  ⚠ No ontology class — skipping JSON mapping for this table")

    print(f"\n{'='*55}")
    print("  MAPPING COMPLETE")
    print(f"{'='*55}")
    print(f"  Mapped successfully  : {success}")
    print(f"  Errors               : {len(errors)}")
    if errors:
        print(f"  Failed tables        : {errors}")
    print(f"\n  Cache  → {PROCESS_FILE}")
    print(f"  Output → {SE_MAPPINGS_FILE}\n")


if __name__ == "__main__":
    try:
        run_se_mapping()
    except Exception as e:
        print(f"\nFatal error: {e}")
        import traceback
        traceback.print_exc()