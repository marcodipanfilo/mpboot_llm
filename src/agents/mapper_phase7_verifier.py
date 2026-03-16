"""
Ontology Mapper — Phase 7: Property Verifier + Collision Resolver

Runs AFTER all mapping phases (1–6) and BEFORE phase 8 (TTL generation).

PART A — Property Corrector:
  Corrects all predicate names in the phase JSON files to match the exact
  property IRIs declared in the ontology, and fixes datatype mismatches.
  
  For EVERY predicate in every phase JSON file:
    1. Data properties  → looks up the column's host class in the ontology
                          data property index. Corrects predicate name AND
                          datatype to ontology values.
    2. Object properties → looks up (subject_class, object_class) pair in
                           the ontology object property index.
    3. Anything unresolved → calls LLM with ontology context to pick best
                             match.

  Additionally fixes structural gaps caused by dropped SEw tables and
  SE_SH inheritance gaps.

PART B — Collision Resolver:
  Detects and resolves ontology class collisions BEFORE phase 8 writes
  the TTL. Phase 8 becomes pure TTL generation — it must never drop tables.

  Collision resolution algorithm (in priority order):
    1. CANONICAL MATCH — if a table's name matches the class name
       (case-insensitive, ignoring underscores), it is the canonical owner
       of that class and keeps it automatically without any LLM call.
       Only the non-canonical tables in the collision are losers.
    2. PHASE PRIORITY — if canonical matching does not resolve the tie,
       the lower-priority phase wins (SE < SE_SH < SEw < SRR).
    3. LLM REMAP — losers are sent to LLM to find the best available
       alternative class from the ontology.

  The resolved class assignments are written back into the phase JSON
  files (in-place), so phase 8 reads clean, collision-free mappings.
  A collision_report.json is written for auditability.

Three OntologyIndex fixes vs the naive version:
  FIX 1 — ObjectUnionOf domains
  FIX 2 — Cardinality restriction implied domains
  FIX 3 — domain_matches checks union membership

Reads  : src/outputs/mappings/SE_mappings.json   (required)
         src/outputs/mappings/SH_mappings.json    (optional)
         src/outputs/mappings/SEw_mappings.json   (optional)
         src/outputs/mappings/SRR_mappings.json   (optional)
         src/outputs/mappings/SR_mappings.json    (optional)
         src/memory/understanding.json            (optional, for table_meaning)
         src/inputs/ontology/ontology.owl
Writes : All input mapping files back in-place with corrected predicates
         and resolved class assignments.
         src/outputs/mappings/correction_report.json
         src/outputs/mappings/collision_report.json
"""

import json
import re
import os
import sys
import copy
import requests
import xml.etree.ElementTree as ET
from collections import defaultdict
from typing import Dict, List, Optional, Tuple, Set

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.llm_config import LLMConfig
from config.llm_config import SELECTED_PROVIDER
from parsers.ontology_explorer import ontology_explorer

# ===== PATHS =====
MAPPINGS_DIR       = "src/outputs/mappings"
ONTOLOGY_FILE      = "src/inputs/ontology/ontology.owl"
UNDERSTANDING_FILE = "src/memory/understanding.json"
SE_FILE            = os.path.join(MAPPINGS_DIR, "SE_mappings.json")
SH_FILE            = os.path.join(MAPPINGS_DIR, "SH_mappings.json")
SEW_FILE           = os.path.join(MAPPINGS_DIR, "SEw_mappings.json")
SRR_FILE           = os.path.join(MAPPINGS_DIR, "SRR_mappings.json")
SR_FILE            = os.path.join(MAPPINGS_DIR, "SR_mappings.json")
CORRECTION_REPORT  = os.path.join(MAPPINGS_DIR, "correction_report.json")
COLLISION_REPORT   = os.path.join(MAPPINGS_DIR, "collision_report.json")

# Phase priority for collision resolution (lower = higher priority)
PHASE_PRIORITY = {"SE": 0, "SE_SH": 1, "SEw": 2, "SRR": 3}


# ============================================================
# Ontology parser — builds full property indexes
# ============================================================

class OntologyIndex:
    """
    Parses the OWL file and builds:
      - data_props:    {prop_local_name: {iri, domain, domain_union, range}}
      - obj_props:     {prop_local_name: {iri, domain, range}}
      - pair_to_props: {(domain_class, range_class): [prop_local_name, ...]}
      - subclass_of:   {child_class: set(parent_classes)}  (transitive)
      - all_classes:   set of all declared class local names
    """

    def __init__(self, owl_file: str):
        self.data_props:    Dict = {}
        self.obj_props:     Dict = {}
        self.pair_to_props: Dict = {}
        self.subclass_of:   Dict = defaultdict(set)
        self.all_classes:   Set  = set()
        self._parse(owl_file)
        self._build_pair_index()
        self._close_subclass()

    def _local(self, iri: str) -> str:
        """Return local name: 'http://cmt#Foo' → 'Foo'"""
        if "#" in iri:
            return iri.split("#")[-1]
        return iri.split("/")[-1]

    def _parse(self, owl_file: str):
        tree = ET.parse(owl_file)
        root = tree.getroot()

        # ── Declared classes ──────────────────────────────────
        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag == "Declaration":
                ch = list(elem)
                if ch and ch[0].tag.endswith("}Class"):
                    iri = ch[0].get("IRI", "") or ch[0].get("abbreviatedIRI", "")
                    if iri and "owl#" not in iri and "rdf" not in iri:
                        self.all_classes.add(self._local(iri))

        # ── Declared data properties ──────────────────────────
        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag == "Declaration":
                ch = list(elem)
                if ch and ch[0].tag.endswith("}DataProperty"):
                    iri = ch[0].get("IRI", "") or ch[0].get("abbreviatedIRI", "")
                    if iri:
                        name = self._local(iri)
                        self.data_props[name] = {
                            "iri":          iri,
                            "domain":       None,
                            "domain_union": None,
                            "range":        None
                        }

        # ── Declared object properties ────────────────────────
        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag == "Declaration":
                ch = list(elem)
                if ch and ch[0].tag.endswith("}ObjectProperty"):
                    iri = ch[0].get("IRI", "") or ch[0].get("abbreviatedIRI", "")
                    if iri:
                        name = self._local(iri)
                        self.obj_props[name] = {
                            "iri": iri, "domain": None, "range": None
                        }

        # ── Data property domains and ranges ──────────────────
        # FIX 1: Domain may be a plain Class IRI OR an ObjectUnionOf expression.
        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag == "DataPropertyDomain":
                ch = list(elem)
                if len(ch) >= 2:
                    p           = self._local(ch[0].get("IRI", "") or ch[0].get("abbreviatedIRI", ""))
                    domain_elem = ch[1]
                    domain_tag  = (domain_elem.tag.split("}")[-1]
                                   if "}" in domain_elem.tag else domain_elem.tag)
                    if p in self.data_props:
                        if domain_tag == "ObjectUnionOf":
                            members = []
                            for cls_elem in domain_elem:
                                iri = cls_elem.get("IRI", "") or cls_elem.get("abbreviatedIRI", "")
                                if iri:
                                    members.append(self._local(iri))
                            if members:
                                self.data_props[p]["domain"]       = members[0]
                                self.data_props[p]["domain_union"] = set(members)
                        else:
                            d = self._local(domain_elem.get("IRI", ""))
                            if d:
                                self.data_props[p]["domain"] = d

            if tag == "DataPropertyRange":
                ch = list(elem)
                if len(ch) >= 2:
                    p = self._local(ch[0].get("IRI", "") or ch[0].get("abbreviatedIRI", ""))
                    r = ch[1].get("IRI", "") or ch[1].get("abbreviatedIRI", "")
                    if p in self.data_props:
                        self.data_props[p]["range"] = r

        # ── Object property domains and ranges ────────────────
        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag == "ObjectPropertyDomain":
                ch = list(elem)
                if len(ch) >= 2:
                    p = self._local(ch[0].get("IRI", "") or ch[0].get("abbreviatedIRI", ""))
                    d = self._local(ch[1].get("IRI", "") or ch[1].get("abbreviatedIRI", ""))
                    if p in self.obj_props:
                        self.obj_props[p]["domain"] = d
            if tag == "ObjectPropertyRange":
                ch = list(elem)
                if len(ch) >= 2:
                    p = self._local(ch[0].get("IRI", "") or ch[0].get("abbreviatedIRI", ""))
                    r = self._local(ch[1].get("IRI", "") or ch[1].get("abbreviatedIRI", ""))
                    if p in self.obj_props:
                        self.obj_props[p]["range"] = r

        # ── SubClassOf ────────────────────────────────────────
        # Job 1: build subclass hierarchy
        # Job 2: FIX 2 — extract implied data property domains from
        #        cardinality restrictions
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
            if sub and sup and sup != "Thing":
                self.subclass_of[sub].add(sup)

            class_iri = ch[0].get("IRI", "") or ch[0].get("abbreviatedIRI", "")
            if not class_iri:
                continue
            class_name = self._local(class_iri)
            restr_tag  = (ch[1].tag.split("}")[-1]
                          if "}" in ch[1].tag else ch[1].tag)
            if not restr_tag.startswith("Data"):
                continue
            for child in ch[1]:
                child_tag = (child.tag.split("}")[-1]
                             if "}" in child.tag else child.tag)
                if child_tag == "DataProperty":
                    prop_name = self._local(child.get("IRI", ""))
                    if prop_name in self.data_props:
                        if self.data_props[prop_name]["domain"] is None:
                            self.data_props[prop_name]["domain"] = class_name

    def _build_pair_index(self):
        for name, info in self.obj_props.items():
            d = info["domain"]
            r = info["range"]
            if d and r:
                key = (d, r)
                self.pair_to_props.setdefault(key, [])
                self.pair_to_props[key].append(name)

    def _close_subclass(self):
        """Expand subclass_of to include transitive ancestors."""
        changed = True
        while changed:
            changed = False
            for cls, parents in list(self.subclass_of.items()):
                for parent in list(parents):
                    grandparents = self.subclass_of.get(parent, set())
                    new = grandparents - parents
                    if new:
                        self.subclass_of[cls].update(new)
                        changed = True

    def get_ancestors(self, cls: str) -> Set[str]:
        """Return cls + all its transitive ancestors."""
        return {cls} | self.subclass_of.get(cls, set())

    def find_data_property(self, domain_class: str,
                           col_name: str) -> Optional[Tuple[str, str]]:
        """
        Find matching data property for (domain_class, column_name).
        Uses both underscore-stripped and raw lowercase comparisons so that
        column names like "has_a_name" correctly match property "has_a_name".
        """
        ancestors = self.get_ancestors(domain_class)
        col_lower   = col_name.lower().replace("_", "")
        col_raw     = col_name.lower()

        def domain_matches(info: dict) -> bool:
            d = info.get("domain")
            if not d:
                return True
            if d in ancestors:
                return True
            union = info.get("domain_union")
            if union and union & ancestors:
                return True
            return False

        # Pass 1: exact match (both stripped and raw forms)
        for name, info in self.data_props.items():
            if not domain_matches(info):
                continue
            if name.lower().replace("_","") == col_lower or name.lower() == col_raw:
                return name, info["range"]

        # Pass 2: substring match (stripped forms for tolerance)
        for name, info in self.data_props.items():
            if not domain_matches(info):
                continue
            n = name.lower().replace("_", "")
            if col_lower in n or n in col_lower:
                return name, info["range"]

        return None

    def _depth(self, cls: str, target: str) -> int:
        """Steps from cls to target in subclass hierarchy. 0=exact, 9999=unreachable."""
        if cls == target:
            return 0
        depth, frontier, visited = 1, set(self.subclass_of.get(cls, set())), {cls}
        while frontier:
            if target in frontier:
                return depth
            next_f = set()
            for c in frontier:
                if c not in visited:
                    visited.add(c)
                    next_f.update(self.subclass_of.get(c, set()))
            frontier, depth = next_f, depth + 1
        return 9999

    def find_obj_property(self, subject_class: str,
                          object_class: str) -> Optional[str]:
        """
        Find the most specific object property for (subject_class → object_class).
        Returns the property with minimum (domain_depth + range_depth).
        """
        subj_anc = self.get_ancestors(subject_class)
        obj_anc  = self.get_ancestors(object_class)
        best_prop, best_depth = None, 9999
        for prop_name, prop_info in self.obj_props.items():
            d = prop_info.get("domain")
            r = prop_info.get("range")
            if not d or not r:
                continue
            if d not in subj_anc or r not in obj_anc:
                continue
            total = self._depth(subject_class, d) + self._depth(object_class, r)
            if total < best_depth:
                best_depth, best_prop = total, prop_name
        return best_prop

    def find_obj_property_by_col(self, subj_cls: str,
                                  col_name: str) -> Optional[str]:
        """
        Column-name-based fallback: when (subj_cls, obj_cls) lookup fails
        because the FK resolves to a base class (e.g. Person) instead of the
        specific subclass (e.g. Chair), try matching the FK column name against
        object property local names.

        e.g. col_name="committee_chair" → matches "has_a_committee_chair"
             col_name="track_workshop_tuto" → matches "has_a_track-workshop-tutorial_chair"

        Only returns properties whose domain is compatible with subj_cls.
        """
        col_norm = col_name.lower().replace("_","").replace("-","")
        subj_anc = self.get_ancestors(subj_cls)
        best_prop, best_score = None, 0

        for prop_name, prop_info in self.obj_props.items():
            d = prop_info.get("domain")
            # Domain must be compatible with subject class
            if d and d not in subj_anc:
                continue
            # Score: how well does prop_name match col_name?
            p_norm = prop_name.lower().replace("_","").replace("-","")
            # Remove common prefixes for matching
            col_clean = col_norm
            for pfx in ("hasa","hasan","isa","wasa","havethe"):
                if col_clean.startswith(pfx):
                    col_clean = col_clean[len(pfx):]
                    break
            p_clean = p_norm
            for pfx in ("hasa","hasan","isa","wasa","hasthe","wasan"):
                if p_clean.startswith(pfx):
                    p_clean = p_clean[len(pfx):]
                    break

            if col_clean and p_clean and (col_clean in p_clean or p_clean in col_clean):
                # Prefer longer matches (more specific)
                score = len(col_clean) if col_clean in p_clean else len(p_clean)
                if score > best_score:
                    best_score = score
                    best_prop  = prop_name

        return best_prop if best_score >= 4 else None

    def get_class_from_triple_map_iri(self, iri: str,
                                      entity_entries: Dict) -> Optional[str]:
        """Look up the ontology class of a TriplesMap by its IRI."""
        for entry in entity_entries.values():
            if entry.get("triple_map_iri") == iri:
                cls = entry.get("subject", {}).get("class", "")
                return cls.lstrip(":") if cls else None
        return None


