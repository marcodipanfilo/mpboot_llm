"""
Ontology Mapper Agent — Phase 1 (SE tables only)
Maps SE tables (concrete strong entities only) to their single best ontology class candidate.
Writes a structured JSON mapping file (SE_mappings.json) with resolved/unresolved FK references.

Column → predicate mapping strategy (in build_se_json_mapping):
  1. Fetch all data properties and object properties of the mapped class via
     ontology_explorer(mode="class_properties", class_name=...).
  2. For each non-FK column:
       a. rdfs:label  — if LLM label call marks this column as a human-readable label
       b. String similarity match against data properties (fast, no LLM cost)
       c. LLM semantic selection — sends column name + meaning from understanding.json
          + available unused properties; the LLM picks the best semantic fit
       d. camelCase fallback (always unique)
  3. For each FK column:
       a. String similarity match against object properties
       b. LLM semantic selection (same as above, for object properties)
       c. camelCase fallback
  4. rdfs:label candidates — a second LLM call decides which columns (if any)
     represent human-readable labels (name, title, caption, display, text …).

The LLM semantic selection step (2c / 3b) is the key fix: it receives the full
semantic meaning of the column from understanding.json, preventing cases where
string-similarity alone assigns the wrong predicate (e.g. lat/long → :hasPostalCode).

Duplicate-predicate prevention:
  - A set of already-used predicates is maintained as columns are processed.
  - _match_property() skips any property already claimed by an earlier column.
  - _llm_select_predicate() only shows the LLM the currently unused properties.
  - The label LLM call is told which predicates are already taken.
  - All LLM calls receive the full table understanding context (table_meaning +
    column meanings from understanding.json) so decisions are semantically grounded.

Reads  : src/memory/patterns_final.json
         src/memory/understanding.json
         src/memory/enrichment.json
         src/outputs/DB_as_json/tables_structure.json
         src/inputs/ontology/ontology.owl
         src/outputs/mappings/SE_mappings.json   (read live to check used predicates)
Writes : src/outputs/mappings_process.json         (LLM results cache, resumable)
         src/outputs/mappings/SE_mappings.json     (final structured mapping)
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
MEMORY_FOLDER         = "src/memory"
DB_JSON_FOLDER        = "src/outputs/DB_as_json"
PATTERNS_FILE         = os.path.join(MEMORY_FOLDER, "patterns_final.json")
UNDERSTANDING_FILE    = os.path.join(MEMORY_FOLDER, "understanding.json")
ENRICHMENT_FILE       = os.path.join(MEMORY_FOLDER, "enrichment.json")
TABLES_STRUCTURE_FILE = os.path.join(DB_JSON_FOLDER, "tables_structure.json")
ONTOLOGY_FILE         = "src/inputs/ontology/ontology.owl"
OUTPUT_DIR            = "src/outputs"
MAPPINGS_DIR          = os.path.join(OUTPUT_DIR, "mappings")
PROCESS_FILE          = os.path.join(OUTPUT_DIR, "mappings_process.json")
SE_MAPPINGS_FILE      = os.path.join(MAPPINGS_DIR, "SE_mappings.json")
CONSTRAINT_META_FILE  = "src/inputs/database/constraint_metadata.json"

RDFS_LABEL = "rdfs:label"

# ── Known label-like column name fragments (quick pre-filter for label call) ──
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
# Helpers — used-predicate tracker
# ============================================================

def _load_used_predicates(table_name: str) -> Set[str]:
    """
    Read the current SE_mappings.json and return the set of predicates
    already assigned for *table_name*.  Returns an empty set if the file
    does not exist or the table has no entry yet.

    This is called once per table at the start of build_se_json_mapping
    so that the in-progress mapping for this run is also reflected (the
    file is written incrementally after each table).
    """
    if not os.path.exists(SE_MAPPINGS_FILE):
        return set()
    try:
        with open(SE_MAPPINGS_FILE, "r", encoding="utf-8") as f:
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
# LLM Interaction Logger — concise output
# ============================================================

def log_llm_request(table_name: str, prompt: str, context: Dict) -> None:
    pass  # Prompt details not needed in console

def log_llm_response(table_name: str, raw_response: str, parsed: Optional[Dict]) -> None:
    if parsed:
        print(f"  LLM → :{parsed.get('ontology_class', '?')}  (score {parsed.get('score', '?')}/5)")
    else:
        print(f"  LLM → parsing failed")

def log_label_llm_request(table_name: str, ontology_class: str,
                           prompt: str, candidates: List[str],
                           used_predicates: Set[str]) -> None:
    pass

def log_label_llm_response(table_name: str, raw: str, decided: List[str]) -> None:
    if decided:
        print(f"    rdfs:label → {decided}")

def log_column_mapping_request(table_name: str, ontology_class: str,
                                prompt: str, col: str,
                                used_predicates: Set[str],
                                available_props: List[str]) -> None:
    pass

def log_column_mapping_response(col: str, raw: str, chosen: Optional[str]) -> None:
    pass


# ============================================================
# Ontology index — subclass hierarchy (properties via explorer)
# ============================================================

class OntologyPropertyIndex:

    def __init__(self, owl_file: str):
        self.data_props:  Dict = {}
        self.obj_props:   Dict = {}
        self.subclass_of: Dict = defaultdict(set)
        self._parse(owl_file)
        self._close_subclass()
        self._display_parsed_index()

    def _display_parsed_index(self):
        print(f"  Ontology index: {len(self.data_props)} data props, "
              f"{len(self.obj_props)} obj props, "
              f"{len(self.subclass_of)} subclass relations")

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
                self.data_props[self._local(iri)] = {
                    "domain": None, "domain_union": None, "range": None
                }
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
                        for c in de
                        if c.get("IRI", "") or c.get("abbreviatedIRI", "")
                    ]
                    if members:
                        self.data_props[p]["domain"]      = members[0]
                        self.data_props[p]["domain_union"] = set(members)
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
            class_iri = ch[0].get("IRI", "") or ch[0].get("abbreviatedIRI", "")
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

        # ── RDF/XML fallback ──────────────────────────────────
        # If no Declaration tags were found, parse RDF/XML format instead.
        if not self.data_props and not self.obj_props:
            print("  [OntologyPropertyIndex] No Declaration tags — trying RDF/XML parsing")
            OWL  = "http://www.w3.org/2002/07/owl#"
            RDF  = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
            RDFS = "http://www.w3.org/2000/01/rdf-schema#"

            for elem in root.iter(f"{{{OWL}}}Class"):
                iri = elem.get(f"{{{RDF}}}about", "")
                if iri and "owl#" not in iri:
                    cls_name = self._local(iri)
                    for sub in elem.findall(f"{{{RDFS}}}subClassOf"):
                        parent_iri = sub.get(f"{{{RDF}}}resource", "")
                        if parent_iri:
                            parent = self._local(parent_iri)
                            if parent and parent != "Thing":
                                self.subclass_of[cls_name].add(parent)

            for elem in root.iter(f"{{{OWL}}}DatatypeProperty"):
                iri = elem.get(f"{{{RDF}}}about", "")
                if not iri:
                    continue
                name = self._local(iri)
                info = {"domain": None, "domain_union": None, "range": None}
                dom = elem.find(f"{{{RDFS}}}domain")
                if dom is not None:
                    d = dom.get(f"{{{RDF}}}resource", "")
                    if d:
                        info["domain"] = self._local(d)
                rng = elem.find(f"{{{RDFS}}}range")
                if rng is not None:
                    r = rng.get(f"{{{RDF}}}resource", "")
                    if r:
                        info["range"] = r
                self.data_props[name] = info

            for elem in root.iter(f"{{{OWL}}}ObjectProperty"):
                iri = elem.get(f"{{{RDF}}}about", "")
                if not iri:
                    continue
                name = self._local(iri)
                info = {"domain": None, "range": None}
                dom = elem.find(f"{{{RDFS}}}domain")
                if dom is not None:
                    d = dom.get(f"{{{RDF}}}resource", "")
                    if d:
                        info["domain"] = self._local(d)
                rng = elem.find(f"{{{RDFS}}}range")
                if rng is not None:
                    r = rng.get(f"{{{RDF}}}resource", "")
                    if r:
                        info["range"] = self._local(r)
                self.obj_props[name] = info

            print(f"  [OntologyPropertyIndex] RDF/XML: {len(self.data_props)} data props, "
                  f"{len(self.obj_props)} obj props")

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

    print(f"\n  ┌── ONTOLOGY PREFIXES ({len(prefixes)}) ──┐")
    if prefixes:
        for name, iri in sorted(prefixes.items(), key=lambda x: x[0]):
            display_name = name if name else '(default/base)'
            print(f"    • {display_name:20s} → {iri}")
    else:
        print(f"    (no prefixes found)")

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
    """Lowercase, strip underscores and hyphens for loose comparison."""
    return s.lower().replace("_", "").replace("-", "")


def _match_property(
    col_name:       str,
    prop_list:      List[str],
    used_predicates: Set[str],
) -> Optional[str]:
    """
    Find the best matching property name from prop_list for col_name,
    skipping any already-used predicate.

    Pass 1: Exact normalised match
    Pass 2: Substring match WITH quality threshold (≥60% coverage).
            Prevents "name" (4 chars) from matching "name_sponsor" (11 chars).
            When multiple candidates pass, highest quality wins.

    Returns the original property name, or None.
    """
    col_norm = _norm(col_name)
    MIN_MATCH_QUALITY = 0.6

    def _already_used(prop: str) -> bool:
        return f":{prop}" in used_predicates

    # Pass 1 — exact normalised match
    for p in prop_list:
        if _norm(p) == col_norm or _norm(_strip_owl_prefix(p)) == col_norm:
            if _already_used(p):
                continue
            return p

    # Pass 2 — substring match with quality threshold
    candidates = []
    for p in prop_list:
        if _already_used(p):
            continue
        pn = _norm(p)
        pn_stripped = _norm(_strip_owl_prefix(p))
        quality = 0.0
        for a, b in [(col_norm, pn), (col_norm, pn_stripped)]:
            if a and b:
                if a in b:
                    quality = max(quality, len(a) / len(b))
                if b in a:
                    quality = max(quality, len(b) / len(a))
        if quality >= MIN_MATCH_QUALITY:
            candidates.append((p, quality, len(p)))

    if candidates:
        candidates.sort(key=lambda x: (x[1], x[2]), reverse=True)
        return candidates[0][0]

    return None


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
    prop_kind:       str,          # "data" or "object"
    constraint_hint: str = "",     # FK constraint name hint
) -> str:
    """
    Build the prompt for the LLM column→predicate selection call.

    Sends:
      - Full table context (table_meaning, ontology class)
      - The column name, its semantic description from understanding.json,
        its DB data type
      - The list of available ontology properties (unused ones only)
      - The set of already-used predicates (for awareness)
      - Whether we are choosing a data property or an object property
    """
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

    constraint_block = ""
    if constraint_hint:
        constraint_block = f"""
