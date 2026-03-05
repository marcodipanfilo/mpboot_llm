"""
Ontology Mapper — Phase 6b: Ontology Property Corrector

This phase runs AFTER all mapping phases (1–6) and BEFORE phase 7 (TTL generation).
It corrects all predicate names in the phase JSON files to match the exact property
IRIs declared in the ontology, and fixes datatype mismatches.

Problem it solves:
  Phases 1–5 LLM agents invent predicate names by camelCasing column names or
  guessing semantically. These invented names (e.g. :adjusted, :decision, :written,
  :comment, :siteUrl, :paperId) do not match the actual ontology property IRIs
  (e.g. :adjustedBy, :hasDecision, :writtenBy, :review, :siteURL, :paperID).
  This causes SPARQL queries against the generated knowledge graph to return 0 results.

What it does:
  For EVERY predicate in every phase JSON file:
    1. Data properties  → looks up the column's host class in the ontology data property
                          index. Corrects predicate name AND datatype to ontology values.
    2. Object properties → looks up (subject_class, object_class) pair in the ontology
                           object property index. Picks the matching property.
                           Also checks directionality — if the FK is on the wrong side,
                           notes a direction warning (phase 7 must handle via inverse).
    3. Anything unresolved → calls LLM with ontology context to pick best match.

Additionally fixes two structural gaps caused by dropped SEw tables:
  - program_committee_members: must still emit :hasProgramCommitteeMember join
  - conference_members: must still emit :hasConferenceMember join
  These are written directly into the SEw JSON before phase 7 reads it.

Also fixes the paper_abstracts inheritance gap (Q35):
  paper_abstracts has no paper_id column — uses SQL join to pull it from papers.

Reads  : src2/outputs/mappings/SE_mappings.json   (required)
         src2/outputs/mappings/SH_mappings.json    (optional)
         src2/outputs/mappings/SEw_mappings.json   (optional)
         src2/outputs/mappings/SRR_mappings.json   (optional)
         src2/outputs/mappings/SR_mappings.json    (optional)
         src2/inputs/ontology/ontology.owl
Writes : All input files back in-place with corrected predicates
         src2/outputs/mappings/correction_report.json
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
MAPPINGS_DIR      = "src2/outputs/mappings"
ONTOLOGY_FILE     = "src2/inputs/ontology/ontology.owl"
SE_FILE           = os.path.join(MAPPINGS_DIR, "SE_mappings.json")
SH_FILE           = os.path.join(MAPPINGS_DIR, "SH_mappings.json")
SEW_FILE          = os.path.join(MAPPINGS_DIR, "SEw_mappings.json")
SRR_FILE          = os.path.join(MAPPINGS_DIR, "SRR_mappings.json")
SR_FILE           = os.path.join(MAPPINGS_DIR, "SR_mappings.json")
CORRECTION_REPORT = os.path.join(MAPPINGS_DIR, "correction_report.json")


# ============================================================
# Ontology parser — builds full property indexes
# ============================================================

class OntologyIndex:
    """
    Parses the OWL file and builds:
      - data_props:   {prop_local_name: {iri, domain_class, range_datatype}}
      - obj_props:    {prop_local_name: {iri, domain_class, range_class}}
      - pair_to_props:{(domain_class, range_class): [prop_local_name, ...]}
      - subclass_of:  {child_class: [parent_class, ...]}  (direct + inherited)
    """

    def __init__(self, owl_file: str):
        self.data_props:    Dict = {}
        self.obj_props:     Dict = {}
        self.pair_to_props: Dict = {}   # (dom, rng) → [prop_name, ...]
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

        # Declared classes
        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag == "Declaration":
                ch = list(elem)
                if ch and ch[0].tag.endswith("}Class"):
                    iri = ch[0].get("IRI", "")
                    if iri and "owl#" not in iri and "rdf" not in iri:
                        self.all_classes.add(self._local(iri))

        # Declared data properties
        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag == "Declaration":
                ch = list(elem)
                if ch and ch[0].tag.endswith("}DataProperty"):
                    iri = ch[0].get("IRI", "")
                    if iri:
                        name = self._local(iri)
                        self.data_props[name] = {
                            "iri": iri, "domain": None, "range": None
                        }

        # Declared object properties
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

        # Data property domains and ranges
        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag == "DataPropertyDomain":
                ch = list(elem)
                if len(ch) >= 2:
                    p = self._local(ch[0].get("IRI", ""))
                    d = self._local(ch[1].get("IRI", ""))
                    if p in self.data_props:
                        self.data_props[p]["domain"] = d
            if tag == "DataPropertyRange":
                ch = list(elem)
                if len(ch) >= 2:
                    p = self._local(ch[0].get("IRI", ""))
                    r = ch[1].get("IRI", "") or ch[1].get("abbreviatedIRI", "")
                    if p in self.data_props:
                        self.data_props[p]["range"] = r

        # Object property domains and ranges
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

        # SubClassOf
        for elem in root.iter():
            if elem.tag.endswith("}SubClassOf"):
                ch = list(elem)
                if len(ch) == 2:
                    sub = self._local(ch[0].get("IRI", "") or ch[0].get("abbreviatedIRI", ""))
                    sup = self._local(ch[1].get("IRI", "") or ch[1].get("abbreviatedIRI", ""))
                    if sub and sup and sup != "Thing":
                        self.subclass_of[sub].add(sup)

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
        """Return cls + all its ancestors."""
        return {cls} | self.subclass_of.get(cls, set())

    def find_data_property(self, domain_class: str, col_name: str) -> Optional[Tuple[str, str]]:
        """
        Find matching data property for (domain_class, column_name).
        Checks exact name match first, then substring match, considering
        the full ancestor chain of domain_class.
        Returns (prop_name, range_datatype) or None.
        """
        ancestors = self.get_ancestors(domain_class)
        col_lower  = col_name.lower().replace("_", "")

        # Pass 1: exact local name match (case-insensitive)
        for name, info in self.data_props.items():
            name_norm = name.lower()
            if (info["domain"] in ancestors or info["domain"] is None):
                if name_norm == col_lower:
                    return name, info["range"]

        # Pass 2: col_name is a substring of prop name or vice versa
        for name, info in self.data_props.items():
            name_norm = name.lower()
            if (info["domain"] in ancestors or info["domain"] is None):
                if col_lower in name_norm or name_norm in col_lower:
                    return name, info["range"]

        return None

    def find_obj_property(self, subject_class: str,
                          object_class: str) -> Optional[str]:
        """
        Find the object property for (subject_class, object_class).
        Checks subject_class ancestors × object_class ancestors.
        Returns property name or None.
        """
        subj_anc = self.get_ancestors(subject_class)
        obj_anc  = self.get_ancestors(object_class)

        for sa in subj_anc:
            for oa in obj_anc:
                props = self.pair_to_props.get((sa, oa), [])
                if props:
                    return props[0]  # return first match
        return None

    def get_ancestors(self, cls: str) -> Set[str]:
        # alias so both internal and external callers work
        return {cls} | self.subclass_of.get(cls, set())

    def get_class_from_triple_map_iri(self, iri: str,
                                      entity_entries: Dict) -> Optional[str]:
        """Look up the ontology class of a TriplesMap by its IRI."""
        for entry in entity_entries.values():
            if entry.get("triple_map_iri") == iri:
                cls = entry.get("subject", {}).get("class", "")
                return cls.lstrip(":") if cls else None
        return None


# ============================================================
# LLM fallback for unresolved predicates
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
# Core correction logic
# ============================================================

def correct_entry(table_name: str, entry: Dict,
                  idx: OntologyIndex, class_map: Dict,
                  fixer: PredicateFixer,
                  report: Dict) -> Dict:
    """
    Correct all predicate_object_maps in one entry.
    Mutates a deep copy of the entry and returns it.
    """
    entry   = copy.deepcopy(entry)
    subj_cls_raw = entry.get("subject", {}).get("class", "")
    subj_cls     = subj_cls_raw.lstrip(":")
    new_poms     = []

    for pom in entry.get("predicate_object_maps", []):
        old_pred = pom.get("predicate", "")
        obj      = pom.get("object", {})
        new_pom  = copy.deepcopy(pom)

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

                # Fix datatype
                if new_range and new_range != obj.get("datatype"):
                    _log(report, table_name, obj["datatype"], new_range,
                         "data_prop_range", f"column={col}")
                    new_pom["object"]["datatype"] = new_range
            else:
                # LLM fallback
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
                    # LLM fallback
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


def _log(report: Dict, table: str, old: str, new: str, kind: str, detail: str):
    report.setdefault(table, []).append({
        "kind": kind, "old": old, "new": new, "detail": detail
    })
    print(f"    [{kind}] {table}: {old}  →  {new}  ({detail})")


# ============================================================
# Structural fixes for dropped SEw tables (Q46, Q48)
# ============================================================

def inject_dropped_sew_joins(sew_data: Dict, sh_data: Dict,
                              report: Dict) -> Dict:
    """
    Add back classless join TriplesMap entries for tables that were dropped
    due to class collision but whose join triples are still needed.

    program_committee_members: ProgramCommittee →:hasProgramCommitteeMember→ ProgramCommitteeMember
    conference_members:        Conference →:hasConferenceMember→ ConferenceMember
    """
    sew_data = copy.deepcopy(sew_data)

    # program_committee_members
    if "program_committee_members" not in sew_data:
        pc_member_iri = "urn:r2rml:SE_SH_pc_members"
        # Verify it exists in SH
        if any(e.get("triple_map_iri") == pc_member_iri for e in sh_data.values()):
            sew_data["program_committee_members"] = {
                "pattern":        "SEw",
                "triple_map_iri": "urn:r2rml:SEw_program_committee_members",
                "logical_table":  "program_committee_members",
                "owner_columns":  ["program_committee"],
                "local_pk_columns": ["program_committee_member"],
                "subject": {
                    "template": "http://cmt#program_committees/{program_committee}",
                    "class":    None   # intentionally classless — only the join matters
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

    # conference_members
    if "conference_members" not in sew_data:
        conf_member_iri = "urn:r2rml:SE_SH_conf_members"
        if any(e.get("triple_map_iri") == conf_member_iri for e in sh_data.values()):
            sew_data["conference_members"] = {
                "pattern":        "SEw",
                "triple_map_iri": "urn:r2rml:SEw_conference_members",
                "logical_table":  "conference_members",
                "owner_columns":  ["conference"],
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
# Inheritance attribute fix (Q35 — paper_abstracts needs paper_id)
# ============================================================

def inject_inherited_attributes(sh_data: Dict, report: Dict) -> Dict:
    """
    paper_abstracts table has only (id PK). It inherits from papers via SE_SH chain.
    The paperID data property (domain=Paper, range=xsd:unsignedLong) must be
    accessible on PaperAbstract individuals too.

    Fix: Change paper_abstracts logicalTable to an SQL join that brings paper_id
    into scope, and add :paperID predicate_object_map.

    Generalised rule applied here: for any SE_SH table that has no predicate_object_maps
    AND whose parent chain has data properties that apply to this subclass too,
    inject a SQL join and the missing attributes.
    """
    sh_data = copy.deepcopy(sh_data)

    for tname, entry in sh_data.items():
        if tname == "paper_abstracts":
            # paper_abstracts only has id — needs paper_id from papers via join
            already_has_paper_id = any(
                p.get("predicate") in (":paperID", ":paperId")
                for p in entry.get("predicate_object_maps", [])
            )
            if not already_has_paper_id:
                # Switch to SQL query that joins papers table
                entry["logical_table_sql"] = (
                    "SELECT pa.id, p.paper_id, p.title "
                    "FROM paper_abstracts pa "
                    "JOIN papers p ON pa.id = p.id"
                )
                # Add paperID property
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
                     "inheritance_fix", "Q35: Abstract IDs")

    return sh_data


# ============================================================
# SR direction correction
# ============================================================


# Bridge table name → the specific ontology property it represents.
# Used when multiple valid properties exist for the same (domain, range) pair
# and we need the semantically correct one, not just the first alphabetically.
# Built from the domain knowledge: table name encodes the action/relationship.
SR_TABLE_TO_PROPERTY: Dict[str, str] = {
    "administrator_conference":        "enableVirtualMeeting",       # generic admin↔conf link
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
    """True if prop_name exists in ontology AND its domain/range covers (subj_cls, obj_cls)."""
    pn   = prop_name.lstrip(":")
    info = idx.obj_props.get(pn)
    if not info:
        return False
    d           = info["domain"]
    r           = info["range"]
    subj_anc    = idx.get_ancestors(subj_cls)
    obj_anc     = idx.get_ancestors(obj_cls)
    dom_ok      = (d is None) or (d in subj_anc)
    rng_ok      = (r is None) or (r in obj_anc)
    return dom_ok and rng_ok


def correct_sr_directions(sr_data: Dict, idx: OntologyIndex,
                           class_map: Dict, report: Dict) -> Dict:
    """
    For each SR mapping:
      1. If current predicate is already a valid ontology property for
         (subj_cls, obj_cls) → keep it unchanged (don't replace with a random one).
      2. If it's an invented/wrong predicate:
         a. Check SR_TABLE_TO_PROPERTY for the semantically correct one.
         b. Verify that property is valid for this pair.
         c. If the pair is reversed (domain↔range swapped), swap subject/object joins.
      3. If nothing resolves → leave as-is and log a warning.
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

                # Step 1: current pred already valid → keep
                if _is_valid_obj_prop(old_name, subj_cls, obj_cls, idx):
                    new_mappings.append(m)
                    continue

                # Step 2a: look up known mapping for this bridge table
                known = SR_TABLE_TO_PROPERTY.get(bridge_table)

                if known and _is_valid_obj_prop(known, subj_cls, obj_cls, idx):
                    # Forward direction is correct, just wrong predicate name
                    new_pred = f":{known}"
                    _log(report, bridge_table, old_pred, new_pred,
                         "sr_predicate", f"{subj_cls} → {obj_cls}")
                    m["predicate"] = new_pred

                elif known and _is_valid_obj_prop(known, obj_cls, subj_cls, idx):
                    # Direction is reversed — swap subject/object
                    new_pred  = f":{known}"
                    old_s_join = copy.deepcopy(m.get("subject_join", {}))
                    old_o_join = copy.deepcopy(m.get("object_join", {}))
                    _log(report, bridge_table, old_pred, new_pred,
                         "sr_direction_swap",
                         f"swapped direction: {obj_cls} → {subj_cls}")
                    m["subject_triples_map"] = obj_iri
                    m["object_triples_map"]  = subj_iri
                    m["subject_join"]        = old_o_join
                    m["object_join"]         = old_s_join
                    m["predicate"]           = new_pred

                else:
                    # Step 2b: fall back to first valid property for this pair
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
# Main
# ============================================================

def run_correction():
    print("=" * 56)
    print("  ONTOLOGY MAPPER — Phase 6b (Property Corrector)")
    print("=" * 56)

    # ── Load ontology index ──────────────────────────────────
    print(f"\nBuilding ontology index from '{ONTOLOGY_FILE}' ...")
    idx = OntologyIndex(ONTOLOGY_FILE)
    print(f"  Data properties  : {len(idx.data_props)}")
    print(f"  Object properties: {len(idx.obj_props)}")
    print(f"  Classes          : {len(idx.all_classes)}")
    print(f"  Pair index       : {len(idx.pair_to_props)} (domain,range) pairs")

    # ── Load all phase files ─────────────────────────────────
    print("\nLoading phase JSON files...")
    se_data  = load_json_optional(SE_FILE)
    sh_data  = load_json_optional(SH_FILE)
    sew_data = load_json_optional(SEW_FILE)
    srr_data = load_json_optional(SRR_FILE)
    sr_data  = load_json_optional(SR_FILE)

    # ── Build unified class map ──────────────────────────────
    class_map = build_class_map([se_data, sh_data, sew_data, srr_data])

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

    # ── Step 5: Correct SR directions and predicates ─────────
    if sr_data:
        print("\nCorrecting SR predicates and directions...")
        sr_data = correct_sr_directions(sr_data, idx, class_map, report)

    # ── Step 6: Inject dropped SEw join tables ───────────────
    print("\nChecking for dropped SEw structural joins...")
    sew_data = inject_dropped_sew_joins(sew_data, sh_data, report)

    # ── Step 7: Fix inheritance attribute gaps ───────────────
    print("\nChecking SE_SH inheritance attribute gaps...")
    sh_data = inject_inherited_attributes(sh_data, report)

    # ── Save corrected files ─────────────────────────────────
    print("\nSaving corrected files...")
    if se_data:  save_json(SE_FILE, se_data);  print(f"  ✓ {SE_FILE}")
    if sh_data:  save_json(SH_FILE, sh_data);  print(f"  ✓ {SH_FILE}")
    if sew_data: save_json(SEW_FILE, sew_data); print(f"  ✓ {SEW_FILE}")
    if srr_data: save_json(SRR_FILE, srr_data); print(f"  ✓ {SRR_FILE}")
    if sr_data:  save_json(SR_FILE, sr_data);  print(f"  ✓ {SR_FILE}")

    save_json(CORRECTION_REPORT, report)

    # ── Summary ──────────────────────────────────────────────
    total_fixes = sum(len(v) for v in report.values())
    print(f"\n{'=' * 56}")
    print("  PHASE 6b COMPLETE")
    print(f"{'=' * 56}")
    print(f"  Total corrections : {total_fixes}")
    for kind in ("data_prop_name","data_prop_range","obj_prop_name",
                 "sr_predicate","sr_direction_swap","data_prop_llm",
                 "obj_prop_llm","structural_fix","inheritance_fix"):
        count = sum(1 for fixes in report.values()
                    for f in fixes if f["kind"] == kind)
        if count:
            print(f"  {kind:25}: {count}")
    print(f"\n  Correction report → {CORRECTION_REPORT}\n")


if __name__ == "__main__":
    try:
        run_correction()
    except Exception as e:
        print(f"\nFatal error: {e}")
        import traceback
        traceback.print_exc()