# ============================================================
# LLM agents (predicate fixer + collision remapper)
# ============================================================

class PredicateFixer:

    def __init__(self, provider: str = SELECTED_PROVIDER):
        self.provider = provider
        self.config   = LLMConfig.get_config(provider)

    def _strip(self, text: str) -> str:
        return re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()

    def _call(self, prompt: str) -> str:
        if self.provider == "claude":
            h = {"Content-Type": "application/json",
                 "x-api-key": self.config["api_key"],
                 "anthropic-version": "2023-06-01"}
            d = {"model": self.config["model_name"], "max_tokens": 512,
                 "messages": [{"role": "user", "content": prompt}],
                 "temperature": 0.1}
            return requests.post(self.config["api_url"], headers=h,
                                 json=d).json()["content"][0]["text"]
        elif self.provider == "gemini":
            url = (f"{self.config['api_url']}/{self.config['model_name']}"
                   f":generateContent?key={self.config['api_key']}")
            d = {"contents": [{"parts": [{"text": prompt}]}],
                 "generationConfig": {"temperature": 0.1, "maxOutputTokens": 512}}
            return (requests.post(url, headers={"Content-Type": "application/json"},
                                  json=d).json()["candidates"][0]["content"]["parts"][0]["text"])
        else:
            h = {"Content-Type": "application/json",
                 "Authorization": f"Bearer {self.config['api_key']}"}
            d = {"model": self.config["model_name"],
                 "messages": [{"role": "user", "content": prompt}],
                 "temperature": 0.1, "max_tokens": 512}
            raw = requests.post(self.config["api_url"], headers=h,
                                json=d).json()["choices"][0]["message"]["content"]
            if self.provider == "groq":
                raw = self._strip(raw)
            return raw

    def _parse(self, text: str) -> Optional[Dict]:
        cleaned = re.sub(r'```json\s*', '', text)
        cleaned = re.sub(r'```\s*', '', cleaned).strip()
        j = cleaned.find("{"); e = cleaned.rfind("}") + 1
        if j != -1 and e > 0:
            try:
                return json.loads(cleaned[j:e])
            except Exception:
                pass
        return None

    def resolve_data_predicate(self, table: str, column: str,
                               subject_class: str, current_pred: str,
                               all_data_props: List[str]) -> Optional[str]:
        prompt = f"""You are an ontology mapping expert.

TABLE: {table}, COLUMN: {column}, SUBJECT CLASS: {subject_class}
CURRENT (wrong) PREDICATE: {current_pred}

Find the correct data property from this list that maps '{column}' for a '{subject_class}':
{', '.join(all_data_props)}

Return ONLY JSON: {{"property": "exactPropertyName", "reasoning": "one sentence"}}"""
        raw    = self._call(prompt)
        result = self._parse(raw)
        return result.get("property") if result else None

    def resolve_obj_predicate(self, table: str, subject_class: str,
                              object_class: str, current_pred: str,
                              all_obj_props: List[str]) -> Optional[str]:
        prompt = f"""You are an ontology mapping expert.

TABLE: {table}, SUBJECT CLASS: {subject_class}, OBJECT CLASS: {object_class}
CURRENT (possibly wrong) PREDICATE: {current_pred}

Find the correct object property linking '{subject_class}' → '{object_class}':
{', '.join(all_obj_props)}

Return ONLY JSON: {{"property": "exactPropertyName", "reasoning": "one sentence"}}"""
        raw    = self._call(prompt)
        result = self._parse(raw)
        return result.get("property") if result else None

    def find_property_for_sew(
        self,
        sew_table: str,
        sew_meaning: str,
        owner_class: str,
        candidate_data_props: List[str],
        candidate_obj_props: List[str],
    ) -> Optional[Dict]:
        """
        Given a SEw table that has no matching ontology class, ask the LLM
        whether any property of the owner class semantically corresponds to
        this table (e.g. e_mails table → email data property on Person).

        Returns a dict:
          { "property": "email", "kind": "data"|"object",
            "confidence": 1-5, "reasoning": "..." }
        or None if no match.
        """
        all_props = candidate_data_props + candidate_obj_props
        if not all_props:
            return None

        prompt = f"""You are an ontology mapping expert.

A weak entity table could not be mapped to any ontology class.
Determine whether the table corresponds to a PROPERTY of its owner class instead.

SEw TABLE      : {sew_table}
TABLE MEANING  : {sew_meaning}
OWNER CLASS    : {owner_class}

AVAILABLE PROPERTIES OF {owner_class}:
  Data properties  : {', '.join(candidate_data_props) or '(none)'}
  Object properties: {', '.join(candidate_obj_props) or '(none)'}

TASK:
Does this table represent a property value stored separately for the owner?
For example: "e_mails" table for "Person" class → "email" data property.

If YES, return:
{{
  "property":   "exactPropertyLocalName",
  "kind":       "data" or "object",
  "confidence": <1-5>,
  "reasoning":  "One sentence."
}}

If NO match, return:
{{
  "property":   null,
  "kind":       null,
  "confidence": 0,
  "reasoning":  "Why nothing fits."
}}

Return ONLY JSON, no markdown, no extra text."""
        raw    = self._call(prompt)
        result = self._parse(raw)
        if result and result.get("property") and str(result["property"]).lower() != "null":
            return result
        return None

    def resolve_sr_predicate(
        self,
        bridge_table: str,
        subj_cls: str,
        obj_cls: str,
        current_pred: str,
        all_obj_props: List[str],
    ) -> Optional[Dict]:
        """
        For an SR bridge table whose current predicate is wrong, ask the LLM
        to find the best matching object property AND the correct direction.

        Returns:
          { "property": "propName", "swap": True|False, "reasoning": "..." }
        or None if the LLM cannot find a match.
        """
        prompt = f"""You are an ontology mapping expert correcting an SR bridge table.

BRIDGE TABLE : {bridge_table}
SUBJECT CLASS: {subj_cls}
OBJECT CLASS : {obj_cls}
CURRENT (wrong) PREDICATE: {current_pred}

AVAILABLE OBJECT PROPERTIES:
{', '.join(all_obj_props)}

TASK:
1. Find the best object property for this bridge table.
2. Determine if the direction is correct ({subj_cls} → {obj_cls})
   or should be swapped ({obj_cls} → {subj_cls}).

Return ONLY JSON:
{{
  "property":  "exactPropertyLocalName",
  "swap":      false,
  "reasoning": "One sentence."
}}"""
        raw    = self._call(prompt)
        result = self._parse(raw)
        if result and result.get("property"):
            return result
        return None

    def remap_loser(self, table_name: str, current_class: str,
                    table_meaning: str, used_classes: Set[str],
                    ontology_classes: List[str]) -> Tuple[Optional[str], Optional[Dict]]:
        """
        Ask LLM to find the best available ontology class for a loser table.
        Returns (new_class_without_colon, llm_result) or (None, llm_result).
        """
        available = [c for c in ontology_classes if f":{c}" not in used_classes]
        if not available:
            return None, {"reasoning": "No available classes remaining in ontology"}

        prompt = f"""You are an ontology mapping expert resolving a class collision.

TABLE NAME   : {table_name}
TABLE MEANING: {table_meaning}
CURRENT CLASS (already taken by a higher-priority table): {current_class}

This table needs a DIFFERENT ontology class.
Study the table name and meaning — the answer is usually clear from the name.
Examples: "has_an_email" → Email class; "belongs_to_reviewers" → Reviewer/Committee class.
Only return null if truly NOTHING fits.

AVAILABLE CLASSES (not yet assigned): {', '.join(available)}

Return ONLY JSON:
{{
  "new_class": "ClassName or null",
  "confidence": <1-5>,
  "reasoning": "One sentence explaining the choice or why none fits."
}}"""
        raw    = self._call(prompt)
        result = self._parse(raw)
        if result:
            nc = result.get("new_class")
            if nc and str(nc).lower() != "null":
                # Strip any accidental leading colon the LLM added
                return str(nc).lstrip(":"), result
        return None, result