FK CONSTRAINT NAME (strong semantic hint — property names are often embedded):
  {constraint_hint}
"""

    return f"""You are an ontology mapping expert.

TABLE: {table_name}
TABLE MEANING: {table_meaning}
ONTOLOGY CLASS MAPPED TO THIS TABLE: :{ontology_class}

COLUMN TO MAP:
  name     : {col_name}
  meaning  : {col_meaning}
  db type  : {col_db_type}
{constraint_block}
AVAILABLE ONTOLOGY {prop_kind.upper()} PROPERTIES (already-used ones excluded):
{props_block}

PREDICATES ALREADY ASSIGNED TO OTHER COLUMNS IN THIS TABLE:
{used_block}

TASK: {kind_instruction}

Rules:
  - Use the column's SEMANTIC MEANING (above) as the primary guide, not just its name.
  - PRIORITIZE the FK constraint name hint when available — it often encodes the property name.
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
    constraint_hint: str = "",
) -> Optional[str]:
    """
    Ask the LLM to choose the best ontology property for one column.
    Returns the local property name (e.g. 'hasLatitude') or None.
    Only calls the LLM when there are available properties to choose from.
    """
    if not available_props:
        print(f"        [LLM-PRED] No available props — skipping LLM call for {col_name!r}")
        return None

    prompt = _build_column_predicate_prompt(
        table_name, table_meaning, ontology_class,
        col_name, col_meaning, col_db_type,
        available_props, used_predicates, prop_kind,
        constraint_hint=constraint_hint
    )

    raw = mapper.get_llm_response(prompt)

    # Parse response
    try:
        cleaned = re.sub(r'```json\s*', '', raw)
        cleaned = re.sub(r'```\s*', '', cleaned).strip()
        j_start = cleaned.find("{")
        j_end   = cleaned.rfind("}") + 1
        if j_start != -1 and j_end > 0:
            obj = json.loads(cleaned[j_start:j_end])
            chosen = obj.get("chosen_property")
            if chosen and chosen in available_props:
                print(f"        [LLM-PRED] ✓ LLM chose: {chosen!r}")
                return chosen
            elif chosen is None or chosen == "null":
                print(f"        [LLM-PRED] LLM returned null — no suitable property")
                return None
            else:
                print(f"        [LLM-PRED] LLM chose {chosen!r} but it's not in available props — ignoring")
                return None
    except Exception as e:
        print(f"        [LLM-PRED] Parse error: {e} — raw: {raw[:200]!r}")

    # Regex fallback
    m = re.search(r'"chosen_property"\s*:\s*"([^"]+)"', raw)
    if m:
        chosen = m.group(1)
        if chosen in available_props:
            print(f"        [LLM-PRED] ✓ LLM chose (regex fallback): {chosen!r}")
            return chosen
    return None


