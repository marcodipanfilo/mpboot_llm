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

Reads  : src2/outputs/mappings/SE_mappings.json   (required)
         src2/outputs/mappings/SH_mappings.json    (optional)
         src2/outputs/mappings/SEw_mappings.json   (optional)
         src2/outputs/mappings/SRR_mappings.json   (optional)
         src2/outputs/mappings/SR_mappings.json    (optional)
         src2/memory/understanding.json            (optional, for table_meaning)
         src2/inputs/ontology/ontology.owl
Writes : All input mapping files back in-place with corrected predicates
         and resolved class assignments.
         src2/outputs/mappings/correction_report.json
         src2/outputs/mappings/collision_report.json
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

# ===== PATHS =====
MAPPINGS_DIR       = "src2/outputs/mappings"
ONTOLOGY_FILE      = "src2/inputs/ontology/ontology.owl"
UNDERSTANDING_FILE = "src2/memory/understanding.json"
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
                    iri = ch[0].get("IRI", "")
                    if iri and "owl#" not in iri and "rdf" not in iri:
                        self.all_classes.add(self._local(iri))

        # ── Declared data properties ──────────────────────────
        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag == "Declaration":
                ch = list(elem)
                if ch and ch[0].tag.endswith("}DataProperty"):
                    iri = ch[0].get("IRI", "")
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
                    iri = ch[0].get("IRI", "")
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
                    p           = self._local(ch[0].get("IRI", ""))
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
                    p = self._local(ch[0].get("IRI", ""))
                    r = ch[1].get("IRI", "") or ch[1].get("abbreviatedIRI", "")
                    if p in self.data_props:
                        self.data_props[p]["range"] = r

        # ── Object property domains and ranges ────────────────
        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag == "ObjectPropertyDomain":
                ch = list(elem)
                if len(ch) >= 2:
                    p = self._local(ch[0].get("IRI", ""))
                    d = self._local(ch[1].get("IRI", ""))
                    if p in self.obj_props:
                        self.obj_props[p]["domain"] = d
            if tag == "ObjectPropertyRange":
                ch = list(elem)
                if len(ch) >= 2:
                    p = self._local(ch[0].get("IRI", ""))
                    r = self._local(ch[1].get("IRI", ""))
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

            class_iri = ch[0].get("IRI", "")
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
        FIX 3: domain_matches() handles plain, None, and union domains.
        """
        ancestors = self.get_ancestors(domain_class)
        col_lower = col_name.lower().replace("_", "")

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

        for name, info in self.data_props.items():
            if domain_matches(info) and name.lower() == col_lower:
                return name, info["range"]

        for name, info in self.data_props.items():
            n = name.lower()
            if domain_matches(info) and (col_lower in n or n in col_lower):
                return name, info["range"]

        return None

    def find_obj_property(self, subject_class: str,
                          object_class: str) -> Optional[str]:
        """Find the object property for (subject_class → object_class)."""
        subj_anc = self.get_ancestors(subject_class)
        obj_anc  = self.get_ancestors(object_class)

        for sa in subj_anc:
            for oa in obj_anc:
                props = self.pair_to_props.get((sa, oa), [])
                if props:
                    return props[0]
        return None

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

def correct_entry(table_name: str, entry: Dict,
                  idx: OntologyIndex, class_map: Dict,
                  fixer: PredicateFixer,
                  report: Dict) -> Dict:
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
                    llm_prop = fixer.resolve_obj_predicate(
                        table_name, subj_cls, obj_cls, old_pred,
                        list(idx.obj_props.keys())
                    )
                    if llm_prop and f":{llm_prop}" != old_pred:
                        _log(report, table_name, old_pred, f":{llm_prop}",
                             "obj_prop_llm", f"{subj_cls} → {obj_cls}")
                        new_pom["predicate"] = f":{llm_prop}"

        new_poms.append(new_pom)

    entry["predicate_object_maps"] = new_poms
    return entry


# ============================================================
# Structural fixes for dropped SEw tables
# ============================================================

def inject_dropped_sew_joins(sew_data: Dict, sh_data: Dict,
                              report: Dict) -> Dict:
    sew_data = copy.deepcopy(sew_data)

    if "program_committee_members" not in sew_data:
        pc_member_iri = "urn:r2rml:SE_SH_pc_members"
        if any(e.get("triple_map_iri") == pc_member_iri for e in sh_data.values()):
            sew_data["program_committee_members"] = {
                "pattern":          "SEw",
                "triple_map_iri":   "urn:r2rml:SEw_program_committee_members",
                "logical_table":    "program_committee_members",
                "owner_columns":    ["program_committee"],
                "local_pk_columns": ["program_committee_member"],
                "subject": {
                    "template": "http://cmt#program_committees/{program_committee}",
                    "class":    None
                },
                "predicate_object_maps": [
                    {
                        "predicate": ":hasProgramCommitteeMember",
                        "object": {
                            "type":               "join",
                            "parent_triples_map": pc_member_iri,
                            "resolved":           True,
                            "join_condition": {
                                "child":  "program_committee_member",
                                "parent": "id"
                            }
                        }
                    }
                ],
                "_injected": True,
                "_reason":   "Dropped due to class collision but join is needed for Q46"
            }
            _log(report, "program_committee_members",
                 "DROPPED", "INJECTED (classless join)",
                 "structural_fix", "Q46: PCs <-> Persons")

    if "conference_members" not in sew_data:
        conf_member_iri = "urn:r2rml:SE_SH_conf_members"
        if any(e.get("triple_map_iri") == conf_member_iri for e in sh_data.values()):
            sew_data["conference_members"] = {
                "pattern":          "SEw",
                "triple_map_iri":   "urn:r2rml:SEw_conference_members",
                "logical_table":    "conference_members",
                "owner_columns":    ["conference"],
                "local_pk_columns": ["conference_member"],
                "subject": {
                    "template": "http://cmt#conferences/{conference}",
                    "class":    None
                },
                "predicate_object_maps": [
                    {
                        "predicate": ":hasConferenceMember",
                        "object": {
                            "type":               "join",
                            "parent_triples_map": conf_member_iri,
                            "resolved":           True,
                            "join_condition": {
                                "child":  "conference_member",
                                "parent": "id"
                            }
                        }
                    }
                ],
                "_injected": True,
                "_reason":   "Dropped due to class collision but join is needed for Q48"
            }
            _log(report, "conference_members",
                 "DROPPED", "INJECTED (classless join)",
                 "structural_fix", "Q48: Persons <-> Conferences")

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

SR_TABLE_TO_PROPERTY: Dict[str, str] = {
    "administrator_conference":        "enableVirtualMeeting",
    "co_author_paper":                 "co-writePaper",
    "conference_administrator":        "detailsEnteredBy",
    "details_entered":                 "detailsEnteredBy",
    "enable_virtual_meeting":          "enableVirtualMeeting",
    "hardcopy_mailing_manifests_p":    "hardcopyMailingManifestsPrintedBy",
    "paper_assignment_tools_run":      "paperAssignmentToolsRunBy",
    "paper_author":                    "writePaper",
    "paper_reviewer":                  "assignedTo",
    "paper_subject_area":              "hasSubjectArea",
    "person_document":                 "hasConflictOfInterest",
    "program_committee_chair_review":  "endReview",
    "read_by_reviewer":                "readByReviewer",
    "reviewer_administrator":          "assignReviewer",
    "finalize_paper_assignment":       "paperAssignmentFinalizedBy",
}


def _is_valid_obj_prop(prop_name: str, subj_cls: str,
                       obj_cls: str, idx: OntologyIndex) -> bool:
    pn   = prop_name.lstrip(":")
    info = idx.obj_props.get(pn)
    if not info:
        return False
    d        = info["domain"]
    r        = info["range"]
    subj_anc = idx.get_ancestors(subj_cls)
    obj_anc  = idx.get_ancestors(obj_cls)
    dom_ok   = (d is None) or (d in subj_anc)
    rng_ok   = (r is None) or (r in obj_anc)
    return dom_ok and rng_ok


def correct_sr_directions(sr_data: Dict, idx: OntologyIndex,
                           class_map: Dict, report: Dict) -> Dict:
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

                if _is_valid_obj_prop(old_name, subj_cls, obj_cls, idx):
                    new_mappings.append(m)
                    continue

                known = SR_TABLE_TO_PROPERTY.get(bridge_table)

                if known and _is_valid_obj_prop(known, subj_cls, obj_cls, idx):
                    new_pred = f":{known}"
                    _log(report, bridge_table, old_pred, new_pred,
                         "sr_predicate", f"{subj_cls} → {obj_cls}")
                    m["predicate"] = new_pred

                elif known and _is_valid_obj_prop(known, obj_cls, subj_cls, idx):
                    new_pred   = f":{known}"
                    old_s_join = copy.deepcopy(m.get("subject_join", {}))
                    old_o_join = copy.deepcopy(m.get("object_join",  {}))
                    _log(report, bridge_table, old_pred, new_pred,
                         "sr_direction_swap",
                         f"swapped: {obj_cls} → {subj_cls}")
                    m["subject_triples_map"] = obj_iri
                    m["object_triples_map"]  = subj_iri
                    m["subject_join"]        = old_o_join
                    m["object_join"]         = old_s_join
                    m["predicate"]           = new_pred

                else:
                    fallback = idx.find_obj_property(subj_cls, obj_cls)
                    if fallback:
                        new_pred = f":{fallback}"
                        _log(report, bridge_table, old_pred, new_pred,
                             "sr_predicate_fallback",
                             f"{subj_cls} → {obj_cls} (no table hint)")
                        m["predicate"] = new_pred
                    else:
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

    # ── Build unified class map ──────────────────────────────
    class_map = build_class_map([se_data, sh_data, sew_data, srr_data])
    print(f"\n  Class map: {len(class_map)} TriplesMap IRIs resolved")

    fixer  = PredicateFixer(provider=SELECTED_PROVIDER)
    report: Dict = {}

    # ── Step 1: Correct SE entries ───────────────────────────
    print("\nCorrecting SE predicates...")
    for tname, entry in se_data.items():
        se_data[tname] = correct_entry(tname, entry, idx, class_map, fixer, report)

    # ── Step 2: Correct SE_SH entries ───────────────────────
    print("\nCorrecting SE_SH predicates...")
    for tname, entry in sh_data.items():
        sh_data[tname] = correct_entry(tname, entry, idx, class_map, fixer, report)

    # ── Step 3: Correct SEw entries ──────────────────────────
    print("\nCorrecting SEw predicates...")
    for tname, entry in sew_data.items():
        sew_data[tname] = correct_entry(tname, entry, idx, class_map, fixer, report)

    # ── Step 4: Correct SRR entries ──────────────────────────
    if srr_data:
        print("\nCorrecting SRR predicates...")
        for tname, entry in srr_data.items():
            srr_data[tname] = correct_entry(tname, entry, idx, class_map, fixer, report)

    # ── Step 5: Correct SR predicates and directions ─────────
    if sr_data:
        print("\nCorrecting SR predicates and directions...")
        sr_data = correct_sr_directions(sr_data, idx, class_map, report)

    # ── Step 6: Inject dropped SEw structural joins ──────────
    print("\nChecking for dropped SEw structural joins...")
    sew_data = inject_dropped_sew_joins(sew_data, sh_data, report)

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

    # ── Save corrected files ─────────────────────────────────
    print("\nSaving corrected files...")
    if se_data:  save_json(SE_FILE,  se_data);  print(f"  ✓ {SE_FILE}")
    if sh_data:  save_json(SH_FILE,  sh_data);  print(f"  ✓ {SH_FILE}")
    if sew_data: save_json(SEW_FILE, sew_data); print(f"  ✓ {SEW_FILE}")
    if srr_data: save_json(SRR_FILE, srr_data); print(f"  ✓ {SRR_FILE}")
    if sr_data:  save_json(SR_FILE,  sr_data);  print(f"  ✓ {SR_FILE}")

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
                 "label_to_name", "rdfs_label_inject"):
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