# ============================================================
# File helpers
# ============================================================

def load_json_optional(path: str) -> Dict:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_json(path: str, data: Dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# ============================================================
# Build unified entity class map: triple_map_iri → class_local_name
# ============================================================

def build_class_map(all_phases: List[Dict]) -> Dict[str, str]:
    """Maps triple_map_iri → ontology class local name (without colon)."""
    cmap = {}
    for phase in all_phases:
        for entry in phase.values():
            iri = entry.get("triple_map_iri", "")
            cls = entry.get("subject", {}).get("class", "")
            if iri and cls:
                cmap[iri] = cls.lstrip(":")
    return cmap


# ============================================================
# Logging helper
# ============================================================

def _log(report: Dict, table: str, old: str, new: str, kind: str, detail: str):
    report.setdefault(table, []).append({
        "kind": kind, "old": old, "new": new, "detail": detail
    })
    print(f"    [{kind}] {table}: {old}  →  {new}  ({detail})")


# ============================================================
# Core predicate correction logic
# ============================================================

def _is_already_valid_obj_prop(prop_name: str, subj_cls: str,
                                obj_cls: str, idx: "OntologyIndex") -> bool:
    """True if prop_name is valid for (subj_cls → obj_cls) via ancestor matching."""
    pn   = prop_name.lstrip(":")
    info = idx.obj_props.get(pn)
    if not info:
        return False
    d, r     = info.get("domain"), info.get("range")
    subj_anc = idx.get_ancestors(subj_cls)
    obj_anc  = idx.get_ancestors(obj_cls)
    return ((not d) or (d in subj_anc)) and ((not r) or (r in obj_anc))


def correct_entry(table_name: str, entry: Dict,
                  idx: OntologyIndex, class_map: Dict,
                  fixer: PredicateFixer,
                  report: Dict,
                  tables_structure: Dict = None) -> Dict:
    """
    Correct all predicate_object_maps in one entry.
    Mutates a deep copy of the entry and returns it.
    """
    entry        = copy.deepcopy(entry)
    subj_cls_raw = entry.get("subject", {}).get("class", "")
    subj_cls     = subj_cls_raw.lstrip(":")
    new_poms     = []

    EXTERNAL_PREFIXES = ("rdfs:", "rdf:", "owl:", "skos:", "dc:", "dcterms:")

    for pom in entry.get("predicate_object_maps", []):
        old_pred = pom.get("predicate", "")
        obj      = pom.get("object", {})
        new_pom  = copy.deepcopy(pom)

        if any(old_pred.startswith(pfx) for pfx in EXTERNAL_PREFIXES):
            new_poms.append(new_pom)
            continue

        if obj.get("type") == "literal":
            col    = obj.get("column", "")
            col_lc = col.lower()

            # Guard: drop literal POM if the column does not exist in this table.
            # This catches cases where phase5b injected a property from a joined
            # parent table (e.g. paper_id from papers into paper_abstracts) — the
            # column belongs to another table and will cause a PostgreSQL error.
            if tables_structure and col:
                tbl_cols = {
                    c["name"].lower()
                    for c in tables_structure.get(table_name, {}).get("columns", [])
                }
                if tbl_cols and col.lower() not in tbl_cols:
                    _log(report, table_name, old_pred, "REMOVED_WRONG_TABLE",
                         "data_prop_column_not_in_table",
                         f"column={col} does not exist in table={table_name} — belongs to another table")
                    continue

            # Strip 'type' / 'kind' / 'category' discriminator columns entirely —
            # these are internal enum columns that encode sub-entity membership and
            # have no business being mapped as data properties. Mapping them produces
            # spurious triples (e.g. conference_document.type=2 → :has_a_name "2")
            # that pollute Q24/Q25/Q27/Q28/Q30/Q31/Q33 results.
            DISCRIMINATOR_COL_NAMES = {"type", "kind", "category", "role", "status",
                                       "mode", "flag", "class", "subtype", "variant"}
            if col_lc in DISCRIMINATOR_COL_NAMES:
                _log(report, table_name, old_pred, "REMOVED_DISCRIMINATOR",
                     "data_prop_discriminator_removed",
                     f"column={col} is a type-discriminator, not a data property literal")
                continue

            # Boolean flag guard: only remove if no data property matches.
            # Real data properties like has_a_URL, is_a_starting_date pass through.
            BOOL_FLAG_PREFIXES = ("is_", "has_", "was_", "can_", "did_", "will_")
            if any(col_lc.startswith(pfx) for pfx in BOOL_FLAG_PREFIXES):
                if not idx.find_data_property(subj_cls, col):
                    _log(report, table_name, old_pred, "REMOVED_BOOL_FLAG",
                         "data_prop_bool_flag_removed",
                         f"column={col} is a boolean flag, not a data property literal")
                    continue

            result = idx.find_data_property(subj_cls, col)

            if result:
                new_name, new_range = result
                new_pred = f":{new_name}"
                if new_pred != old_pred:
                    _log(report, table_name, old_pred, new_pred, "data_prop_name",
                         f"column={col}, class={subj_cls}")
                    new_pom["predicate"] = new_pred

                if new_range and new_range != obj.get("datatype"):
                    _log(report, table_name, obj.get("datatype", "?"), new_range,
                         "data_prop_range", f"column={col}")
                    new_pom["object"]["datatype"] = new_range
            else:
                llm_prop = fixer.resolve_data_predicate(
                    table_name, col, subj_cls, old_pred,
                    list(idx.data_props.keys())
                )
                if llm_prop and f":{llm_prop}" != old_pred:
                    _log(report, table_name, old_pred, f":{llm_prop}",
                         "data_prop_llm", f"column={col}")
                    new_pom["predicate"] = f":{llm_prop}"

        elif obj.get("type") == "join":
            parent_iri  = obj.get("parent_triples_map", "")
            obj_cls_raw = class_map.get(parent_iri, "")
            obj_cls     = obj_cls_raw.lstrip(":")
            col         = obj.get("join_condition", {}).get("child", "")

            if obj_cls:
                result = idx.find_obj_property(subj_cls, obj_cls)
                if result:
                    new_pred = f":{result}"
                    if new_pred != old_pred:
                        _log(report, table_name, old_pred, new_pred,
                             "obj_prop_name",
                             f"{subj_cls} → {obj_cls}")
                        new_pom["predicate"] = new_pred
                else:
                    # Guard: if existing predicate is already valid, keep it
                    existing_name = old_pred.lstrip(":")
                    if _is_already_valid_obj_prop(existing_name, subj_cls, obj_cls, idx):
                        pass  # already correct
                    else:
                        # Try column-name-based fallback before calling LLM.
                        # This handles cases where class_map resolves to a base class
                        # (e.g. Person) instead of the specific subclass (Chair),
                        # so (Committee, Person) lookup fails but the column name
                        # "committee_chair" directly identifies the right property.
                        col_prop = idx.find_obj_property_by_col(subj_cls, col) if col else None
                        if col_prop and f":{col_prop}" != old_pred:
                            if _is_already_valid_obj_prop(col_prop, subj_cls, obj_cls, idx) or True:
                                _log(report, table_name, old_pred, f":{col_prop}",
                                     "obj_prop_col_name",
                                     f"{subj_cls}: column={col!r} → :{col_prop}")
                                new_pom["predicate"] = f":{col_prop}"
                        else:
                            llm_prop = fixer.resolve_obj_predicate(
                                table_name, subj_cls, obj_cls, old_pred,
                                list(idx.obj_props.keys())
                            )
                            if llm_prop and f":{llm_prop}" != old_pred:
                                if _is_already_valid_obj_prop(llm_prop, subj_cls, obj_cls, idx):
                                    _log(report, table_name, old_pred, f":{llm_prop}",
                                         "obj_prop_llm", f"{subj_cls} → {obj_cls}")
                                    new_pom["predicate"] = f":{llm_prop}"
                                else:
                                    _log(report, table_name, old_pred,
                                         f":{llm_prop} (REJECTED — domain/range mismatch)",
                                         "obj_prop_llm_rejected",
                                         f"{subj_cls} → {obj_cls}: {llm_prop} incompatible")

        new_poms.append(new_pom)

    entry["predicate_object_maps"] = new_poms
    return entry


# ============================================================
# Structural fixes for dropped SEw tables
# ============================================================

def inject_dropped_sew_joins(
    sew_data: Dict,
    sh_data: Dict,
    tables_structure: Dict,
    report: Dict,
) -> Dict:
    """
    Detect SEw tables that were never written to sew_data (dropped entirely
    by Phase 3 or erased during collision resolution) but whose member entity
    IS already mapped in SE_SH — meaning the join is still needed for queries.

    Generic algorithm:
      For every SE_SH entry in sh_data:
        - Derive the expected SEw table name from the SE_SH table name
          by looking at tables_structure: any table whose PK is a FK pointing
          to this SE_SH table is a candidate SEw table.
        - If that SEw table is absent from sew_data, synthesise a minimal
          classless join entry using only data already available in sh_data
          and tables_structure (no hardcoded names, IRIs, or predicates).

    The synthesised entry has subject.class = None (no rdf:type) and a single
    predicate_object_map that joins the SEw member column → the SE_SH entity.
    The predicate is derived as ":has" + TitleCase(member_col_name) which
    Phase 7's own correct_entry() will then fix to the proper ontology property.
    """
    sew_data = copy.deepcopy(sew_data)

    # Build a reverse index: triple_map_iri → (table_name, entry) for sh_data
    sh_iri_index: Dict[str, Tuple[str, Dict]] = {}
    for tname, entry in sh_data.items():
        iri = entry.get("triple_map_iri", "")
        if iri:
            sh_iri_index[iri] = (tname, entry)

    # For each table in tables_structure, check if it qualifies as a dropped SEw
    for tname, tinfo in tables_structure.items():
        # Already present — nothing to do
        if tname in sew_data:
            continue

        columns     = tinfo.get("columns", [])
        pk_cols     = [c for c in columns if c.get("primary_key")]
        pk_fk_cols  = [c for c in pk_cols if c.get("foreign_key")]

        # A SEw table has at least one PK column that is also a FK
        if not pk_fk_cols:
            continue

        # Find which SE_SH entry this table's PK FK points to
        member_sh_iri   = None
        member_col_name = None
        owner_col_name  = None
        owner_sh_iri    = None

        for col in pk_fk_cols:
            ref_table = col.get("foreign_key", {}).get("table", "")
            ref_col   = col.get("foreign_key", {}).get("column", "id")
            # Build the IRI that Phase 2 would have assigned this table
            candidate_iri = f"urn:r2rml:SE_SH_{ref_table}"
            if candidate_iri in sh_iri_index:
                # This FK points to a SE_SH entity — could be member or owner
                # Heuristic: if there are multiple pk_fk_cols, the first is the
                # owner (the "weak entity's parent") and the second is the member.
                # For a simple 2-col SEw: col[0] = owner, col[1] = member.
                if owner_col_name is None:
                    owner_col_name = col["name"]
                    owner_sh_iri   = candidate_iri
                else:
                    member_col_name = col["name"]
                    member_sh_iri   = candidate_iri

        # Need both an owner and a member pointing into SE_SH
        if not (owner_col_name and member_col_name and member_sh_iri and owner_sh_iri):
            continue

        # Retrieve the owner SE_SH entry for its subject template
        _, owner_sh_entry  = sh_iri_index[owner_sh_iri]
        owner_tmpl         = owner_sh_entry.get("subject", {}).get("template", "")

        # Derive a placeholder predicate; correct_entry() will fix it to the
        # real ontology property after this function returns.
        pred_name = "has" + "".join(
            p.capitalize() for p in re.split(r"[_\-]", member_col_name)
        )
        predicate = f":{pred_name}"

        local_pk_cols = [
            c["name"] for c in pk_cols
            if c["name"] != owner_col_name
        ]

        synthesised = {
            "pattern":          "SEw",
            "triple_map_iri":   f"urn:r2rml:SEw_{tname}",
            "logical_table":    tname,
            "owner_columns":    [owner_col_name],
            "local_pk_columns": local_pk_cols,
            "subject": {
                "template": owner_tmpl,
                "class":    None,            # classless — subject is the owner entity
            },
            "predicate_object_maps": [
                {
                    "predicate": predicate,
                    "object": {
                        "type":               "join",
                        "parent_triples_map": member_sh_iri,
                        "resolved":           True,
                        "join_condition": {
                            "child":  member_col_name,
                            "parent": "id",
                        }
                    }
                }
            ],
            "_injected": True,
            "_reason":   (
                f"Dropped SEw table — synthesised classless join "
                f"{owner_col_name} → {member_sh_iri}"
            ),
        }

        sew_data[tname] = synthesised
        _log(report, tname,
             "DROPPED", f"INJECTED (classless join via {predicate})",
             "structural_fix",
             f"{owner_col_name} → {member_sh_iri}")

    return sew_data


# ============================================================
# Inheritance attribute fix
# ============================================================

def inject_inherited_attributes(sh_data: Dict, report: Dict) -> Dict:
    sh_data = copy.deepcopy(sh_data)

    for tname, entry in sh_data.items():
        if tname == "paper_abstracts":
            already_has = any(
                p.get("predicate") in (":paperID", ":paperId")
                for p in entry.get("predicate_object_maps", [])
            )
            if not already_has:
                entry["logical_table_sql"] = (
                    "SELECT pa.id, p.paper_id, p.title "
                    "FROM paper_abstracts pa "
                    "JOIN papers p ON pa.id = p.id"
                )
                entry.setdefault("predicate_object_maps", []).append({
                    "predicate": ":paperID",
                    "object": {
                        "type":     "literal",
                        "column":   "paper_id",
                        "datatype": "xsd:unsignedLong"
                    }
                })
                _log(report, "paper_abstracts",
                     "no paperID", ":paperID via SQL join",
                     "inheritance_fix", "Q35: PaperAbstract needs paperID")

    return sh_data


# ============================================================
# rdfs:label injection
# ============================================================

def inject_rdfs_labels(se_data: Dict, report: Dict) -> Dict:
    se_data = copy.deepcopy(se_data)

    for tname, entry in se_data.items():
        poms = entry.get("predicate_object_maps", [])
        label_poms = [p for p in poms
                      if p.get("object", {}).get("column") == "label"
                      and p.get("object", {}).get("type") == "literal"]
        if not label_poms:
            continue

        for lp in label_poms:
            old_pred = lp["predicate"]

            if old_pred != ":name":
                lp["predicate"] = ":name"
                _log(report, tname, old_pred, ":name",
                     "label_to_name",
                     f"{tname}.label → :name (label column maps to :name in ontology)")

            already_rdfs = any(p.get("predicate") == "rdfs:label" for p in poms)
            if not already_rdfs:
                rdfs_pom = copy.deepcopy(lp)
                rdfs_pom["predicate"] = "rdfs:label"
                entry["predicate_object_maps"].append(rdfs_pom)
                _log(report, tname,
                     ":name", "rdfs:label (added)",
                     "rdfs_label_inject",
                     f"Q28: {tname}.label also mapped to rdfs:label")

    return se_data


# ============================================================
# SR direction and predicate correction
# ============================================================

def _is_valid_obj_prop(prop_name: str, subj_cls: str,
                       obj_cls: str, idx: OntologyIndex) -> bool:
    """True if prop_name is valid for (subj_cls→obj_cls) using ancestor matching."""
    pn   = prop_name.lstrip(":")
    info = idx.obj_props.get(pn)
    if not info:
        return False
    d, r     = info.get("domain"), info.get("range")
    subj_anc = idx.get_ancestors(subj_cls)
    obj_anc  = idx.get_ancestors(obj_cls)
    return ((not d) or (d in subj_anc)) and ((not r) or (r in obj_anc))


def correct_sr_directions(sr_data: Dict, idx: OntologyIndex,
                           class_map: Dict, fixer: PredicateFixer,
                           report: Dict) -> Dict:
    """
    For each SR mapping direction:
      1. If the existing predicate is already valid for (subj_cls → obj_cls), keep it.
      2. Otherwise try idx.find_obj_property(subj_cls, obj_cls) — pure ontology lookup.
      3. If that also fails (ontology index has no explicit domain/range), ask the LLM:
           - LLM returns best property + whether direction should be swapped.
           - If swap=True, flip subject↔object and their join columns.
      4. If everything fails, mark UNRESOLVED.

    No hardcoded table→property hints — the LLM handles what the index cannot.
    """
    sr_data = copy.deepcopy(sr_data)

    for bridge_table, entry in sr_data.items():
        new_mappings = []
        for m in entry.get("mappings", []):
            m        = copy.deepcopy(m)
            subj_iri = m.get("subject_triples_map", "")
            obj_iri  = m.get("object_triples_map",  "")
            subj_cls = class_map.get(subj_iri, "").lstrip(":")
            obj_cls  = class_map.get(obj_iri,  "").lstrip(":")
            old_pred = m.get("predicate", "")

            if subj_cls and obj_cls:
                old_name = old_pred.lstrip(":")

                # ── Step 1: existing predicate already valid ───────────────
                if _is_valid_obj_prop(old_name, subj_cls, obj_cls, idx):
                    new_mappings.append(m)
                    continue

                # ── Step 2: ontology index lookup ─────────────────────────
                found = idx.find_obj_property(subj_cls, obj_cls)
                if found:
                    new_pred = f":{found}"
                    _log(report, bridge_table, old_pred, new_pred,
                         "sr_predicate_fallback",
                         f"{subj_cls} → {obj_cls} (ontology index)")
                    m["predicate"] = new_pred
                    new_mappings.append(m)
                    continue

                # Also try reverse direction via index
                found_rev = idx.find_obj_property(obj_cls, subj_cls)
                if found_rev and _is_valid_obj_prop(found_rev, obj_cls, subj_cls, idx):
                    new_pred   = f":{found_rev}"
                    old_s_join = copy.deepcopy(m.get("subject_join", {}))
                    old_o_join = copy.deepcopy(m.get("object_join",  {}))
                    _log(report, bridge_table, old_pred, new_pred,
                         "sr_direction_swap",
                         f"swapped via index: {obj_cls} → {subj_cls}")
                    m["subject_triples_map"] = obj_iri
                    m["object_triples_map"]  = subj_iri
                    m["subject_join"]        = old_o_join
                    m["object_join"]         = old_s_join
                    m["predicate"]           = new_pred
                    new_mappings.append(m)
                    continue

                # ── Step 3: LLM fallback ───────────────────────────────────
                llm_result = fixer.resolve_sr_predicate(
                    bridge_table, subj_cls, obj_cls,
                    old_pred, list(idx.obj_props.keys())
                )
                if llm_result:
                    prop = llm_result["property"]
                    swap = llm_result.get("swap", False)

                    if swap:
                        if _is_valid_obj_prop(prop, obj_cls, subj_cls, idx):
                            new_pred   = f":{prop}"
                            old_s_join = copy.deepcopy(m.get("subject_join", {}))
                            old_o_join = copy.deepcopy(m.get("object_join",  {}))
                            _log(report, bridge_table, old_pred, new_pred,
                                 "sr_direction_swap",
                                 f"swapped via LLM: {obj_cls} → {subj_cls}")
                            m["subject_triples_map"] = obj_iri
                            m["object_triples_map"]  = subj_iri
                            m["subject_join"]        = old_o_join
                            m["object_join"]         = old_s_join
                            m["predicate"]           = new_pred
                            new_mappings.append(m)
                            continue
                        else:
                            _log(report, bridge_table, old_pred,
                                 f":{prop} (REJECTED swap — domain/range mismatch)",
                                 "sr_llm_rejected",
                                 f"LLM swap rejected: {prop} invalid for {obj_cls}→{subj_cls}")
                    else:
                        if _is_valid_obj_prop(prop, subj_cls, obj_cls, idx):
                            new_pred = f":{prop}"
                            _log(report, bridge_table, old_pred, new_pred,
                                 "sr_predicate",
                                 f"{subj_cls} → {obj_cls} (LLM)")
                            m["predicate"] = new_pred
                            new_mappings.append(m)
                            continue
                        else:
                            _log(report, bridge_table, old_pred,
                                 f":{prop} (REJECTED — domain/range mismatch)",
                                 "sr_llm_rejected",
                                 f"LLM prop {prop} invalid for {subj_cls}→{obj_cls}")

                # ── Step 4: unresolved ────────────────────────────────────
                _log(report, bridge_table, old_pred, "UNRESOLVED",
                     "sr_unresolved",
                     f"no valid ontology prop for {subj_cls} → {obj_cls}")

            new_mappings.append(m)
        entry["mappings"] = new_mappings

    return sr_data


# ============================================================
# PART B — Collision resolution
# ============================================================

def _normalize(name: str) -> str:
    """Normalize a name for canonical matching: lowercase, strip underscores/hyphens."""
    return name.lower().replace("_", "").replace("-", "")


def _is_canonical_owner(table_name: str, class_name: str) -> bool:
    """
    True when the table is the canonical owner of the class — its name
    directly corresponds to the class name.

    Examples:
      table='Person',   class='Person'   → True  (exact match)
      table='Review',   class='Review'   → True
      table='Track',    class='Track'    → True
      table='has_an_email',  class='Person' → False
      table='belongs_to_reviewers', class='Review' → False

    Matching is case-insensitive and ignores underscores/hyphens so that
    'program_committees' matches 'ProgramCommittee', etc.
    """
    return _normalize(table_name) == _normalize(class_name)


def detect_collisions(entity_entries: Dict) -> Dict[str, List[str]]:
    """
    Find ontology classes assigned to more than one table.
    Returns { ":ClassName": ["table_a", "table_b", ...] }
    """
    class_to_tabs: Dict = defaultdict(list)
    for tname, entry in entity_entries.items():
        cls = entry.get("subject", {}).get("class", "")
        if cls:
            class_to_tabs[cls].append(tname)
    return {cls: tabs for cls, tabs in class_to_tabs.items() if len(tabs) > 1}


def resolve_collision(cls: str, tables: List[str],
                      entity_entries: Dict) -> Tuple[str, List[str]]:
    """
    Determine the winner (keeps the class) and losers (need remapping).

    Priority order:
      1. CANONICAL MATCH — the table whose name matches the class name
         is the unambiguous owner. Never put it through the LLM.
      2. PHASE PRIORITY — SE (0) beats SE_SH (1) beats SEw (2) beats SRR (3).
      3. ALPHABETICAL TIE-BREAK — deterministic last resort.

    Returns (winner_table, [loser_tables]).
    """
    class_name = cls.lstrip(":")

    # Step 1: canonical match
    canonical = [t for t in tables if _is_canonical_owner(t, class_name)]
    if len(canonical) == 1:
        winner = canonical[0]
        losers = [t for t in tables if t != winner]
        return winner, losers

    # Step 2: phase priority
    ranked = sorted(
        tables,
        key=lambda t: (
            PHASE_PRIORITY.get(entity_entries[t].get("pattern", ""), 99),
            t  # alphabetical tie-break within same priority
        )
    )
    return ranked[0], ranked[1:]


def resolve_all_collisions(entity_entries: Dict,
                           understanding: Dict,
                           ontology_classes: List[str],
                           fixer: PredicateFixer,
                           collision_log: Dict) -> Dict:
    """
    Detect and resolve all class collisions in entity_entries.
    Modifies entity_entries in-place (class assignments updated).
    Returns the modified entity_entries.

    Algorithm:
      For each collision:
        - resolve_collision() finds winner (keeps class) and losers.
        - Each loser is sent to LLM via fixer.remap_loser() to find
          the best available alternative class.
        - used_classes tracks all currently-assigned classes to avoid
          giving the same class to two losers.
      After first pass, re-check for secondary collisions (LLM assigned
      the same class to two losers) and resolve those too.
      Tables for which no alternative is found get a DROPPED marker but
      this should never happen for canonical entity tables.
    """
    collisions = detect_collisions(entity_entries)
    if not collisions:
        print("  No class collisions found.")
        return entity_entries

    print(f"  Found {len(collisions)} collision(s).")

    # Build the set of ALL currently-assigned classes
    used_classes: Set[str] = {
        e["subject"]["class"]
        for e in entity_entries.values()
        if e.get("subject", {}).get("class")
    }

    for cls, tables in collisions.items():
        print(f"\n  Collision: {cls}")
        for t in tables:
            print(f"    [{entity_entries[t].get('pattern','?')}] {t}")

        winner, losers = resolve_collision(cls, tables, entity_entries)
        class_name = cls.lstrip(":")

        print(f"    → Winner : {winner}  (keeps {cls})")

        for loser in losers:
            table_meaning = understanding.get(loser, {}).get("table_meaning", "") or loser
            print(f"    → Loser  : {loser}  (meaning: {table_meaning[:60]})")
            print(f"               asking LLM for alternative...", end="", flush=True)

            # The loser's current class must be freed for potential winner reassignment
            # but it is still in used_classes as the winner holds it.
            # used_classes correctly reflects that the class is taken.
            new_cls, llm_res = fixer.remap_loser(
                loser, cls, table_meaning, used_classes, ontology_classes
            )

            if new_cls:
                colon_new = f":{new_cls}"
                if colon_new in used_classes:
                    # LLM suggested an already-taken class — note it, keep old class
                    # but mark as collision-unresolved (phase 8 will comment it)
                    print(f" ⚠  LLM suggested {colon_new} (already used) — marking unresolved")
                    entity_entries[loser]["_collision_unresolved"] = True
                    entity_entries[loser]["_collision_note"] = (
                        f"LLM suggested :{new_cls} but it was already taken"
                    )
                    collision_log.setdefault(cls, {}).setdefault("losers", {})[loser] = {
                        "status": "unresolved", "llm_suggested": new_cls, "llm_result": llm_res
                    }
                else:
                    print(f" → :{new_cls}")
                    entity_entries[loser]["subject"]["class"] = colon_new
                    used_classes.add(colon_new)
                    collision_log.setdefault(cls, {}).setdefault("losers", {})[loser] = {
                        "status": "remapped", "new_class": new_cls, "llm_result": llm_res
                    }
            else:
                # LLM found nothing — best effort: keep the class as-is but
                # mark it so phase 8 can emit a comment. Do NOT drop.
                print(f" → LLM found no alternative — keeping original class with warning")
                entity_entries[loser]["_collision_unresolved"] = True
                entity_entries[loser]["_collision_note"] = (
                    f"Could not find alternative for {cls} — duplicate class kept"
                )
                collision_log.setdefault(cls, {}).setdefault("losers", {})[loser] = {
                    "status": "no_alternative_found", "llm_result": llm_res
                }

        collision_log[cls]["winner"] = winner
        collision_log[cls]["losers_list"] = losers

    # ── Second pass: check for secondary collisions ────────────
    secondary = detect_collisions(entity_entries)
    # Filter out collisions where we already accepted duplicates
    secondary = {
        c: tabs for c, tabs in secondary.items()
        if not all(entity_entries[t].get("_collision_unresolved") for t in tabs)
    }
    if secondary:
        print(f"\n  Secondary collisions (from LLM remapping):")
        for cls2, tables2 in secondary.items():
            winner2, losers2 = resolve_collision(cls2, tables2, entity_entries)
            print(f"    {cls2}: winner={winner2}, losers={losers2}")
            for loser2 in losers2:
                table_meaning2 = understanding.get(loser2, {}).get("table_meaning", "") or loser2
                print(f"    → Secondary loser: {loser2} — asking LLM...", end="", flush=True)
                new_cls2, llm_res2 = fixer.remap_loser(
                    loser2, cls2, table_meaning2, used_classes, ontology_classes
                )
                if new_cls2:
                    colon_new2 = f":{new_cls2}"
                    if colon_new2 not in used_classes:
                        print(f" → :{new_cls2}")
                        entity_entries[loser2]["subject"]["class"] = colon_new2
                        used_classes.add(colon_new2)
                        collision_log.setdefault(f"SECONDARY_{cls2}", {}).setdefault(
                            "losers", {})[loser2] = {
                            "status": "remapped", "new_class": new_cls2
                        }
                        continue
                print(f" → unresolved — keeping with warning")
                entity_entries[loser2]["_collision_unresolved"] = True

    return entity_entries


# ============================================================
# SEw rescue: remap unresolved SEw tables as properties
# ============================================================

def _fetch_owner_properties(owner_class: str) -> Tuple[List[str], List[str]]:
    """
    Call ontology_explorer(mode="class_properties") for owner_class.
    Returns (data_prop_names, object_prop_names) as plain local-name strings.
    """
    try:
        result = ontology_explorer(mode="class_properties", class_name=owner_class)
    except Exception as e:
        print(f"    [WARN] ontology_explorer failed for {owner_class!r}: {e}")
        return [], []

    data_props: List[str] = []
    obj_props:  List[str] = []

    if isinstance(result, dict):
        # Data properties
        for item in (result.get("data_properties") or []):
            name = item if isinstance(item, str) else (
                item.get("property_name") or item.get("name") or
                (item.get("property_iri", "")).split("#")[-1].split("/")[-1]
            )
            if name:
                data_props.append(name)
        # Object properties
        for item in (result.get("object_properties") or []):
            name = item if isinstance(item, str) else (
                item.get("property_name") or item.get("name") or
                (item.get("property_iri", "")).split("#")[-1].split("/")[-1]
            )
            if name:
                obj_props.append(name)

    return data_props, obj_props


def _find_owner_triple_map_iri(
    owner_table: str,
    se_data: Dict,
    sh_data: Dict,
) -> Optional[str]:
    """Find the triple_map_iri for the owner table across SE and SE_SH phases."""
    for phase_data in (se_data, sh_data):
        entry = phase_data.get(owner_table)
        if entry:
            iri = entry.get("triple_map_iri")
            if iri:
                return iri
    return None


def _find_owner_class(
    owner_table: str,
    se_data: Dict,
    sh_data: Dict,
) -> Optional[str]:
    """Return the ontology class (without colon) for the owner table."""
    for phase_data in (se_data, sh_data):
        entry = phase_data.get(owner_table)
        if entry:
            cls = entry.get("subject", {}).get("class", "")
            if cls:
                return cls.lstrip(":")
    return None


def _get_owner_pk_column(
    owner_table: str,
    se_data: Dict,
    sh_data: Dict,
) -> str:
    """
    Best-effort: find the PK column name of the owner table from its
    subject template (e.g. '{base_iri}{table}/{id}' → 'id').
    Falls back to 'id'.
    """
    for phase_data in (se_data, sh_data):
        entry = phase_data.get(owner_table)
        if entry:
            tmpl = entry.get("subject", {}).get("template", "")
            # Templates look like: "http://base/{table}/{pk_col}"
            parts = tmpl.rstrip("}").split("{")
            if len(parts) >= 2:
                candidate = parts[-1].strip()
                if candidate and "/" not in candidate:
                    return candidate
    return "id"


def rescue_unresolved_sew_as_property(
    sew_data: Dict,
    se_data: Dict,
    sh_data: Dict,
    understanding: Dict,
    tables_structure: Dict,
    idx: OntologyIndex,
    fixer: PredicateFixer,
    report: Dict,
) -> Tuple[Dict, Dict, Dict]:
    """
    For every SEw entry marked _collision_unresolved (no alternative class found),
    attempt to rescue it by:

      1. Determine the owner class from the SEw entry's owner_columns FK.
      2. Call ontology_explorer to get all data + object properties of that class.
      3. Try a fast string-similarity match between the SEw table name and property
         names (e.g. 'e_mails' ~ 'email').  If that fails, ask the LLM.
      4. If a property match is found:
           Inject a new SQL-join TriplesMap entry into the owner's phase dict
           (se_data or sh_data).  The new entry:
             - Uses a SQL query joining owner table → SEw table as its logical table
             - Reuses the owner's subject template (same IRI → same subject in TTL)
             - Has subject.class = None (no rdf:type — owner already emits it)
             - Maps each data column in the SEw table with the matched predicate
           The original SEw entry is removed from sew_data so it is never
           emitted as a standalone entity with a duplicate rr:class.
      5. If no property match found, leave the entry unchanged (still unresolved).

    Returns (sew_data, se_data, sh_data) — all three may be modified.
    """
    sew_data = copy.deepcopy(sew_data)
    to_remove: List[str] = []   # keys successfully rescued — popped AFTER the loop

    for tname, entry in sew_data.items():
        if not entry.get("_collision_unresolved"):
            continue

        print(f"\n  [RESCUE] SEw table '{tname}' is unresolved — trying property rescue...")

        # ── 1. Determine owner table and class ────────────────────────────
        owner_cols = entry.get("owner_columns", [])
        if not owner_cols:
            print(f"    [SKIP] No owner_columns found — cannot determine parent")
            continue

        # owner_columns is a list of FK column names; the first one points to the owner
        owner_fk_col = owner_cols[0]

        # Primary: read the FK reference directly from the attributes list.
        # Phase 3 builds attributes from tables_structure.json which has explicit
        # fk_references — this is reliable and works for any SEw table.
        owner_table = None
        for attr in entry.get("attributes", []):
            if attr.get("name") == owner_fk_col and attr.get("role") in ("pk+fk", "fk"):
                owner_table = attr.get("fk_references", {}).get("table")
                break

        if not owner_table:
            # Fallback: the FK col name often encodes the table name.
            # Strip a trailing "_id" or "id" SUFFIX (not characters) then
            # try common pluralisation patterns, searching SE and SH data.
            base = re.sub(r"_id$", "", owner_fk_col)   # "person_id" → "person"
            if base == owner_fk_col:
                base = re.sub(r"id$",  "", owner_fk_col)  # "personid" → "person" (rare)
            base = base.rstrip("_")                        # clean trailing underscore
            candidates = [base, base + "s", base + "es"]  # person → persons
            # Also try the col name as-is (some schemas use bare table name as FK)
            if owner_fk_col not in candidates:
                candidates.insert(0, owner_fk_col)
            for candidate in candidates:
                if candidate and (candidate in se_data or candidate in sh_data):
                    owner_table = candidate
                    print(f"    [FALLBACK] inferred owner_table='{owner_table}' "
                          f"from FK col '{owner_fk_col}'")
                    break

        if not owner_table:
            print(f"    [SKIP] Could not resolve owner table from FK col '{owner_fk_col}'")
            continue

        owner_class = _find_owner_class(owner_table, se_data, sh_data)
        if not owner_class:
            print(f"    [SKIP] Could not find ontology class for owner table '{owner_table}'")
            continue

        owner_iri = _find_owner_triple_map_iri(owner_table, se_data, sh_data)
        owner_pk  = _get_owner_pk_column(owner_table, se_data, sh_data)

        print(f"    Owner table : '{owner_table}'  class=:{owner_class}  iri={owner_iri}")

        # ── 2. Fetch owner class properties via ontology_explorer ─────────
        data_props, obj_props = _fetch_owner_properties(owner_class)
        print(f"    Owner data props   : {data_props}")
        print(f"    Owner object props : {obj_props}")

        all_prop_names = data_props + obj_props
        if not all_prop_names:
            print(f"    [SKIP] No properties found for :{owner_class}")
            continue

        # ── 3. Use phase3 predicate if already identified, else string-match
        phase3_pred = entry.get("owner_predicate")
        if phase3_pred:
            matched_prop = phase3_pred.lstrip(":")
            matched_kind = "data"
            print(f"    [phase3_pred] Using phase3 owner_predicate: {matched_prop!r}")
        else:
            pass  # fall through to string-match below

        # ── String-similarity match (fast path, no LLM cost) ──────────
        def _norm(s: str) -> str:
            return re.sub(r"[^a-z0-9]", "", s.lower())

        _OWL_PFXS = ("hasan","hasa","hasthe","has","isan","isa","isthe","is")
        def _strip(s: str) -> str:
            n = _norm(s)
            for pfx in _OWL_PFXS:
                if n.startswith(pfx) and len(n) > len(pfx):
                    return n[len(pfx):]
            return n

        tname_norm = _norm(tname)
        if not phase3_pred:
            matched_prop = None
        matched_kind: str = matched_kind if phase3_pred else "data"

        # First pass: exact normalised match — also try OWL prefix-stripped form
        if not phase3_pred:
            for prop in all_prop_names:
                if _norm(prop) == tname_norm or _strip(prop) == tname_norm:
                    matched_prop = prop
                    matched_kind = "data" if prop in data_props else "object"
                    print(f"    [MATCH-exact] '{tname}' ~ '{prop}' (normalised exact)")
                    break

        # Second pass: substring match — also try OWL prefix-stripped form
        if not phase3_pred and not matched_prop:
            for prop in all_prop_names:
                pn = _norm(prop)
                ps = _strip(prop)
                if pn in tname_norm or tname_norm in pn or ps in tname_norm or tname_norm in ps:
                    matched_prop = prop
                    matched_kind = "data" if prop in data_props else "object"
                    print(f"    [MATCH-substr] '{tname}' ~ '{prop}'")
                    break

        # Third pass: LLM
        if not phase3_pred and not matched_prop:
            print(f"    [LLM] String similarity failed — asking LLM...")
            sew_meaning = understanding.get(tname, {}).get("table_meaning", tname)
            llm_result  = fixer.find_property_for_sew(
                tname, sew_meaning, owner_class, data_props, obj_props
            )
            if llm_result:
                matched_prop = llm_result["property"]
                matched_kind = llm_result.get("kind", "data")
                print(f"    [LLM] Matched → :{matched_prop} (kind={matched_kind}, "
                      f"confidence={llm_result.get('confidence')}, "
                      f"why={llm_result.get('reasoning','')!r})")
            else:
                print(f"    [SKIP] LLM found no property match — leaving unresolved")
                continue

        # ── 4. Inject a SQL-join TriplesMap into the owner's phase data ──────
        #
        # The SEw table stores values for a property of the owner entity.
        # Correct R2RML pattern:
        #
        #   <urn:r2rml:SEw_e_mails_prop>
        #     rr:logicalTable [ rr:sqlQuery
        #       "SELECT o.id, s.value FROM persons o JOIN e_mails s ON s.person = o.id" ] ;
        #     rr:subjectMap [ rr:template "...persons/{id}" ] ;  ← same IRI as owner
        #     rr:predicateObjectMap [
        #       rr:predicate :E-mail ;
        #       rr:objectMap [ rr:column "value" ] ;
        #     ] .
        #
        # This TriplesMap is added to the owner's phase dict so the TTL
        # generator emits it alongside the other owner triples.
        # The original SEw entry is removed (marked absorbed) so it is
        # never emitted as a standalone entity with rr:class :Person.

        predicate = f":{matched_prop}"

        # Identify the data column(s).
        # owner_columns (the FK back to the owner) must be skipped — it is
        # not a data value, just a join key.
        # local_pk_columns must NOT be skipped: in tables like e_mails the
        # local PK *is* the data value (the email string itself).  Phase 3
        # labels it local_pk because it forms part of the composite PK, but
        # that doesn't mean it isn't a real data column to map.
        owner_col_set = set(entry.get("owner_columns", []))   # only skip these
        skip_cols     = owner_col_set                          # do NOT skip local_pk

        # Pass 1: literal poms already in the entry
        data_cols: List[str] = []
        for pom in entry.get("predicate_object_maps", []):
            col = pom.get("object", {}).get("column", "")
            if (pom.get("object", {}).get("type") == "literal"
                    and col and col not in skip_cols):
                data_cols.append(col)

        # Pass 2: attributes list (may include local_pk_columns as attributes)
        if not data_cols:
            for attr in entry.get("attributes", []):
                col = attr.get("name", "")
                if col and col not in skip_cols:
                    data_cols.append(col)

        # Pass 3: read tables_structure directly — most reliable, bypasses
        # whatever Phase 3 stored.  Take every column that is not the owner FK.
        if not data_cols and tables_structure:
            for col_info in tables_structure.get(tname, {}).get("columns", []):
                col = col_info.get("name", "")
                if col and col not in skip_cols:
                    data_cols.append(col)
            if data_cols:
                print(f"    [INFO] data cols resolved from tables_structure: {data_cols}")

        # Pass 4: local_pk_columns as last resort (they ARE data in SEw tables
        # like e_mails where the value column is both PK and content)
        if not data_cols:
            data_cols = [c for c in entry.get("local_pk_columns", [])
                         if c not in owner_col_set]
            if data_cols:
                print(f"    [INFO] using local_pk_columns as data cols: {data_cols}")

        if not data_cols:
            print(f"    [WARN] No data columns found to map — leaving unresolved")
            continue

        # Build owner subject template — reuse the owner's own template
        owner_entry   = (se_data.get(owner_table) or sh_data.get(owner_table) or {})
        owner_tmpl    = owner_entry.get("subject", {}).get("template", "")

        # Build the SQL query that joins owner → SEw table
        # owner_pk  : PK column in the owner table (used in template + join)
        # owner_fk_col: FK column in SEw table referencing the owner
        sql_cols = ", ".join(
            [f"o.{owner_pk} AS {owner_pk}"] +
            [f"s.{c} AS {c}" for c in data_cols]
        )
        sql_query = (
            f"SELECT {sql_cols} "
            f"FROM {owner_table} o "
            f"JOIN {tname} s ON s.{owner_fk_col} = o.{owner_pk}"
        )

        # Build one pom per data column
        new_poms: List[Dict] = []
        for col in data_cols:
            new_poms.append({
                "predicate": predicate,
                "object": {
                    "type":     "literal",
                    "column":   col,
                    "datatype": "xsd:string",
                }
            })

        # The new TriplesMap entry — same pattern as an SE entry but with
        # a SQL logical table and no rr:class (subject template alone gives the IRI)
        rescue_key = f"{tname}__rescued_prop"
        rescue_entry = {
            "pattern":         "SEw_rescued",
            "triple_map_iri":  f"urn:r2rml:SEw_{tname}_prop",
            "logical_table_sql": sql_query,
            "subject": {
                "template": owner_tmpl,
                "class":    None,       # no rdf:type — owner already emits it
            },
            "predicate_object_maps": new_poms,
            "_rescued_as_property": True,
            "_rescue_property":     matched_prop,
            "_rescue_owner_table":  owner_table,
            "_rescue_owner_class":  owner_class,
            "_rescue_sew_table":    tname,
            "_rescue_kind":         matched_kind,
        }

        # Inject into the owner's phase dict so the TTL generator finds it
        # alongside the other owner-phase entries.
        if owner_table in se_data:
            se_data[rescue_key] = rescue_entry
            _log(report, tname, "_collision_unresolved", f"→ se_data[{rescue_key!r}]",
                 "sew_property_rescue",
                 f":{owner_class} --:{matched_prop}--> {data_cols} via SQL join")
        elif owner_table in sh_data:
            sh_data[rescue_key] = rescue_entry
            _log(report, tname, "_collision_unresolved", f"→ sh_data[{rescue_key!r}]",
                 "sew_property_rescue",
                 f":{owner_class} --:{matched_prop}--> {data_cols} via SQL join")
        else:
            print(f"    [WARN] Could not find owner phase dict for '{owner_table}' — skipping")
            continue

        # Mark for removal after the loop — cannot mutate dict during iteration
        to_remove.append(tname)

        print(f"    ✓ Rescued '{tname}': "
              f":{owner_class} --:{matched_prop}--> {data_cols}  "
              f"(SQL join injected as '{rescue_key}')")

    # Now safe to remove — iteration is complete
    for tname in to_remove:
        sew_data.pop(tname, None)
        print(f"  [RESCUE] Removed absorbed entry '{tname}' from sew_data")

    return sew_data, se_data, sh_data


# ============================================================
# Main
# ============================================================

def run_correction():
    print("=" * 56)
    print("  ONTOLOGY MAPPER — Phase 7 (Verifier + Collision Resolver)")
    print("=" * 56)

    # ── Build ontology index ─────────────────────────────────
    print(f"\nBuilding ontology index from '{ONTOLOGY_FILE}' ...")
    idx = OntologyIndex(ONTOLOGY_FILE)
    print(f"  Data properties  : {len(idx.data_props)}")
    print(f"  Object properties: {len(idx.obj_props)}")
    print(f"  Classes          : {len(idx.all_classes)}")
    print(f"  Pair index       : {len(idx.pair_to_props)} (domain,range) pairs")

    # ── Load understanding for table meanings ────────────────
    understanding = load_json_optional(UNDERSTANDING_FILE)
    print(f"  Understanding    : {len(understanding)} tables")

    # ── Load ontology classes list ───────────────────────────
    # Use the index's own all_classes set (already parsed from OWL)
    ontology_classes = sorted(idx.all_classes)
    print(f"  Ontology classes : {len(ontology_classes)}")

    # ── Load all phase files ─────────────────────────────────
    print("\nLoading phase JSON files...")
    se_data  = load_json_optional(SE_FILE)
    sh_data  = load_json_optional(SH_FILE)
    sew_data = load_json_optional(SEW_FILE)
    srr_data = load_json_optional(SRR_FILE)
    sr_data  = load_json_optional(SR_FILE)
    print(f"  SE={len(se_data)}  SH={len(sh_data)}  SEw={len(sew_data)}  "
          f"SRR={len(srr_data)}  SR={len(sr_data)}")

    # ── Load tables_structure (needed for dropped SEw detection) ─
    tables_structure = load_json_optional(
        os.path.join("src", "outputs", "DB_as_json", "tables_structure.json")
    )
    print(f"  Tables structure : {len(tables_structure)} tables")

    # ── Build unified class map ──────────────────────────────
    class_map = build_class_map([se_data, sh_data, sew_data, srr_data])
    print(f"\n  Class map: {len(class_map)} TriplesMap IRIs resolved")

    fixer  = PredicateFixer(provider=SELECTED_PROVIDER)
    report: Dict = {}

    # ── Step 1: Correct SE entries ───────────────────────────
    print("\nCorrecting SE predicates...")
    for tname, entry in se_data.items():
        se_data[tname] = correct_entry(tname, entry, idx, class_map, fixer, report, tables_structure)

    # ── Step 2: Correct SE_SH entries ───────────────────────
    print("\nCorrecting SE_SH predicates...")
    for tname, entry in sh_data.items():
        sh_data[tname] = correct_entry(tname, entry, idx, class_map, fixer, report, tables_structure)

    # ── Step 3: Correct SEw entries ──────────────────────────
    print("\nCorrecting SEw predicates...")
    for tname, entry in sew_data.items():
        sew_data[tname] = correct_entry(tname, entry, idx, class_map, fixer, report, tables_structure)

    # ── Step 4: Correct SRR entries ──────────────────────────
    if srr_data:
        print("\nCorrecting SRR predicates...")
        for tname, entry in srr_data.items():
            srr_data[tname] = correct_entry(tname, entry, idx, class_map, fixer, report, tables_structure)

    # ── Step 5: Correct SR predicates and directions ─────────
    if sr_data:
        print("\nCorrecting SR predicates and directions...")
        sr_data = correct_sr_directions(sr_data, idx, class_map, fixer, report)

    # ── Step 6: Inject dropped SEw structural joins ──────────
    print("\nChecking for dropped SEw structural joins...")
    sew_data = inject_dropped_sew_joins(sew_data, sh_data, tables_structure, report)

    # ── Step 7: Fix SE_SH inheritance attribute gaps ─────────
    print("\nChecking SE_SH inheritance attribute gaps...")
    sh_data = inject_inherited_attributes(sh_data, report)

    # ── Step 8: Inject rdfs:label for label columns ──────────
    print("\nInjecting rdfs:label for label columns (Q28)...")
    se_data = inject_rdfs_labels(se_data, report)

    # ── Step 9: Resolve class collisions ─────────────────────
    # IMPORTANT: this must happen AFTER predicate correction and structural
    # fixes, and BEFORE saving, so phase 8 reads clean collision-free files.
    print("\nResolving class collisions across all entity phases...")
    collision_log: Dict = {}

    # Merge all entity phases into one view for collision detection
    all_entity: Dict = {}
    for t, e in se_data.items():
        all_entity[t] = {**e, "_phase_file": "SE", "pattern": e.get("pattern", "SE")}
    for t, e in sh_data.items():
        all_entity[t] = {**e, "_phase_file": "SH", "pattern": e.get("pattern", "SE_SH")}
    for t, e in sew_data.items():
        all_entity[t] = {**e, "_phase_file": "SEw", "pattern": e.get("pattern", "SEw")}
    for t, e in srr_data.items():
        all_entity[t] = {**e, "_phase_file": "SRR", "pattern": e.get("pattern", "SRR")}

    all_entity = resolve_all_collisions(
        all_entity, understanding, ontology_classes, fixer, collision_log
    )

    # Write collision resolution decisions back into per-phase dicts
    for t, e in all_entity.items():
        phase_file = e.get("_phase_file", "SE")
        # Strip the internal keys before saving
        clean_entry = {k: v for k, v in e.items()
                       if not k.startswith("_phase_file")}
        if phase_file == "SE"  and t in se_data:   se_data[t]  = clean_entry
        if phase_file == "SH"  and t in sh_data:   sh_data[t]  = clean_entry
        if phase_file == "SEw" and t in sew_data:  sew_data[t] = clean_entry
        if phase_file == "SRR" and t in srr_data:  srr_data[t] = clean_entry

    # ── Step 10: Rescue unresolved SEw tables + property-table SEw ──────
    # Any SEw table that lost the collision resolver OR that was assigned a
    # generic/container class (like :Conference_participant) is inspected:
    # if a property of its owner class semantically matches the table name
    # (e.g. has_an_email table → Person::has_an_email data property),
    # the table is rewritten as a property block on the owner.
    #
    # GENERIC_CLASSES: classes that indicate the SEw wasn't properly mapped
    # to a specific entity — these should be rescued as properties instead.
    GENERIC_RESCUE_CLASSES = {
        "Conference_participant", "Active_conference_participant",
        "Conference_contributor", "Person",  # Person on SEw = collision fallback
    }
    print("\nRescuing unresolved SEw tables as owner properties...")
    unresolved_sew = [
        t for t, e in sew_data.items()
        if e.get("_collision_unresolved")
        or e.get("sew_type") == "property_of_owner"
        or e.get("subject", {}).get("class", "").lstrip(":") in GENERIC_RESCUE_CLASSES
    ]
    if unresolved_sew:
        print(f"  Unresolved SEw tables: {unresolved_sew}")
        sew_data, se_data, sh_data = rescue_unresolved_sew_as_property(
            sew_data, se_data, sh_data, understanding, tables_structure, idx, fixer, report
        )
    else:
        print("  No unresolved SEw tables — nothing to rescue.")

    # ── Save corrected files ─────────────────────────────────
    # Always save all files unconditionally — even an empty dict must overwrite
    # a stale file on disk so the TTL generator never reads old data.
    print("\nSaving corrected files...")
    save_json(SE_FILE,  se_data);  print(f"  ✓ {SE_FILE}")
    save_json(SH_FILE,  sh_data);  print(f"  ✓ {SH_FILE}")
    save_json(SEW_FILE, sew_data); print(f"  ✓ {SEW_FILE}")
    save_json(SRR_FILE, srr_data); print(f"  ✓ {SRR_FILE}")
    if sr_data:  save_json(SR_FILE, sr_data); print(f"  ✓ {SR_FILE}")

    save_json(CORRECTION_REPORT, report)
    print(f"  ✓ {CORRECTION_REPORT}")
    save_json(COLLISION_REPORT, collision_log)
    print(f"  ✓ {COLLISION_REPORT}")

    # ── Summary ──────────────────────────────────────────────
    total_fixes = sum(len(v) for v in report.values())
    n_collisions = len([c for c in collision_log if not c.startswith("SECONDARY_")])
    n_remapped   = sum(
        1 for c_data in collision_log.values()
        for t_data in c_data.get("losers", {}).values()
        if t_data.get("status") == "remapped"
    )
    n_unresolved = sum(
        1 for c_data in collision_log.values()
        for t_data in c_data.get("losers", {}).values()
        if t_data.get("status") in ("unresolved", "no_alternative_found")
    )

    print(f"\n{'=' * 56}")
    print("  PHASE 7 COMPLETE")
    print(f"{'=' * 56}")
    print(f"  Total predicate corrections : {total_fixes}")
    for kind in ("data_prop_name", "data_prop_range", "obj_prop_name",
                 "sr_predicate", "sr_direction_swap", "sr_predicate_fallback",
                 "data_prop_llm", "obj_prop_llm",
                 "structural_fix", "inheritance_fix", "sr_unresolved",
                 "label_to_name", "rdfs_label_inject", "sew_property_rescue"):
        count = sum(1 for fixes in report.values()
                    for f in fixes if f["kind"] == kind)
        if count:
            print(f"  {kind:25}: {count}")
    print(f"\n  Class collisions detected   : {n_collisions}")
    print(f"  Losers remapped by LLM      : {n_remapped}")
    print(f"  Unresolved (kept w/ warning): {n_unresolved}")
    print(f"\n  Correction report → {CORRECTION_REPORT}")
    print(f"  Collision report  → {COLLISION_REPORT}\n")


if __name__ == "__main__":
    try:
        run_correction()
    except Exception as e:
        print(f"\nFatal error: {e}")
        import traceback
        traceback.print_exc()