# ============================================================
# Class properties fetcher — via ontology_explorer
# ============================================================

def _fetch_class_properties(ontology_class: str) -> Tuple[List[str], List[str]]:
    """
    Call ontology_explorer(mode="class_properties", class_name=ontology_class)
    and return (data_property_names, object_property_names) as plain local-name strings.
    """
    try:
        result = ontology_explorer(mode="class_properties", class_name=ontology_class)
    except Exception as e:
        print(f"    [WARN] ontology_explorer failed for class {ontology_class!r}: {e}")
        return [], []


    print(f"    │  raw keys: {list(result.keys()) if isinstance(result, dict) else type(result)}")

    # If ontology_explorer returned an error, the class doesn't exist in the ontology
    if isinstance(result, dict) and "error" in result:
        print(f"    │  [ERROR] Class not found in ontology: {result['error']}")
        print(f"    └────────────────────────────────────────────────────────\n")
        return [], [], False   # (data_props, obj_props, class_valid=False)

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

        # flat list with a "type" discriminator
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
                # ontology_explorer returns "property_name" and "property_iri"
                # also handle legacy keys "name", "local_name", "iri"
                n = (entry.get("property_name")
                     or entry.get("name")
                     or entry.get("local_name")
                     or "")
                if n:
                    return n.split("#")[-1] if "#" in n else n.split("/")[-1]
                # fallback: derive from IRI
                iri = entry.get("property_iri") or entry.get("iri", "")
                return iri.split("#")[-1] if "#" in iri else iri.split("/")[-1]
            return str(entry)

        data_props = [_to_local(e) for e in dp_raw if e]
        obj_props  = [_to_local(e) for e in op_raw if e]

    print(f"    │  data_properties   ({len(data_props)}): {data_props}")
    print(f"    │  object_properties ({len(obj_props)}): {obj_props}")
    print(f"    └────────────────────────────────────────────────────────\n")

    return data_props, obj_props, True  # class_valid=True


# ============================================================
# rdfs:label column detector — LLM call, duplicate-aware
# ============================================================

def _build_label_prompt(
    table_name:      str,
    ontology_class:  str,
    candidates:      List[str],
    col_meanings:    Dict[str, str],
    table_meaning:   str,
    used_predicates: Set[str],
) -> str:
    """
    Build the prompt for the rdfs:label LLM call.

    Includes:
      - Full table understanding (table_meaning + per-column meanings)
      - The list of candidate columns with their semantic descriptions
      - Already-used predicates so the LLM knows not to re-suggest rdfs:label
        if it was already assigned to a prior column in this table
    """
    col_lines = []
    for col in candidates:
        meaning = col_meanings.get(col, "(no description available)")
        col_lines.append(f"  - {col}: {meaning}")
    col_block = "\n".join(col_lines)

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


def _format_all_col_meanings(col_meanings: Dict[str, str]) -> str:
    """Format all column meanings for inclusion in a prompt."""
    if not col_meanings:
        return "  (none available)"
    return "\n".join(f"  - {col}: {meaning}" for col, meaning in col_meanings.items())


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
    Pre-filters to columns whose name contains a known label-hint word.
    Passes used_predicates so the LLM is aware of what is already mapped.
    Returns a set of confirmed column names.
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
    log_label_llm_request(table_name, ontology_class, prompt, candidates, used_predicates)

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

    # Validate: only keep names that were in the candidate list
    valid = set(candidates)
    label_cols = [c for c in label_cols if c in valid]

    log_label_llm_response(table_name, raw, label_cols)
    return set(label_cols)


# ============================================================
# SE JSON mapping builder — duplicate-aware, context-rich
# ============================================================

def build_se_json_mapping(
    table_name:     str,
    ontology_class: str,
    attributes:     List[Dict],
    base_iri:       str,
    all_se_tables:  set,
    mapper:         "OntologyMapper",
    col_meanings:   Dict[str, str],
    table_meaning:  str,
    constraint_meta: Dict = None,
) -> Dict:
    """
    Build the structured JSON mapping for one SE table.

    Column → predicate resolution (per column type):

    Non-FK attribute columns  (processed in order):
      1. rdfs:label  — if the LLM label call marks this column as a human-readable
                       label AND rdfs:label is not yet in used_predicates
      2. String similarity match — fast check against data property names
      3. LLM semantic selection — sends column meaning from understanding.json +
                                  list of unused data properties; LLM picks best fit.
                                  This prevents wrong matches like lat/long → :hasPostalCode
      4. camelCase fallback (always unique because it encodes the column name)

    FK columns  (processed after attribute columns):
      1. String similarity match against object properties
      2. LLM semantic selection (same mechanism, for object properties)
      3. camelCase fallback

    used_predicates is seeded from the current SE_mappings.json on disk (if any)
    so that even predicates assigned by a previous run are respected, then grows
    as each column is processed within this run.

    All LLM calls receive:
      - table_meaning and col_meanings from understanding.json
      - the column's DB data type
      - the current used_predicates set at the moment of each call
      - only the *unused* subset of available ontology properties
    """
    triple_map_iri = f"urn:r2rml:SE_{table_name}"

    pk_cols   = [a for a in attributes if a["role"] in ("pk", "pk+fk")]
    attr_cols = [a for a in attributes if a["role"] == "attribute"]
    fk_cols   = [a for a in attributes if a["role"] == "fk"]

    if pk_cols:
        pk_template = "/".join(f"{{{c['name']}}}" for c in pk_cols)
    else:
        # No declared PK — use all non-FK columns as natural composite key.
        # Never fall back to a synthetic {id} that doesn't exist in the DB.
        non_fk = [a for a in attributes if a["role"] != "fk"]
        pk_template = "/".join(f"{{{c['name']}}}" for c in non_fk) if non_fk else "/".join(f"{{{c['name']}}}" for c in attributes)


    # ── Step 1: seed used_predicates from disk ─────────────────────────────────
    # This ensures that if SE_mappings.json already has an entry for this table
    # (e.g. from a previous partial run), we don't re-assign the same predicates.
    used_predicates: Set[str] = _load_used_predicates(table_name)

    # ── Step 2: fetch class properties from ontology_explorer ─────────────────
    data_prop_names, obj_prop_names, class_valid = _fetch_class_properties(ontology_class)
    if not class_valid:
        print(f"    [WARN] Class {ontology_class!r} not found in ontology — "
              f"all columns will use camelCase fallback predicates")

    # ── Step 3: LLM decides which attribute columns → rdfs:label ──────────────
    # Pass used_predicates so it knows if rdfs:label is already taken.
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

    # ── Step 4: map non-FK attribute columns (in order, tracking used set) ────
    for attr in attr_cols:
        col         = attr["name"]
        col_meaning = col_meanings.get(col, "(no description)")
        col_db_type = attr.get("data_type", "unknown")

        emit_rdfs_label = col in label_columns

        if data_prop_names:
            matched = _match_property(col, data_prop_names, used_predicates)
            if matched:
                predicate = f":{matched}"
                datatype  = _xsd_type(col_db_type)
                print(f"      {col!r} → {predicate}  [string-match]")
            else:
                unused_data_props = [
                    p for p in data_prop_names if f":{p}" not in used_predicates
                ]
                chosen = _llm_select_predicate(
                    table_name, table_meaning, ontology_class,
                    col, col_meaning, col_db_type,
                    unused_data_props, used_predicates, "data", mapper
                ) if unused_data_props else None
                if chosen:
                    predicate = f":{chosen}"
                    datatype  = _xsd_type(col_db_type)
                    print(f"      {col!r} → {predicate}  [LLM]")
                else:
                    predicate = f":{_to_camel_case(col)}"
                    datatype  = _xsd_type(col_db_type)
                    print(f"      {col!r} → {predicate}  [camelCase fallback]")
        else:
            predicate = f":{_to_camel_case(col)}"
            datatype  = _xsd_type(col_db_type)

        used_predicates.add(predicate)
        predicate_object_maps.append({
            "predicate": predicate,
            "object": {
                "type":     "literal",
                "column":   col,
                "datatype": datatype,
            }
        })

        if emit_rdfs_label and predicate != RDFS_LABEL:
            predicate_object_maps.append({
                "predicate": RDFS_LABEL,
                "object": {
                    "type":     "literal",
                    "column":   col,
                    "datatype": "xsd:string",
                }
            })

    # ── Step 5: map FK columns to object properties (skip already-used) ───────
    # Load constraint metadata for this table
    fk_constraints = {}
    if constraint_meta:
        table_meta = constraint_meta.get(table_name, {})
        fk_constraints = table_meta.get("fk_constraints", {})

    for fk in fk_cols:
        col        = fk["name"]
        ref_table  = fk.get("fk_references", {}).get("table", "unknown")
        ref_col    = fk.get("fk_references", {}).get("column", "id")
        parent_iri = f"urn:r2rml:SE_{ref_table}"
        resolved   = ref_table in all_se_tables
        col_meaning = col_meanings.get(col, "(no description)")
        col_db_type = fk.get("data_type", "unknown")

        # Get constraint name hint for this FK column
        fk_meta = fk_constraints.get(col, {})
        constraint_hint = fk_meta.get("constraint_name", "")

        if obj_prop_names:
            matched = _match_property(col, obj_prop_names, used_predicates)
            if matched:
                predicate = f":{matched}"
                print(f"      {col!r} (FK→{ref_table}) → {predicate}  [string-match]")
            else:
                unused_obj_props = [
                    p for p in obj_prop_names if f":{p}" not in used_predicates
                ]
                chosen = _llm_select_predicate(
                    table_name, table_meaning, ontology_class,
                    col, col_meaning, col_db_type,
                    unused_obj_props, used_predicates, "object", mapper,
                    constraint_hint=constraint_hint
                ) if unused_obj_props else None
                if chosen:
                    predicate = f":{chosen}"
                    print(f"      {col!r} (FK→{ref_table}) → {predicate}  [LLM]")
                else:
                    predicate = f":{_to_camel_case(col)}"
                    print(f"      {col!r} (FK→{ref_table}) → {predicate}  [camelCase fallback]")
        else:
            predicate = f":{_to_camel_case(col)}"
            print(f"      {col!r} (FK→{ref_table}) → {predicate}  [camelCase — no obj props]")

        used_predicates.add(predicate)
        predicate_object_maps.append({
            "predicate": predicate,
            "object": {
                "type":               "join",
                "parent_triples_map": parent_iri,
                "resolved":           resolved,
                "join_condition": {
                    "child":  col,
                    "parent": ref_col,
                }
            }
        })




    return {
        "pattern":               "SE",
        "triple_map_iri":        triple_map_iri,
        "logical_table":         table_name,
        "subject": {
            "template": f"{base_iri}{table_name}/{pk_template}",
            "class":    f":{ontology_class}",
        },
        "predicate_object_maps": predicate_object_maps,
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
# OntologyMapper (LLM agent)
# ============================================================

class OntologyMapper:
    """
    LLM agent responsible for:
      1. Mapping a table name → best ontology class  (map_table)
      2. Deciding which columns should use rdfs:label (decide_label_columns,
         called from build_se_json_mapping via get_llm_response)
    """

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
    _BACKOFF_BASE     = 2

    def _should_retry(self, status_code: int) -> bool:
        return status_code in self._RETRYABLE_STATUS

    def _wait(self, attempt: int, status_code: int) -> None:
        wait = self._BACKOFF_BASE * (2 ** attempt)
        print(f"  [RETRY {attempt+1}/{self._MAX_RETRIES}] HTTP {status_code} — "
              f"retrying in {wait}s ...", flush=True)
        import time; time.sleep(wait)

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
        # No implicit PK inference — if the schema has no PK declared,
        # pk_cols will be empty and the template builder uses non-FK columns.

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
        used_classes:         Optional[Set[str]] = None,
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

        # Filter out already-used classes so LLM can't double-assign
        available = [c for c in ontology_classes
                     if not (used_classes and c in used_classes)]

        # If all classes already used, send full list — re-use is better than hallucination
        if not available:
            available = list(ontology_classes)
            extra_note = "\nNOTE: All classes are already assigned. Pick the CLOSEST semantic match from the list above even if it was used. Do NOT invent class names not in this list."
        else:
            extra_note = ""

        return f"""You are an ontology mapping expert. Find the SINGLE best matching ontology class for this database table. Prefer a match that is very high both syntactically AND semantically. Prefer the class whose local name most directly matches the table name — do NOT pick a subclass when the parent class name is a better syntactic match.

TABLE: {table_name}
TABLE MEANING: {table_meaning}
ENTITY TYPE: {entity_type}

COLUMNS (with meanings and roles):
{columns_block}
{enums_block}
AVAILABLE ONTOLOGY CLASSES (classes already assigned to other tables are excluded):
{', '.join(available)}

IMPORTANT: If the table name closely matches a class name (e.g. 'persons' → 'Person',
'committees' → 'Committee'), prefer that class even if a subclass seems more specific.
Subclasses should only be chosen if the table name directly suggests them.
{extra_note}
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
        used_classes:     Optional[Set[str]] = None,
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

        # ── Canonical name-match: skip LLM if table name ≡ class name ────
        def _canonical_norm(s: str) -> str:
            return s.lower().replace("_", "").replace("-", "").rstrip("s")

        table_norm = _canonical_norm(table_name)
        canonical_class = None
        for cls in ontology_classes:
            if _canonical_norm(cls) == table_norm:
                canonical_class = cls
                print(f"  ✓ [CANONICAL] table={table_name!r} → :{cls}  (no LLM needed)")
                break

        if canonical_class:
            mapping = {
                "ontology_class": canonical_class,
                "score": 5,
                "why": f"Canonical name match: '{table_name}' ≡ '{canonical_class}'"
            }
        else:
            prompt = self._build_mapping_prompt(
                table_name, table_meaning, entity_type,
                column_meanings, column_enrichment, enum_interpretations,
                ontology_classes, used_classes=used_classes
            )

            log_llm_request(
                table_name=table_name,
                prompt=prompt,
                context={
                    "table_name":           table_name,
                    "table_meaning":        table_meaning,
                    "entity_type":          entity_type,
                    "column_meanings":      column_meanings,
                    "column_enrichment":    column_enrichment,
                    "enum_interpretations": enum_interpretations,
                    "ontology_classes":     ontology_classes,
                }
            )

            response = self.get_llm_response(prompt)
            mapping  = self._parse_mapping_response(response)

            log_llm_response(
                table_name=table_name,
                raw_response=response,
                parsed=mapping
            )

            if mapping:
                chosen_cls = mapping['ontology_class']
                if chosen_cls not in ontology_classes:
                    # LLM hallucinated a non-existent class — pick closest real one
                    print(f"  ⚠ LLM returned non-existent class {chosen_cls!r} — finding closest match")
                    norm = lambda s: s.lower().replace("_","").replace("-","")
                    best = min(ontology_classes, key=lambda c: (
                        0 if norm(c) == norm(chosen_cls) else
                        1 if norm(chosen_cls) in norm(c) or norm(c) in norm(chosen_cls) else 2
                    ))
                    print(f"  ✓ Remapped {chosen_cls!r} → {best!r}")
                    mapping['ontology_class'] = best
                    mapping['why'] = f"LLM chose non-existent '{chosen_cls}', remapped to closest: '{best}'"
                print(f"  ✓ → {mapping['ontology_class']}  (score {mapping.get('score')}/5)")
            else:
                print(f"  ✗ mapping failed")

        return {
            "table":           table_name,
            "pattern":         pattern,
            "table_meaning":   table_meaning,
            "entity_type":     entity_type,
            "attributes":      attributes,
            "column_meanings": column_meanings,   # stored so label call can re-use it
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

    table_patterns   = load_json_safe(PATTERNS_FILE,         "patterns_final.json")
    tables_structure = load_json_safe(TABLES_STRUCTURE_FILE, "tables_structure.json")
    understanding    = load_json_safe(UNDERSTANDING_FILE,     "understanding.json")
    enrichment       = load_json_safe(ENRICHMENT_FILE,        "enrichment.json")

    print(f"  Patterns   : {len(table_patterns)} tables")
    print(f"  Understood : {len(understanding)} tables")
    print(f"  Enriched   : {len(enrichment)} tables")

    print(f"\nParsing ontology from '{ONTOLOGY_FILE}' ...")
    prefixes = parse_ontology_prefixes(ONTOLOGY_FILE)
    base_iri = get_ontology_base_iri(prefixes)
    print(f"  ✓ Base IRI: {base_iri}")

    print("  Building ontology property index (subclass hierarchy)...")
    prop_index = OntologyPropertyIndex(ONTOLOGY_FILE)

    se_tables     = {t: p for t, p in table_patterns.items() if p == "SE"}
    all_se_tables = set(se_tables.keys())
    print(f"  SE (concrete) : {len(se_tables)} tables")

    ontology_classes = ontology_explorer(mode="classes")["classes"]
    print(f"  Ontology classes: {len(ontology_classes)}")

    # Load constraint metadata from Phase 0 (for FK property hints)
    constraint_meta = {}
    if os.path.exists(CONSTRAINT_META_FILE):
        try:
            with open(CONSTRAINT_META_FILE, "r", encoding="utf-8") as f:
                constraint_meta = json.load(f)
            print(f"  Constraint metadata: {len(constraint_meta)} tables")
        except Exception:
            print(f"  [WARN] Could not load constraint_metadata.json")

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

    # Track classes already assigned so no two tables get the same class
    used_classes: Set[str] = {
        m["ontology_mapping"]["ontology_class"]
        for m in mappings_process.values()
        if m.get("ontology_mapping", {}).get("ontology_class")
    }
    if used_classes:
        print(f"  Pre-existing used classes (from cache): {sorted(used_classes)}")

    for idx, (table_name, pattern) in enumerate(se_tables.items(), 1):
        print(f"\n[{idx:>2}/{total}] {table_name}  [{pattern}]")

        # ── Phase A: LLM table → ontology class (cached) ──────────────────────
        if table_name not in mappings_process:
            try:
                record = mapper.map_table(
                    table_name, pattern,
                    tables_structure, understanding, enrichment,
                    ontology_classes, used_classes=used_classes
                )
                # Track newly assigned class
                assigned = record.get("ontology_mapping", {}).get("ontology_class")
                if assigned and assigned in set(ontology_classes):
                    used_classes.add(assigned)
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
            print(f"  → already mapped (cached), skipping LLM class call")

        record     = mappings_process[table_name]
        ont_class  = record.get("ontology_mapping", {}).get("ontology_class")
        attributes = record.get("attributes", [])

        # col_meanings / table_meaning may be absent in older cache records
        col_meanings: Dict[str, str] = (
            record.get("column_meanings")
            or understanding.get(table_name, {}).get("columns", {})
        )
        table_meaning: str = (
            record.get("table_meaning")
            or understanding.get(table_name, {}).get("table_meaning", "Not available")
        )

        # ── Phase B: column → predicate mapping ───────────────────────────────
        if ont_class:
            se_mappings[table_name] = build_se_json_mapping(
                table_name     = table_name,
                ontology_class = ont_class,
                attributes     = attributes,
                base_iri       = base_iri,
                all_se_tables  = all_se_tables,
                mapper         = mapper,
                col_meanings   = col_meanings,
                table_meaning  = table_meaning,
                constraint_meta = constraint_meta,
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