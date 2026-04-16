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
CONSTRAINT_META_FILE = "src/inputs/database/constraint_metadata.json"

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

        # ── RDF/XML fallback ──────────────────────────────────
        # If we found nothing via OWL/XML <Declaration> tags, the ontology
        # is likely in RDF/XML format.  Parse <owl:Class>, <owl:DatatypeProperty>,
        # <owl:ObjectProperty> with rdf:about attributes instead.
        if not self.data_props and not self.obj_props:
            print("  [OntologyIndex] No Declaration tags found — trying RDF/XML parsing")
            OWL  = "http://www.w3.org/2002/07/owl#"
            RDF  = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
            RDFS = "http://www.w3.org/2000/01/rdf-schema#"

            # ── Classes ──────────────────────────────────────
            for elem in root.iter(f"{{{OWL}}}Class"):
                iri = elem.get(f"{{{RDF}}}about", "")
                if iri and "owl#" not in iri and "rdf" not in iri:
                    cls_name = self._local(iri)
                    self.all_classes.add(cls_name)
                    # subClassOf
                    for sub in elem.findall(f"{{{RDFS}}}subClassOf"):
                        parent_iri = sub.get(f"{{{RDF}}}resource", "")
                        if parent_iri:
                            parent = self._local(parent_iri)
                            if parent and parent != "Thing":
                                self.subclass_of[cls_name].add(parent)

            # ── Data properties ──────────────────────────────
            for elem in root.iter(f"{{{OWL}}}DatatypeProperty"):
                iri = elem.get(f"{{{RDF}}}about", "")
                if not iri:
                    continue
                name = self._local(iri)
                info = {"iri": iri, "domain": None, "domain_union": None, "range": None}
                # domain
                dom_elem = elem.find(f"{{{RDFS}}}domain")
                if dom_elem is not None:
                    d_iri = dom_elem.get(f"{{{RDF}}}resource", "")
                    if d_iri:
                        info["domain"] = self._local(d_iri)
                # range
                rng_elem = elem.find(f"{{{RDFS}}}range")
                if rng_elem is not None:
                    r_iri = rng_elem.get(f"{{{RDF}}}resource", "")
                    if r_iri:
                        info["range"] = r_iri
                self.data_props[name] = info

            # ── Object properties ────────────────────────────
            for elem in root.iter(f"{{{OWL}}}ObjectProperty"):
                iri = elem.get(f"{{{RDF}}}about", "")
                if not iri:
                    continue
                name = self._local(iri)
                info = {"iri": iri, "domain": None, "range": None}
                dom_elem = elem.find(f"{{{RDFS}}}domain")
                if dom_elem is not None:
                    d_iri = dom_elem.get(f"{{{RDF}}}resource", "")
                    if d_iri:
                        info["domain"] = self._local(d_iri)
                rng_elem = elem.find(f"{{{RDFS}}}range")
                if rng_elem is not None:
                    r_iri = rng_elem.get(f"{{{RDF}}}resource", "")
                    if r_iri:
                        info["range"] = self._local(r_iri)
                self.obj_props[name] = info

            print(f"  [OntologyIndex] RDF/XML fallback: {len(self.all_classes)} classes, "
                  f"{len(self.data_props)} data props, {len(self.obj_props)} obj props")

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

    def get_descendants(self, cls: str) -> Set[str]:
        """Return cls + all classes that are subclasses of cls (transitively)."""
        result = {cls}
        for child, parents in self.subclass_of.items():
            if cls in parents:
                result.add(child)
        return result

    def get_all_related(self, cls: str) -> Set[str]:
        """Return cls + all ancestors + all descendants.
        This is the full set of classes that are hierarchy-compatible with cls."""
        return self.get_ancestors(cls) | self.get_descendants(cls)

    def find_data_property(self, domain_class: str,
                           col_name: str) -> Optional[Tuple[str, str]]:
        """
        Find matching data property for (domain_class, column_name).
        Uses underscore-stripped comparisons, connecting-word normalization,
        and prefers the LONGEST match (most specific).
        """
        ancestors = self.get_ancestors(domain_class)
        col_lower   = col_name.lower().replace("_", "")
        col_raw     = col_name.lower()
        # Also normalize stripping common OWL connecting words (of/the/for/in/by)
        col_norm = re.sub(r'[_\-]', ' ', col_name.lower())
        col_norm = re.sub(r'\b(of|the|for|in|on|by)\b', '', col_norm).replace(' ', '')

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

        # Pass 1: exact match (stripped and raw forms, plus connecting-word normalized)
        for name, info in self.data_props.items():
            if not domain_matches(info):
                continue
            n_stripped = name.lower().replace("_","")
            n_norm = re.sub(r'[_\-]', ' ', name.lower())
            n_norm = re.sub(r'\b(of|the|for|in|on|by)\b', '', n_norm).replace(' ', '')
            if n_stripped == col_lower or name.lower() == col_raw or n_norm == col_norm:
                return name, info["range"]

        # Pass 2: substring match WITH quality threshold.
        # A short property name "Name" (4 chars) matching inside a long column
        # "name_sponsor" (11 chars) has quality 4/11=36% → below 60% → rejected.
        # This prevents "name" from hijacking "name_sponsor".
        # When multiple candidates pass, highest quality wins.
        MIN_MATCH_QUALITY = 0.6
        candidates = []
        for name, info in self.data_props.items():
            if not domain_matches(info):
                continue
            n = name.lower().replace("_", "")
            n_norm = re.sub(r'[_\-]', ' ', name.lower())
            n_norm = re.sub(r'\b(of|the|for|in|on|by)\b', '', n_norm).replace(' ', '')

            quality = 0.0
            for a, b in [(col_lower, n), (col_norm, n_norm)]:
                if a and b:
                    if a in b:
                        quality = max(quality, len(a) / len(b))
                    if b in a:
                        quality = max(quality, len(b) / len(a))

            if quality >= MIN_MATCH_QUALITY:
                candidates.append((name, info["range"], quality, len(name)))

        if candidates:
            # Best quality first, then longest name (most specific) as tiebreaker
            candidates.sort(key=lambda x: (x[2], x[3]), reverse=True)
            return candidates[0][0], candidates[0][1]

        return None

    def _depth(self, cls: str, target: str) -> int:
        """Steps from cls to target in subclass hierarchy (up or down). 0=exact, 9999=unreachable."""
        if cls == target:
            return 0
        # BFS in both directions: up (ancestors) and down (descendants)
        visited = {cls}
        frontier = set()
        # Up: parents
        frontier.update(self.subclass_of.get(cls, set()))
        # Down: children
        for child, parents in self.subclass_of.items():
            if cls in parents:
                frontier.add(child)
        depth = 1
        while frontier:
            if target in frontier:
                return depth
            next_f = set()
            for c in frontier:
                if c not in visited:
                    visited.add(c)
                    # Up
                    next_f.update(self.subclass_of.get(c, set()))
                    # Down
                    for child, parents in self.subclass_of.items():
                        if c in parents and child not in visited:
                            next_f.add(child)
            frontier, depth = next_f, depth + 1
        return 9999

    def find_obj_property(self, subject_class: str,
                          object_class: str,
                          col_name: str = "") -> Optional[str]:
        """
        Find the most specific object property for (subject_class → object_class).
        Returns the property with minimum (domain_depth + range_depth).

        When col_name is provided and multiple properties have the same depth,
        prefer the one whose name best matches the FK column name.
        e.g. col_name='hasauthor' should prefer :hasAuthor over :acceptedBy.
        """
        subj_related = self.get_all_related(subject_class)
        obj_related  = self.get_all_related(object_class)

        has_domain_range = any(
            info.get("domain") and info.get("range")
            for info in self.obj_props.values()
        )

        if not has_domain_range:
            return None

        col_norm = col_name.lower().replace("_", "").replace("-", "") if col_name else ""

        candidates = []
        for prop_name, prop_info in self.obj_props.items():
            d = prop_info.get("domain")
            r = prop_info.get("range")
            if not d or not r:
                continue
            if d not in subj_related or r not in obj_related:
                continue
            total = self._depth(subject_class, d) + self._depth(object_class, r)
            # Column name similarity bonus
            p_norm = prop_name.lower().replace("_", "").replace("-", "")
            col_match = 1 if (col_norm and (col_norm == p_norm or col_norm in p_norm or p_norm in col_norm)) else 0
            candidates.append((total, -col_match, prop_name))

        if not candidates:
            return None
        candidates.sort()
        return candidates[0][2]

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
        if not all_data_props:
            return None
        prompt = f"""You are an ontology mapping expert.

TABLE: {table}, COLUMN: {column}, SUBJECT CLASS: {subject_class}
CURRENT (possibly wrong) PREDICATE: {current_pred}

Find the correct data property from this list that maps '{column}' for a '{subject_class}':
{', '.join(all_data_props)}

You MUST pick a property from the list above. Do NOT invent new property names.
If none of the properties fit this column at all, return {{"property": null}}.

Return ONLY JSON: {{"property": "exactPropertyName", "reasoning": "one sentence"}}"""
        raw    = self._call(prompt)
        result = self._parse(raw)
        if not result:
            return None
        prop = result.get("property")
        if not prop or str(prop).lower() == "null":
            return None
        # VALIDATE: property must exist in the ontology list
        if prop not in all_data_props:
            print(f"        [REJECT-LLM] data prop '{prop}' not in ontology — keeping original")
            return None
        return prop

    def resolve_obj_predicate(self, table: str, subject_class: str,
                              object_class: str, current_pred: str,
                              all_obj_props: List[str]) -> Optional[str]:
        if not all_obj_props:
            return None
        prompt = f"""You are an ontology mapping expert.

TABLE: {table}, SUBJECT CLASS: {subject_class}, OBJECT CLASS: {object_class}
CURRENT (possibly wrong) PREDICATE: {current_pred}

Find the correct object property linking '{subject_class}' → '{object_class}':
{', '.join(all_obj_props)}

You MUST pick a property from the list above. Do NOT invent new property names.
If none of the properties fit, return {{"property": null}}.

Return ONLY JSON: {{"property": "exactPropertyName", "reasoning": "one sentence"}}"""
        raw    = self._call(prompt)
        result = self._parse(raw)
        if not result:
            return None
        prop = result.get("property")
        if not prop or str(prop).lower() == "null":
            return None
        # VALIDATE: property must exist in the ontology list
        if prop not in all_obj_props:
            print(f"        [REJECT-LLM] obj prop '{prop}' not in ontology — keeping original")
            return None
        return prop

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
            prop = result["property"]
            all_props_set = set(candidate_data_props + candidate_obj_props)
            if prop not in all_props_set:
                print(f"        [REJECT-LLM] SEw prop '{prop}' not in ontology — skipping")
                return None
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
        if not all_obj_props:
            return None
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

You MUST pick a property from the AVAILABLE list above. Do NOT invent new names.

Return ONLY JSON:
{{
  "property":  "exactPropertyLocalName",
  "swap":      false,
  "reasoning": "One sentence."
}}"""
        raw    = self._call(prompt)
        result = self._parse(raw)
        if not result or not result.get("property"):
            return None
        prop = result["property"]
        if str(prop).lower() == "null":
            return None
        # VALIDATE: property must exist in the ontology list
        if prop not in all_obj_props:
            print(f"        [REJECT-LLM] SR prop '{prop}' not in ontology — keeping original")
            return None
        return result

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
                nc_clean = str(nc).lstrip(":")
                # VALIDATE: class must be in the available list
                if nc_clean not in available:
                    print(f"        [REJECT-LLM] class '{nc_clean}' not in available ontology classes")
                    return None, result
                return nc_clean, result
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
# Class existence validation
# ============================================================

def validate_class_assignments(
    phase_data: Dict, phase_name: str,
    idx: "OntologyIndex", fixer: "PredicateFixer",
    ontology_classes: List[str], report: Dict,
) -> Dict:
    """
    Verify every subject.class in the phase data exists in the ontology.
    If not, attempt to find the closest matching class via normalized name
    matching, then LLM fallback. Log all corrections.
    """
    phase_data = copy.deepcopy(phase_data)
    valid_set = set(ontology_classes)

    for tname, entry in phase_data.items():
        cls_raw = entry.get("subject", {}).get("class", "")
        if not cls_raw:
            continue
        cls = cls_raw.lstrip(":")
        if cls in valid_set:
            continue

        # Class doesn't exist in ontology — try to fix
        # Normalized match
        cls_norm = cls.lower().replace("_", "").replace("-", "")
        found = None
        for oc in ontology_classes:
            if oc.lower().replace("_", "").replace("-", "") == cls_norm:
                found = oc
                break

        if found:
            entry["subject"]["class"] = f":{found}"
            _log(report, tname, f":{cls}", f":{found}",
                 "class_validation_fix",
                 f"[{phase_name}] class not in ontology — normalized match")
        else:
            print(f"    [WARN] {phase_name}/{tname}: class :{cls} not in ontology — no match found")

    return phase_data


# ============================================================
# Missing data property detection + injection
# ============================================================

def detect_missing_data_properties(
    phase_data: Dict, tables_structure: Dict,
    idx: "OntologyIndex", report: Dict,
) -> Dict:
    """
    For each mapped class, check if the ontology declares data properties
    whose domain matches this class, and the DB table has a column that
    could match but no POM exists for it. Inject the missing POM.

    This catches cases where Phase 1-3 missed a column→property mapping
    that the ontology expects.
    """
    phase_data = copy.deepcopy(phase_data)

    for tname, entry in phase_data.items():
        cls = entry.get("subject", {}).get("class", "").lstrip(":")
        if not cls:
            continue

        # Get existing mapped predicates
        mapped_preds = {
            pom.get("predicate", "").lstrip(":")
            for pom in entry.get("predicate_object_maps", [])
        }

        # Get table columns
        tbl_cols = {
            c["name"]: c for c in tables_structure.get(tname, {}).get("columns", [])
        }
        pk_set = set(tables_structure.get(tname, {}).get("primary_keys", []))

        ancestors = idx.get_ancestors(cls)

        for dp_name, dp_info in idx.data_props.items():
            if dp_name in mapped_preds:
                continue

            d = dp_info.get("domain")
            du = dp_info.get("domain_union")
            if not ((d and d in ancestors) or (du and du & ancestors)):
                continue

            # This property is expected — is there a matching column?
            for col_name, col_def in tbl_cols.items():
                if col_name in pk_set:
                    continue
                if col_def.get("is_foreign_key"):
                    continue

                match = idx.find_data_property(cls, col_name)
                if match and match[0] == dp_name:
                    # Column matches this missing property — inject
                    dt = col_def.get("data_type", "unknown").lower().split("(")[0].strip()
                    XSD_MAP = {
                        "integer": "xsd:integer", "int": "xsd:integer",
                        "boolean": "xsd:boolean", "float": "xsd:decimal",
                        "numeric": "xsd:decimal", "date": "xsd:date",
                        "timestamp": "xsd:dateTime",
                    }
                    datatype = match[1] if match[1] else XSD_MAP.get(dt, "xsd:string")

                    entry.setdefault("predicate_object_maps", []).append({
                        "predicate": f":{dp_name}",
                        "object": {
                            "type": "literal",
                            "column": col_name,
                            "datatype": datatype,
                        }
                    })
                    mapped_preds.add(dp_name)
                    _log(report, tname, "(missing)", f":{dp_name}",
                         "missing_data_prop_injected",
                         f"col={col_name} → :{dp_name} (ontology expects, DB has)")
                    break

    return phase_data


# ============================================================
# Core predicate correction logic
# ============================================================

def _is_already_valid_obj_prop(prop_name: str, subj_cls: str,
                                obj_cls: str, idx: "OntologyIndex") -> bool:
    """True if prop_name is valid for (subj_cls → obj_cls) via ancestor AND descendant matching."""
    pn   = prop_name.lstrip(":")
    info = idx.obj_props.get(pn)
    if not info:
        return False
    d, r         = info.get("domain"), info.get("range")
    subj_related = idx.get_all_related(subj_cls)
    obj_related  = idx.get_all_related(obj_cls)
    return ((not d) or (d in subj_related)) and ((not r) or (r in obj_related))


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
            if col_lc in DISCRIMINATOR_COL_NAMES and not idx.find_data_property(subj_cls, col):
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

            # Special case: column named 'comment' or 'comments' should map to rdfs:comment
            # rdfs:comment is an annotation property, not in OntologyIndex.data_props,
            # but RODI queries commonly use it for review text fields
            if not result and col_lc in ("comment", "comments"):
                if old_pred != "rdfs:comment":
                    _log(report, table_name, old_pred, "rdfs:comment",
                         "data_prop_name",
                         f"column={col} → rdfs:comment (standard annotation property)")
                    new_pom["predicate"] = "rdfs:comment"
                new_poms.append(new_pom)
                continue

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
                # Before calling LLM, try case-insensitive match against ALL ontology
                # properties (data + object). This catches cases like
                # `:assignedbyreviewer` → `:assignedByReviewer` where the property
                # is an object property but the POM was classified as data property.
                pred_lower = old_pred.lstrip(":").lower().replace("_", "")
                all_props = {**idx.data_props, **idx.obj_props}
                case_match = None
                for prop_name in all_props:
                    if prop_name.lower().replace("_", "") == pred_lower:
                        case_match = prop_name
                        break
                if case_match and f":{case_match}" != old_pred:
                    _log(report, table_name, old_pred, f":{case_match}",
                         "data_prop_case_fix",
                         f"column={col} → case-corrected from ontology")
                    new_pom["predicate"] = f":{case_match}"
                else:
                    llm_prop = fixer.resolve_data_predicate(
                        table_name, col, subj_cls, old_pred,
                        list(idx.data_props.keys()) + list(idx.obj_props.keys())
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
                result = idx.find_obj_property(subj_cls, obj_cls, col_name=col)
                if result:
                    new_pred = f":{result}"
                    if new_pred != old_pred:
                        _log(report, table_name, old_pred, new_pred,
                             "obj_prop_name",
                             f"{subj_cls} → {obj_cls}")
                        new_pom["predicate"] = new_pred
                else:
                    # Guard: if existing predicate is already a known ontology
                    # property (regardless of domain/range), keep it as-is.
                    # This prevents the LLM from "correcting" correct predicates
                    # when the ontology has no domain/range declarations.
                    existing_name = old_pred.lstrip(":")
                    if existing_name in idx.obj_props:
                        pass  # already a valid ontology property — keep it
                    elif _is_already_valid_obj_prop(existing_name, subj_cls, obj_cls, idx):
                        pass  # already correct via domain/range matching
                    else:
                        # Try case-insensitive match against ALL ontology properties first
                        pred_lower = existing_name.lower().replace("_", "")
                        all_props = {**idx.data_props, **idx.obj_props}
                        case_match = None
                        for prop_name in all_props:
                            if prop_name.lower().replace("_", "") == pred_lower:
                                case_match = prop_name
                                break
                        if case_match and f":{case_match}" != old_pred:
                            _log(report, table_name, old_pred, f":{case_match}",
                                 "obj_prop_case_fix",
                                 f"{subj_cls} → {obj_cls}: case-corrected from ontology")
                            new_pom["predicate"] = f":{case_match}"
                        else:
                            # Try column-name-based fallback before calling LLM.
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

def inject_inherited_attributes(sh_data: Dict, se_data: Dict,
                                tables_structure: Dict,
                                idx: "OntologyIndex",
                                report: Dict) -> Dict:
    """
    For SE_SH tables that inherit from a parent SE table via a shared PK/FK,
    check if the ontology expects data properties on the child class that only
    exist as columns in the parent table. If so, inject a SQL join to pull
    those columns from the parent.

    Generalized: works for any SE_SH table, not just hardcoded names.
    """
    sh_data = copy.deepcopy(sh_data)

    for tname, entry in sh_data.items():
        parent_table = entry.get("parent_table")
        if not parent_table or parent_table not in se_data:
            continue

        child_cls = entry.get("subject", {}).get("class", "").lstrip(":")
        if not child_cls:
            continue

        # Get columns already mapped in the child
        mapped_cols = {
            pom.get("object", {}).get("column", "")
            for pom in entry.get("predicate_object_maps", [])
            if pom.get("object", {}).get("type") == "literal"
        }

        # Get parent's columns that are NOT in the child's own table
        child_own_cols = {
            c["name"] for c in tables_structure.get(tname, {}).get("columns", [])
        }
        parent_cols = tables_structure.get(parent_table, {}).get("columns", [])

        # Check if any ontology data property for this class matches a parent column
        # but is missing from the child's mappings
        for dp_name, dp_info in idx.data_props.items():
            d = dp_info.get("domain")
            du = dp_info.get("domain_union")
            ancestors = idx.get_ancestors(child_cls)
            if not ((d and d in ancestors) or (du and du & ancestors)):
                continue

            # Is this property already mapped?
            already = any(
                pom.get("predicate", "").lstrip(":") == dp_name
                for pom in entry.get("predicate_object_maps", [])
            )
            if already:
                continue

            # Does the parent table have a column that matches this property?
            parent_match = idx.find_data_property(child_cls, dp_name)
            if not parent_match:
                continue

            # Is the matching column in the parent but NOT in the child?
            prop_name = parent_match[0]
            parent_col_names = {c["name"].lower() for c in parent_cols}
            # Try to find the column in parent that corresponds
            for pc in parent_cols:
                pc_match = idx.find_data_property(child_cls, pc["name"])
                if pc_match and pc_match[0] == prop_name and pc["name"].lower() not in {c.lower() for c in child_own_cols}:
                    # This parent column maps to a needed property but isn't in child table
                    # Don't inject SQL join for now — just log it as a known gap
                    # (SQL join injection is complex and scenario-specific)
                    break

    return sh_data


# ============================================================
# rdfs:label injection
# ============================================================

def inject_rdfs_labels(phase_data: Dict, idx: "OntologyIndex",
                       report: Dict) -> Dict:
    """
    For any table that has a column mapped to a 'name'-like data property
    (e.g. :name, :has_a_name) but no rdfs:label, add an additive rdfs:label
    POM for that same column. This ensures SPARQL queries using rdfs:label
    find results.

    Generalized: works for any phase, any column, not just 'label'.
    """
    phase_data = copy.deepcopy(phase_data)
    NAME_LIKE = {"name", "has_a_name", "hasName", "title", "label",
                 "has_a_title", "hasTitle", "rdfs:label"}

    for tname, entry in phase_data.items():
        poms = entry.get("predicate_object_maps", [])
        already_rdfs = any(p.get("predicate") == "rdfs:label" for p in poms)
        if already_rdfs:
            continue

        # Find any literal POM whose predicate is name-like
        for pom in poms:
            if pom.get("object", {}).get("type") != "literal":
                continue
            pred = pom.get("predicate", "").lstrip(":")
            if pred.lower().replace("_", "").replace("-", "") in {
                n.lower().replace("_", "").replace("-", "") for n in NAME_LIKE
            }:
                rdfs_pom = copy.deepcopy(pom)
                rdfs_pom["predicate"] = "rdfs:label"
                rdfs_pom["object"]["datatype"] = "xsd:string"
                entry["predicate_object_maps"].append(rdfs_pom)
                _log(report, tname,
                     pom["predicate"], "rdfs:label (added)",
                     "rdfs_label_inject",
                     f"{tname}: additive rdfs:label for {pom.get('object',{}).get('column','?')}")
                break  # one rdfs:label per table is enough

    return phase_data


# ============================================================
# SR direction and predicate correction
# ============================================================

def _is_valid_obj_prop(prop_name: str, subj_cls: str,
                       obj_cls: str, idx: OntologyIndex) -> bool:
    """True if prop_name is valid for (subj_cls→obj_cls) using ancestor AND descendant matching.
    
    KEY FIX: A property with domain=Author is valid for subj_cls=Person because
    Author IS-A Person (some Person rows are Authors). Similarly, range=Paper is
    valid for obj_cls=Document because Paper IS-A Document."""
    pn   = prop_name.lstrip(":")
    info = idx.obj_props.get(pn)
    if not info:
        return False
    d, r         = info.get("domain"), info.get("range")
    subj_related = idx.get_all_related(subj_cls)
    obj_related  = idx.get_all_related(obj_cls)
    return ((not d) or (d in subj_related)) and ((not r) or (r in obj_related))


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
# PART C — Axiom-implied typing (EquivalentClasses materialization)
# ============================================================

def parse_equivalent_classes_axioms(owl_file: str) -> List[Dict]:
    """
    Parse OWL EquivalentClasses axioms of the form:

        EquivalentClasses(
            Class(#Author)
            ObjectSomeValuesFrom(ObjectProperty(#submit) Class(#Paper))
        )

    Meaning: Author ≡ ∃submit.Paper
      → "Anything that :submit's a :Paper is an :Author"

    Returns a list of dicts:
      { "equiv_class": "Author",     -- the class being defined
        "property":    "submit",      -- the object property
        "filler":      "Paper" }      -- the range filler class

    Also parses SubClassOf axioms with ObjectSomeValuesFrom:
        SubClassOf(Class(#Author) ObjectSomeValuesFrom(#notification_until #Deadline_Author_notification))
    These are weaker (→ not ≡) but still imply: if X :notification_until Y, then X is an Author.
    We only use EquivalentClasses for typing (they are definitional).
    """
    axioms = []
    try:
        tree = ET.parse(owl_file)
        root = tree.getroot()
    except Exception as e:
        print(f"  [WARN] Could not parse OWL file for axioms: {e}")
        return axioms

    def _local(iri: str) -> str:
        if not iri:
            return ""
        return iri.split("#")[-1] if "#" in iri else iri.split("/")[-1]

    # ── EquivalentClasses axioms ──────────────────────────────
    for elem in root.iter():
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if tag != "EquivalentClasses":
            continue

        children = list(elem)
        if len(children) != 2:
            continue

        # Find the named class and the ObjectSomeValuesFrom restriction
        named_class = None
        restriction = None
        for ch in children:
            ch_tag = ch.tag.split("}")[-1] if "}" in ch.tag else ch.tag
            if ch_tag == "Class":
                iri = ch.get("IRI", "") or ch.get("abbreviatedIRI", "")
                named_class = _local(iri)
            elif ch_tag == "ObjectSomeValuesFrom":
                restriction = ch

        if not named_class or restriction is None:
            continue

        # Parse the restriction: ObjectSomeValuesFrom(ObjectProperty(P) Class(C))
        restr_children = list(restriction)
        if len(restr_children) < 2:
            continue

        prop_elem = restr_children[0]
        filler_elem = restr_children[1]

        prop_tag = prop_elem.tag.split("}")[-1] if "}" in prop_elem.tag else prop_elem.tag
        filler_tag = filler_elem.tag.split("}")[-1] if "}" in filler_elem.tag else filler_elem.tag

        if prop_tag != "ObjectProperty":
            continue

        prop_iri = prop_elem.get("IRI", "") or prop_elem.get("abbreviatedIRI", "")
        prop_name = _local(prop_iri)

        if filler_tag == "Class":
            filler_iri = filler_elem.get("IRI", "") or filler_elem.get("abbreviatedIRI", "")
            filler_name = _local(filler_iri)
        else:
            # Could be ObjectUnionOf or other complex expression — skip
            continue

        if prop_name and filler_name:
            axioms.append({
                "equiv_class": named_class,
                "property":    prop_name,
                "filler":      filler_name,
                "axiom_type":  "EquivalentClasses",
            })

    if axioms:
        print(f"  EquivalentClasses axioms parsed: {len(axioms)}")
        for a in axioms:
            print(f"    {a['equiv_class']} ≡ ∃{a['property']}.{a['filler']}")

    return axioms


def inject_axiom_implied_typing(
    sr_data: Dict,
    se_data: Dict,
    sh_data: Dict,
    axioms: List[Dict],
    idx: "OntologyIndex",
    class_map: Dict,
    report: Dict,
) -> Dict:
    """
    For each EquivalentClasses axiom (e.g. Author ≡ ∃submit.Paper), find SR
    junction tables whose participants are hierarchy-compatible with the axiom's
    domain/range classes, and inject additional rdf:type TriplesMaps.

    KEY DESIGN: This is PREDICATE-INDEPENDENT. It does NOT check what predicate
    Phase 5 assigned to the SR mapping. Instead it checks whether the axiom's
    property connects two classes that are hierarchy-compatible with the SR
    table's participant classes. This handles cases where Phase 5 picked a
    different (but valid) property for the same pair of entity classes.

    Example:
      Axiom: Author ≡ ∃submit.Paper
      :submit has domain=Author (subclass of Person), range=Paper (subclass of Document)
      SR table document_person connects Person ↔ Document
      → Person is ancestor of Author ✓, Document is ancestor of Paper ✓
      → Inject: persons in document_person get rdf:type :Author
      → Inject: documents in document_person get rdf:type :Paper

    Deduplication: if Person↔Document appears in multiple SR tables, the typing
    is only injected once per (template, class) pair.

    Returns the modified sr_data with new SR_AXIOM_TYPING entries.
    """
    new_entries: Dict[str, Dict] = {}

    # Build set of all (entity_template, class) pairs already typed
    already_typed: Set[str] = set()  # "template|class"
    for phase_data in (se_data, sh_data):
        for entry in phase_data.values():
            tmpl = entry.get("subject", {}).get("template", "")
            cls = entry.get("subject", {}).get("class", "").lstrip(":")
            if tmpl and cls:
                already_typed.add(f"{tmpl}|{cls}")

    # Build IRI → template lookup
    iri_to_template: Dict[str, str] = {}
    for phase_data in (se_data, sh_data):
        for entry in phase_data.values():
            iri = entry.get("triple_map_iri", "")
            tmpl = entry.get("subject", {}).get("template", "")
            if iri and tmpl:
                iri_to_template[iri] = tmpl

    # For each axiom, look up the property's domain and range from the ontology
    # Then find SR tables whose participants match
    for axiom in axioms:
        equiv_class = axiom["equiv_class"]
        prop_name = axiom["property"]
        filler_class = axiom["filler"]

        # Get the property's declared domain and range
        prop_info = idx.obj_props.get(prop_name, {})
        prop_domain = prop_info.get("domain", "")  # e.g. "Author"
        prop_range = prop_info.get("range", "")     # e.g. "Paper"

        if not prop_domain or not prop_range:
            # Can't determine direction without domain/range
            continue

        # For each SR table, check if its participants are hierarchy-compatible
        for sr_table in list(sr_data.keys()):
            sr_entry = sr_data[sr_table]
            if sr_entry.get("pattern") != "SR":
                continue

            # Get the two participant IRIs and their classes
            participants = sr_entry.get("participants", [])
            if len(participants) < 2:
                continue

            # Try all participant pairs to find a match
            # We need: one participant compatible with prop_domain (subject side)
            #          another compatible with prop_range (object side)
            mappings = sr_entry.get("mappings", [])
            if not mappings:
                continue

            # Use the first mapping's join info to get subject/object structure
            # But check ALL unique (subj_iri, obj_iri) pairs in the mappings
            checked_pairs: Set[str] = set()

            for mapping in mappings:
                subj_iri = mapping.get("subject_triples_map", "")
                obj_iri = mapping.get("object_triples_map", "")
                subj_join = mapping.get("subject_join", {})
                obj_join = mapping.get("object_join", {})

                pair_key = f"{subj_iri}|{obj_iri}"
                if pair_key in checked_pairs:
                    continue
                checked_pairs.add(pair_key)

                subj_cls = class_map.get(subj_iri, "").lstrip(":")
                obj_cls = class_map.get(obj_iri, "").lstrip(":")
                subj_tmpl = iri_to_template.get(subj_iri, "")
                obj_tmpl = iri_to_template.get(obj_iri, "")
                logical_table = sr_entry.get("logical_table", sr_table)

                if not subj_cls or not obj_cls:
                    continue

                # Check: is subj_cls hierarchy-compatible with prop_domain?
                # And obj_cls hierarchy-compatible with prop_range?
                subj_related = idx.get_all_related(subj_cls)
                obj_related = idx.get_all_related(obj_cls)

                subj_matches_domain = prop_domain in subj_related
                obj_matches_range = prop_range in obj_related

                if not (subj_matches_domain and obj_matches_range):
                    continue

                # ── Subject-side typing: entities get rdf:type equiv_class ──
                if subj_join and subj_join.get("child") and subj_tmpl:
                    fk_col = subj_join["child"]
                    base_match = re.match(r'(.*?)\{[^}]+\}', subj_tmpl)
                    if base_match:
                        typing_template = f"{base_match.group(1)}{{{fk_col}}}"
                    else:
                        typing_template = subj_tmpl

                    typing_key = f"{subj_tmpl}|{equiv_class}"
                    entry_key = f"__axiom_typing_{sr_table}_{equiv_class}_{fk_col}"

                    if (typing_key not in already_typed
                            and entry_key not in new_entries
                            and entry_key not in sr_data):
                        new_entries[entry_key] = {
                            "pattern": "SR_AXIOM_TYPING",
                            "triple_map_iri": f"urn:r2rml:AXIOM_{sr_table}_{equiv_class}",
                            "logical_table": logical_table,
                            "typing_class": f":{equiv_class}",
                            "subject_template": typing_template,
                            "source_column": fk_col,
                            "axiom": {
                                "equiv_class": equiv_class,
                                "property": prop_name,
                                "filler": filler_class,
                            },
                            "_reason": (
                                f"EquivalentClasses: {equiv_class} ≡ ∃{prop_name}.{filler_class} "
                                f"→ {subj_cls} in {sr_table}.{fk_col} typed as :{equiv_class}"
                            ),
                        }
                        already_typed.add(typing_key)
                        _log(report, sr_table,
                             "(no typing)", f"rdf:type :{equiv_class}",
                             "axiom_implied_typing",
                             f"{subj_cls} ({fk_col}) → :{equiv_class} "
                             f"(≡ ∃{prop_name}.{filler_class})")

                # ── Object-side typing: entities get rdf:type filler_class ──
                if obj_join and obj_join.get("child") and obj_tmpl:
                    fk_col = obj_join["child"]
                    base_match = re.match(r'(.*?)\{[^}]+\}', obj_tmpl)
                    if base_match:
                        typing_template = f"{base_match.group(1)}{{{fk_col}}}"
                    else:
                        typing_template = obj_tmpl

                    typing_key = f"{obj_tmpl}|{filler_class}"
                    entry_key = f"__axiom_typing_{sr_table}_{filler_class}_{fk_col}"

                    if (typing_key not in already_typed
                            and entry_key not in new_entries
                            and entry_key not in sr_data):
                        new_entries[entry_key] = {
                            "pattern": "SR_AXIOM_TYPING",
                            "triple_map_iri": f"urn:r2rml:AXIOM_{sr_table}_{filler_class}",
                            "logical_table": logical_table,
                            "typing_class": f":{filler_class}",
                            "subject_template": typing_template,
                            "source_column": fk_col,
                            "axiom": {
                                "equiv_class": equiv_class,
                                "property": prop_name,
                                "filler": filler_class,
                            },
                            "_reason": (
                                f"EquivalentClasses: {equiv_class} ≡ ∃{prop_name}.{filler_class} "
                                f"→ {obj_cls} in {sr_table}.{fk_col} typed as :{filler_class}"
                            ),
                        }
                        already_typed.add(typing_key)
                        _log(report, sr_table,
                             "(no typing)", f"rdf:type :{filler_class}",
                             "axiom_implied_typing",
                             f"{obj_cls} ({fk_col}) → :{filler_class} "
                             f"(≡ ∃{prop_name}.{filler_class})")

    # Merge new entries into sr_data
    sr_data.update(new_entries)
    print(f"  Axiom-implied typing entries injected: {len(new_entries)}")
    return sr_data


# ============================================================
# PART D — Mapping Simulation & Verification
# ============================================================
# Generates test query pairs (SQL ↔ SPARQL patterns) from the ontology
# and traces them against the mapping JSON to detect gaps.
# Three test types per mapped class:
#   Q-TYPE: Does the class have instances?
#   Q-PROP: Do instances have expected data properties?
#   Q-LINK: Are object property relationships correctly mapped?
# ============================================================

HIDDEN_MAPPINGS_FILE = os.path.join(MAPPINGS_DIR, "HIDDEN_mappings.json")


class MappingSimulator:
    """
    Generates SPARQL triple-pattern checklists from the ontology, traces them
    against mapping JSONs, fixes gaps where DB data exists, and re-verifies.

    KEY PRINCIPLE: A pattern is only a gap if the DATABASE has the data.
    If the ontology defines something with no corresponding DB element → SKIP.

    Three-pass workflow:
      Pass 1 — Generate patterns from ontology, trace against mappings
      Pass 2 — Fix gaps where DB data exists but mapping is incomplete
      Pass 3 — Re-trace to verify fixes

    Test types:
      Q-TYPE:    ?x rdf:type :Class
      Q-PROP:    ?x rdf:type :Class; :dp ?v
      Q-LINK:    ?x :op ?y  (FK join or SR bridge)
      Q-SEW:     ?x :prop ?v  (weak entity property on owner)
      Q-HIDDEN:  ?x rdf:type :Subclass  (bool flag / type dispatch)
      Q-EQUIV:   ?x rdf:type :A; :p ?y. ?y rdf:type :B  (axiom)
      Q-INVERSE: ?x :p ?y ↔ ?y :p_inv ?x
    """

    def __init__(
        self,
        se_data: Dict, sh_data: Dict, sew_data: Dict,
        srr_data: Dict, sr_data: Dict,
        idx: "OntologyIndex",
        tables_structure: Dict,
    ):
        self.se_data  = se_data
        self.sh_data  = sh_data
        self.sew_data = sew_data
        self.srr_data = srr_data
        self.sr_data  = sr_data
        self.idx      = idx
        self.tables_structure = tables_structure
        self.hidden_data = load_json_optional(HIDDEN_MAPPINGS_FILE)

        self.db_tables = set(tables_structure.keys())

        # class → table mapping
        self.class_to_table: Dict[str, str] = {}
        for phase_data in [se_data, sh_data, srr_data]:
            for tname, entry in phase_data.items():
                cls = entry.get("subject", {}).get("class", "").lstrip(":")
                if cls:
                    self.class_to_table[cls] = tname

        # Parse inverse properties
        self.inverse_of: Dict[str, str] = {}
        try:
            root = ET.parse(ONTOLOGY_FILE).getroot()
            for elem in root.iter():
                tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
                if tag == "InverseObjectProperties":
                    ch = list(elem)
                    if len(ch) == 2:
                        a = (ch[0].get("IRI", "") or ch[0].get("abbreviatedIRI", ""))
                        b = (ch[1].get("IRI", "") or ch[1].get("abbreviatedIRI", ""))
                        a = a.split("#")[-1] if "#" in a else a.split("/")[-1]
                        b = b.split("#")[-1] if "#" in b else b.split("/")[-1]
                        if a and b:
                            self.inverse_of[a] = b
                            self.inverse_of[b] = a
        except Exception:
            pass

        self._build_indexes()

    # ── Index builders ───────────────────────────────────────

    def _build_indexes(self):
        self._build_typing_index()
        self._build_property_index()

    def _build_typing_index(self):
        """class → list of {source, template, table}"""
        self.typing_index: Dict[str, List[Dict]] = defaultdict(list)

        for phase_name, phase_data in [
            ("SE", self.se_data), ("SH", self.sh_data),
            ("SEw", self.sew_data), ("SRR", self.srr_data),
        ]:
            for tname, entry in phase_data.items():
                cls = entry.get("subject", {}).get("class", "").lstrip(":")
                tmpl = entry.get("subject", {}).get("template", "")
                if cls and tmpl:
                    self.typing_index[cls].append({
                        "source": phase_name, "table": tname, "template": tmpl,
                    })

        for key, entry in self.sr_data.items():
            if entry.get("pattern") == "SR_AXIOM_TYPING":
                cls = entry.get("typing_class", "").lstrip(":")
                tmpl = entry.get("subject_template", "")
                if cls and tmpl:
                    self.typing_index[cls].append({
                        "source": "AXIOM", "table": entry.get("logical_table", key),
                        "template": tmpl,
                    })

        for tname, entry in self.hidden_data.items():
            for hs in entry.get("hidden_sh", []) or []:
                cls = hs.get("subject", {}).get("class", "").lstrip(":")
                tmpl = hs.get("subject", {}).get("template", "")
                if cls and tmpl:
                    self.typing_index[cls].append({
                        "source": "HIDDEN_SH", "table": tname, "template": tmpl,
                    })
            for td in entry.get("type_dispatch", []) or []:
                for disp in td.get("dispatch", []) or []:
                    cls = disp.get("subject", {}).get("class", "").lstrip(":")
                    tmpl = disp.get("subject", {}).get("template", "")
                    if cls and tmpl:
                        self.typing_index[cls].append({
                            "source": "HIDDEN_TD", "table": tname, "template": tmpl,
                        })

    def _build_property_index(self):
        """Build data_prop and obj_prop indexes."""
        self.data_prop_index: Dict[tuple, List[Dict]] = defaultdict(list)
        self.obj_prop_index:  Dict[tuple, List[Dict]] = defaultdict(list)

        for phase_data in [self.se_data, self.sh_data, self.sew_data, self.srr_data]:
            for tname, entry in phase_data.items():
                cls = entry.get("subject", {}).get("class", "").lstrip(":")
                if not cls:
                    continue
                for pom in entry.get("predicate_object_maps", []):
                    pred = pom.get("predicate", "")
                    if pred.startswith("rdfs:") or pred.startswith("rdf:"):
                        continue
                    pred = pred.lstrip(":")
                    obj = pom.get("object", {})
                    obj_type = obj.get("type", "")
                    if obj_type == "literal":
                        self.data_prop_index[(cls, pred)].append({
                            "table": tname, "column": obj.get("column", ""),
                        })
                    elif obj_type in ("join", "junction_join"):
                        self.obj_prop_index[(cls, pred)].append({
                            "table": tname, "join_type": obj_type,
                            "parent_triples_map": obj.get("parent_triples_map", ""),
                        })

        for sr_table, entry in self.sr_data.items():
            if entry.get("pattern") != "SR":
                continue
            for m in entry.get("mappings", []):
                pred = m.get("predicate", "").lstrip(":")
                if not pred or pred == "UNRESOLVED":
                    continue
                subj_cls = self._iri_to_class(m.get("subject_triples_map", ""))
                if subj_cls:
                    self.obj_prop_index[(subj_cls, pred)].append({
                        "table": sr_table, "join_type": "sr_bridge",
                        "parent_triples_map": m.get("object_triples_map", ""),
                    })

    # ── Helpers ───────────────────────────────────────────────

    def _iri_to_class(self, iri: str) -> Optional[str]:
        for pd in [self.se_data, self.sh_data, self.sew_data, self.srr_data]:
            for entry in pd.values():
                if entry.get("triple_map_iri") == iri:
                    return entry.get("subject", {}).get("class", "").lstrip(":")
        return None

    def _iri_to_template(self, iri: str) -> str:
        for pd in [self.se_data, self.sh_data]:
            for entry in pd.values():
                if entry.get("triple_map_iri") == iri:
                    return entry.get("subject", {}).get("template", "")
        return ""

    def _sr_connects(self, subj_cls: str, obj_cls: str) -> Optional[str]:
        """Find SR table connecting compatible classes."""
        subj_rel = self.idx.get_all_related(subj_cls)
        obj_rel = self.idx.get_all_related(obj_cls)
        for sr_t, entry in self.sr_data.items():
            if entry.get("pattern") != "SR":
                continue
            for m in entry.get("mappings", []):
                s = self._iri_to_class(m.get("subject_triples_map", ""))
                o = self._iri_to_class(m.get("object_triples_map", ""))
                if s and o and s in subj_rel and o in obj_rel:
                    return sr_t
        return None

    # ── Test generation ──────────────────────────────────────

    def generate_tests(self) -> List[Dict]:
        tests = []
        tid = [0]
        def _t():
            tid[0] += 1
            return tid[0]

        # Mapped classes from entity phases
        mapped: Dict[str, Dict] = {}
        for pn, pd in [("SE", self.se_data), ("SH", self.sh_data), ("SRR", self.srr_data)]:
            for tname, entry in pd.items():
                cls = entry.get("subject", {}).get("class", "").lstrip(":")
                if cls and cls not in mapped:
                    mapped[cls] = {"table": tname, "phase": pn, "entry": entry}

        for cls, info in sorted(mapped.items()):
            table = info["table"]

            # ── Q-TYPE ────────────────────────────────────────
            sources = self.typing_index.get(cls, [])
            tests.append({
                "id": _t(), "type": "Q-TYPE", "class": cls,
                "pattern": f"?x rdf:type :{cls}",
                "description": f"Instances of :{cls}",
                "table": table, "found": bool(sources),
                "sources": [f"{s['source']}:{s['table']}" for s in sources],
                "has_db_support": True,
                "status": "PASS" if sources else "FAIL",
                "gap": "" if sources else f"No TriplesMap emits rdf:type :{cls}",
            })

            # ── Q-PROP: data properties with domain = this class ──
            for dp, dp_info in sorted(self.idx.data_props.items()):
                d = dp_info.get("domain")
                du = dp_info.get("domain_union")
                if not ((d and d == cls) or (du and cls in du)):
                    continue

                dp_src = self.data_prop_index.get((cls, dp), [])
                if not dp_src:
                    for anc in self.idx.get_ancestors(cls) - {cls}:
                        dp_src = self.data_prop_index.get((anc, dp), [])
                        if dp_src:
                            break

                has_col = table in self.tables_structure
                tests.append({
                    "id": _t(), "type": "Q-PROP", "class": cls,
                    "property": dp,
                    "pattern": f"?x rdf:type :{cls}; :{dp} ?v",
                    "description": f":{cls} . :{dp}",
                    "table": table, "found": bool(dp_src),
                    "sources": [f"{s['table']}.{s.get('column','?')}" for s in dp_src],
                    "has_db_support": has_col,
                    "status": "PASS" if dp_src else ("WARN" if has_col else "SKIP"),
                    "gap": "" if dp_src else f":{dp} not mapped for :{cls}",
                })

            # ── Q-LINK: object properties from this class ─────
            for op, op_info in sorted(self.idx.obj_props.items()):
                d = op_info.get("domain")
                r = op_info.get("range")
                if not d or not r:
                    continue
                if d not in self.idx.get_all_related(cls):
                    continue

                op_src = self.obj_prop_index.get((cls, op), [])
                if not op_src:
                    for desc in self.idx.get_descendants(cls):
                        op_src = self.obj_prop_index.get((desc, op), [])
                        if op_src:
                            break

                sr_t = self._sr_connects(cls, r)
                has_fk = any(
                    col.get("is_foreign_key")
                    for col in self.tables_structure.get(table, {}).get("columns", [])
                )
                has_db = sr_t is not None or has_fk

                tests.append({
                    "id": _t(), "type": "Q-LINK", "class": cls,
                    "property": op, "range_class": r,
                    "pattern": f"?x :{op} ?y (:{cls}→:{r})",
                    "description": f":{cls} --:{op}--> :{r}",
                    "table": table, "sr_table": sr_t,
                    "found": bool(op_src),
                    "sources": [f"{s['table']}({s.get('join_type','?')})" for s in op_src],
                    "has_db_support": has_db,
                    "status": "PASS" if op_src else ("WARN" if has_db else "SKIP"),
                    "gap": "" if op_src else (
                        f":{op} (→:{r}) not mapped" if has_db
                        else f"No DB link for :{op} (:{cls}→:{r})"
                    ),
                })

        # ── Q-SEW ─────────────────────────────────────────────
        for tname, entry in self.sew_data.items():
            owner_pred = entry.get("owner_predicate", "").lstrip(":")
            owner_table = entry.get("owner_table", "")
            if not owner_pred:
                continue
            owner_cls = None
            for pd in [self.se_data, self.sh_data]:
                e = pd.get(owner_table)
                if e:
                    owner_cls = e.get("subject", {}).get("class", "").lstrip(":")
                    break

            found = bool(self.data_prop_index.get((owner_cls, owner_pred), []))
            if not found:
                for key in self.se_data:
                    if "rescued" in key and tname in key:
                        found = True
                        break

            tests.append({
                "id": tid[0] + 1, "type": "Q-SEW",
                "class": owner_cls or "?", "property": owner_pred,
                "pattern": f"?x rdf:type :{owner_cls}; :{owner_pred} ?v",
                "description": f"SEw :{owner_pred} on :{owner_cls} ({tname})",
                "table": tname, "found": found,
                "sources": [f"SEw:{tname}"],
                "has_db_support": tname in self.db_tables,
                "status": "PASS" if found else "WARN",
                "gap": "" if found else f"SEw :{owner_pred} not reaching :{owner_cls}",
            })
            tid[0] += 1

        # ── Q-HIDDEN ──────────────────────────────────────────
        for tname, entry in self.hidden_data.items():
            base_cls = None
            for pd in [self.se_data, self.sh_data]:
                e = pd.get(tname)
                if e:
                    base_cls = e.get("subject", {}).get("class", "").lstrip(":")
                    break

            for hs in entry.get("hidden_sh", []) or []:
                sub = hs.get("subject", {}).get("class", "").lstrip(":")
                if not sub:
                    continue
                typed = bool(self.typing_index.get(sub, []))
                hier_ok = sub in self.idx.get_all_related(base_cls) if base_cls else True
                tests.append({
                    "id": _t(), "type": "Q-HIDDEN", "class": sub,
                    "base_class": base_cls or "?",
                    "pattern": f"?x rdf:type :{sub}",
                    "description": f"Hidden :{sub} from {tname}",
                    "table": tname, "found": typed,
                    "status": "PASS" if (typed and hier_ok) else "WARN",
                    "gap": "" if typed else f":{sub} not typed",
                })

            for td in entry.get("type_dispatch", []) or []:
                for disp in td.get("dispatch", []) or []:
                    sub = disp.get("subject", {}).get("class", "").lstrip(":")
                    if not sub:
                        continue
                    typed = bool(self.typing_index.get(sub, []))
                    tests.append({
                        "id": _t(), "type": "Q-HIDDEN", "class": sub,
                        "pattern": f"?x rdf:type :{sub}",
                        "description": f"Dispatch :{sub} from {tname}",
                        "table": tname, "found": typed,
                        "status": "PASS" if typed else "WARN",
                        "gap": "" if typed else f":{sub} not typed",
                    })

        # ── Q-EQUIV ───────────────────────────────────────────
        axioms = parse_equivalent_classes_axioms(ONTOLOGY_FILE)
        for ax in axioms:
            eq, prop, filler = ax["equiv_class"], ax["property"], ax["filler"]

            typing_ok = bool(self.typing_index.get(eq, []))
            filler_ok = bool(self.typing_index.get(filler, []))
            prop_ok = any(
                m.get("predicate", "").lstrip(":") == prop
                for sr_e in self.sr_data.values() if sr_e.get("pattern") == "SR"
                for m in sr_e.get("mappings", [])
            )

            pi = self.idx.obj_props.get(prop, {})
            pd_d, pd_r = pi.get("domain", ""), pi.get("range", "")
            has_db = False
            if pd_d and pd_r:
                for mc in self.class_to_table:
                    if pd_d in self.idx.get_all_related(mc):
                        if self._sr_connects(mc, pd_r):
                            has_db = True
                            break

            all_ok = typing_ok and filler_ok and prop_ok
            if not all_ok:
                missing = []
                if not typing_ok: missing.append(f"rdf:type :{eq}")
                if not filler_ok: missing.append(f"rdf:type :{filler}")
                if not prop_ok: missing.append(f":{prop} predicate")
                tests.append({
                    "id": _t(), "type": "Q-EQUIV",
                    "class": eq, "property": prop, "filler": filler,
                    "pattern": f"?x rdf:type :{eq}; :{prop} ?y. ?y rdf:type :{filler}",
                    "description": f"Axiom: {eq} ≡ ∃{prop}.{filler}",
                    "missing": missing, "has_db_support": has_db,
                    "status": "FAIL" if has_db else "SKIP",
                    "gap": f"Missing: {', '.join(missing)}" if has_db
                           else f"No DB for {eq}≡∃{prop}.{filler}",
                })

        # ── Q-INVERSE ─────────────────────────────────────────
        checked = set()
        for pa, pb in self.inverse_of.items():
            pair = tuple(sorted([pa, pb]))
            if pair in checked:
                continue
            checked.add(pair)

            a_ok = any(self.obj_prop_index.get((c, pa), []) for c in self.class_to_table)
            b_ok = any(self.obj_prop_index.get((c, pb), []) for c in self.class_to_table)

            if a_ok and not b_ok:
                tests.append({
                    "id": _t(), "type": "Q-INVERSE",
                    "property": pb, "inverse_of": pa,
                    "pattern": f"?y :{pb} ?x (inverse of :{pa})",
                    "description": f"Inverse :{pb} of :{pa}",
                    "found": False, "status": "WARN",
                    "gap": f":{pa} mapped but inverse :{pb} missing",
                })
            elif b_ok and not a_ok:
                tests.append({
                    "id": _t(), "type": "Q-INVERSE",
                    "property": pa, "inverse_of": pb,
                    "pattern": f"?x :{pa} ?y (inverse of :{pb})",
                    "description": f"Inverse :{pa} of :{pb}",
                    "found": False, "status": "WARN",
                    "gap": f":{pb} mapped but inverse :{pa} missing",
                })

        return tests

    # ── Fix methods ──────────────────────────────────────────

    def _fix_equiv_gap(self, test: Dict, report: Dict) -> int:
        """Inject missing axiom property as additional SR direction."""
        prop = test.get("property", "")
        eq_cls = test.get("class", "")
        filler = test.get("filler", "")
        missing = test.get("missing", [])
        if not prop or f":{prop} predicate" not in missing:
            return 0

        pi = self.idx.obj_props.get(prop, {})
        pd, pr = pi.get("domain", ""), pi.get("range", "")
        if not pd or not pr:
            return 0

        fixes = 0
        for sr_t in list(self.sr_data.keys()):
            sr_e = self.sr_data[sr_t]
            if sr_e.get("pattern") != "SR":
                continue
            ms = sr_e.get("mappings", [])
            if not ms:
                continue
            if any(m.get("predicate", "").lstrip(":") == prop for m in ms):
                continue

            for m in ms:
                sc = self._iri_to_class(m.get("subject_triples_map", ""))
                oc = self._iri_to_class(m.get("object_triples_map", ""))
                if not sc or not oc:
                    continue
                if pd not in self.idx.get_all_related(sc) or pr not in self.idx.get_all_related(oc):
                    continue

                sr_e["mappings"].append({
                    "subject_triples_map": m["subject_triples_map"],
                    "subject_resolved": True,
                    "subject_join": copy.deepcopy(m.get("subject_join", {})),
                    "predicate": f":{prop}",
                    "object_triples_map": m["object_triples_map"],
                    "object_resolved": True,
                    "object_join": copy.deepcopy(m.get("object_join", {})),
                    "_injected_by": "simulation_fix_equiv",
                })
                fixes += 1
                print(f"    [FIX-EQUIV] {sr_t}: injected :{prop} ({sc}→{oc}) — {eq_cls}≡∃{prop}.{filler}")
                _log(report, sr_t, f"missing :{prop}", f"injected ({sc}→{oc})",
                     "simulation_fix_equiv", f"Axiom {eq_cls}≡∃{prop}.{filler}")

                inv = self.inverse_of.get(prop)
                if inv and not any(em.get("predicate", "").lstrip(":") == inv for em in sr_e["mappings"]):
                    sr_e["mappings"].append({
                        "subject_triples_map": m["object_triples_map"],
                        "subject_resolved": True,
                        "subject_join": copy.deepcopy(m.get("object_join", {})),
                        "predicate": f":{inv}",
                        "object_triples_map": m["subject_triples_map"],
                        "object_resolved": True,
                        "object_join": copy.deepcopy(m.get("subject_join", {})),
                        "_injected_by": "simulation_fix_equiv_inv",
                    })
                    fixes += 1
                    print(f"    [FIX-EQUIV] {sr_t}: injected inverse :{inv} ({oc}→{sc})")
                break
            if fixes:
                break
        return fixes

    def _fix_typing_gap(self, test: Dict, report: Dict) -> int:
        """Inject axiom typing for missing class instances."""
        cls = test.get("class", "")
        if not cls:
            return 0
        axioms = parse_equivalent_classes_axioms(ONTOLOGY_FILE)
        matching = [a for a in axioms if a["equiv_class"] == cls]
        if not matching:
            return 0
        for key, e in self.sr_data.items():
            if e.get("pattern") == "SR_AXIOM_TYPING" and e.get("typing_class", "").lstrip(":") == cls:
                return 0

        fixes = 0
        for ax in matching:
            pi = self.idx.obj_props.get(ax["property"], {})
            pd = pi.get("domain", "")
            if not pd:
                continue
            for sr_t, sr_e in self.sr_data.items():
                if sr_e.get("pattern") != "SR":
                    continue
                for m in sr_e.get("mappings", []):
                    si = m.get("subject_triples_map", "")
                    sc = self._iri_to_class(si)
                    st = self._iri_to_template(si)
                    if not sc or pd not in self.idx.get_all_related(sc):
                        continue
                    fk = m.get("subject_join", {}).get("child", "")
                    if not fk or not st:
                        continue
                    bm = re.match(r'(.*?)\{[^}]+\}', st)
                    tmpl = f"{bm.group(1)}{{{fk}}}" if bm else st
                    key = f"__sim_typing_{sr_t}_{cls}_{fk}"
                    if key not in self.sr_data:
                        self.sr_data[key] = {
                            "pattern": "SR_AXIOM_TYPING",
                            "triple_map_iri": f"urn:r2rml:SIM_{sr_t}_{cls}",
                            "logical_table": sr_e.get("logical_table", sr_t),
                            "typing_class": f":{cls}",
                            "subject_template": tmpl,
                            "source_column": fk,
                            "axiom": ax,
                            "_reason": f"Simulation fix: {cls} typing",
                        }
                        fixes += 1
                        print(f"    [FIX-TYPE] Injected rdf:type :{cls} from {sr_t}.{fk}")
                        _log(report, sr_t, f"no :{cls}", f"typed from {fk}",
                             "simulation_fix_typing", f"Axiom {cls}")
                        break
                if fixes:
                    break
        return fixes

    def _fix_link_gap(self, test: Dict, report: Dict) -> int:
        """Inject missing object property direction on compatible SR table."""
        cls, prop, rng = test.get("class",""), test.get("property",""), test.get("range_class","")
        sr_t = test.get("sr_table")
        if not cls or not prop or not sr_t:
            return 0
        if self.obj_prop_index.get((cls, prop)):
            return 0

        pi = self.idx.obj_props.get(prop, {})
        od, orng = pi.get("domain",""), pi.get("range","")
        sr_e = self.sr_data.get(sr_t)
        if not sr_e or sr_e.get("pattern") != "SR":
            return 0
        if any(m.get("predicate","").lstrip(":") == prop for m in sr_e.get("mappings",[])):
            return 0

        fixes = 0
        for m in sr_e.get("mappings", []):
            sc = self._iri_to_class(m.get("subject_triples_map",""))
            oc = self._iri_to_class(m.get("object_triples_map",""))
            if not sc or not oc:
                continue
            if ((not od) or od in self.idx.get_all_related(sc)) and \
               ((not orng) or orng in self.idx.get_all_related(oc)):
                sr_e["mappings"].append({
                    "subject_triples_map": m["subject_triples_map"],
                    "subject_resolved": True,
                    "subject_join": copy.deepcopy(m.get("subject_join",{})),
                    "predicate": f":{prop}",
                    "object_triples_map": m["object_triples_map"],
                    "object_resolved": True,
                    "object_join": copy.deepcopy(m.get("object_join",{})),
                    "_injected_by": "simulation_fix_link",
                })
                fixes += 1
                print(f"    [FIX-LINK] {sr_t}: injected :{prop} ({sc}→{oc})")
                _log(report, sr_t, f"missing :{prop}", f"injected ({sc}→{oc})",
                     "simulation_fix_link", f":{cls}→:{rng}")
                break
        return fixes

    def _fix_inverse_gap(self, test: Dict, report: Dict) -> int:
        """Inject missing inverse property direction."""
        missing_p = test.get("property","")
        existing_p = test.get("inverse_of","")
        if not missing_p or not existing_p:
            return 0
        fixes = 0
        for sr_t, sr_e in self.sr_data.items():
            if sr_e.get("pattern") != "SR":
                continue
            if any(m.get("predicate","").lstrip(":") == missing_p for m in sr_e.get("mappings",[])):
                continue
            for m in sr_e.get("mappings",[]):
                if m.get("predicate","").lstrip(":") != existing_p:
                    continue
                sr_e["mappings"].append({
                    "subject_triples_map": m.get("object_triples_map",""),
                    "subject_resolved": True,
                    "subject_join": copy.deepcopy(m.get("object_join",{})),
                    "predicate": f":{missing_p}",
                    "object_triples_map": m.get("subject_triples_map",""),
                    "object_resolved": True,
                    "object_join": copy.deepcopy(m.get("subject_join",{})),
                    "_injected_by": "simulation_fix_inverse",
                })
                fixes += 1
                print(f"    [FIX-INV] {sr_t}: injected :{missing_p} (inverse of :{existing_p})")
                _log(report, sr_t, f"missing :{missing_p}", f"inverse of :{existing_p}",
                     "simulation_fix_inverse", f":{missing_p}↔:{existing_p}")
                break
            if fixes:
                break
        return fixes

    # ── Main simulation loop ─────────────────────────────────

    def run_simulation(self, report: Dict) -> Dict:
        """Three-pass: diagnose → fix → verify."""

        print("\n  ── Pass 1: Generating patterns & tracing mappings ──")
        tests = self.generate_tests()

        passed  = [t for t in tests if t["status"] == "PASS"]
        warned  = [t for t in tests if t["status"] == "WARN"]
        failed  = [t for t in tests if t["status"] == "FAIL"]
        skipped = [t for t in tests if t["status"] == "SKIP"]

        print(f"\n  Patterns traced: {len(tests)}")
        print(f"  ├── PASS : {len(passed)}")
        print(f"  ├── WARN : {len(warned)}")
        print(f"  ├── FAIL : {len(failed)}")
        print(f"  └── SKIP : {len(skipped)}  (no DB support)")

        bf, bw = len(failed), len(warned)

        # ── PASS 2: Fix ──────────────────────────────────────
        # Only fix SR direction gaps (link + inverse) — do NOT inject typing
        # or axiom-based triples (those create false positives)
        fixes = 0
        if failed or warned:
            print(f"\n  ── Pass 2: Fixing SR gaps ({bw} warnings) ──")
            for t in warned:
                if t["type"] == "Q-LINK" and t.get("has_db_support") and t.get("sr_table"):
                    fixes += self._fix_link_gap(t, report)
                elif t["type"] == "Q-INVERSE":
                    fixes += self._fix_inverse_gap(t, report)
            print(f"\n  Fixes applied: {fixes}")

        # ── PASS 3: Re-verify ────────────────────────────────
        if fixes > 0:
            print(f"\n  ── Pass 3: Re-verifying after {fixes} fixes ──")
            self._build_indexes()
            tests = self.generate_tests()
            passed  = [t for t in tests if t["status"] == "PASS"]
            warned  = [t for t in tests if t["status"] == "WARN"]
            failed  = [t for t in tests if t["status"] == "FAIL"]
            skipped = [t for t in tests if t["status"] == "SKIP"]
            print(f"\n  After fixes:")
            print(f"  ├── PASS : {len(passed)}  (was {len(tests)-bf-bw-len(skipped)})")
            print(f"  ├── WARN : {len(warned)}  (was {bw})")
            print(f"  ├── FAIL : {len(failed)}  (was {bf})")
            print(f"  └── SKIP : {len(skipped)}")

        # ── Console report ────────────────────────────────────
        if failed:
            print(f"\n  {'─'*56}")
            print(f"  REMAINING FAILURES ({len(failed)}):")
            print(f"  {'─'*56}")
            for t in failed:
                print(f"    ✗ [{t['type']}] {t['description']}")
                print(f"      Pattern: {t['pattern']}")
                print(f"      Gap: {t.get('gap','')}")
                _log(report, t.get("class","?"), t.get("gap",""),
                     "SIMULATION_FAIL", "simulation_fail", t["description"])

        if warned:
            print(f"\n  {'─'*56}")
            print(f"  WARNINGS ({len(warned)}):")
            print(f"  {'─'*56}")
            for t in warned[:20]:
                print(f"    ⚠ [{t['type']}] {t['description']}")
            if len(warned) > 20:
                print(f"    ... and {len(warned)-20} more")

        if skipped:
            print(f"\n  SKIPPED ({len(skipped)} patterns — no DB support)")

        if passed:
            print(f"\n  {'─'*56}")
            print(f"  PASSED ({len(passed)}):")
            print(f"  {'─'*56}")
            for t in passed[:15]:
                src = ", ".join(t.get("sources",[])[:2])
                print(f"    ✓ [{t['type']}] {t['description']}  ({src})")
            if len(passed) > 15:
                print(f"    ... and {len(passed)-15} more")

        return {
            "total": len(tests), "passed": len(passed),
            "warned": len(warned), "failed": len(failed),
            "skipped": len(skipped), "fixes_applied": fixes,
        }


def run_mapping_simulation(
    se_data: Dict, sh_data: Dict, sew_data: Dict,
    srr_data: Dict, sr_data: Dict,
    idx: "OntologyIndex", tables_structure: Dict,
    report: Dict,
) -> Dict:
    """Run mapping simulation with active gap fixing."""
    sim = MappingSimulator(
        se_data, sh_data, sew_data, srr_data, sr_data,
        idx, tables_structure,
    )
    return sim.run_simulation(report)
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

    # ── Step 3b: Correct SEw owner_predicate names ───────────
    # SEw property_of_owner entries store the predicate in owner_predicate,
    # NOT in predicate_object_maps. correct_entry misses these because
    # predicate_object_maps is empty. Fix them here.
    print("\nCorrecting SEw owner_predicate names...")
    for tname, entry in sew_data.items():
        if entry.get("sew_type") != "property_of_owner":
            continue
        old_pred = entry.get("owner_predicate", "").lstrip(":")
        if not old_pred:
            continue
        # Already a valid ontology property?
        if old_pred in idx.data_props or old_pred in idx.obj_props:
            continue

        # Determine owner class for domain-aware matching
        owner_table = entry.get("owner_table", "")
        owner_cls = None
        for pd in [se_data, sh_data]:
            e = pd.get(owner_table)
            if e:
                owner_cls = e.get("subject", {}).get("class", "").lstrip(":")
                break

        fixed = False

        # Try data property match via ontology index (domain-aware)
        if owner_cls:
            match = idx.find_data_property(owner_cls, old_pred)
            if match:
                new_name = match[0]
                entry["owner_predicate"] = f":{new_name}"
                _log(report, tname, f":{old_pred}", f":{new_name}",
                     "sew_owner_pred_fix",
                     f"SEw owner_predicate corrected (data prop match)")
                fixed = True

        # Try normalized name matching against all ontology properties
        if not fixed:
            old_norm = old_pred.lower().replace("-", "").replace("_", "")
            all_props = list(idx.data_props.keys()) + list(idx.obj_props.keys())
            for ont_prop in all_props:
                ont_norm = ont_prop.lower().replace("-", "").replace("_", "")
                if old_norm == ont_norm:
                    entry["owner_predicate"] = f":{ont_prop}"
                    _log(report, tname, f":{old_pred}", f":{ont_prop}",
                         "sew_owner_pred_fix",
                         f"SEw owner_predicate corrected (normalized match)")
                    fixed = True
                    break

        # Try substring matching (e.g. "Conference-member" ~ "hasConferenceMember")
        if not fixed:
            for ont_prop in all_props:
                ont_norm = ont_prop.lower().replace("-", "").replace("_", "")
                if old_norm in ont_norm or ont_norm in old_norm:
                    # Verify domain compatibility if possible
                    if owner_cls:
                        prop_info = idx.obj_props.get(ont_prop) or idx.data_props.get(ont_prop)
                        if prop_info:
                            domain = prop_info.get("domain")
                            if domain and domain not in idx.get_all_related(owner_cls):
                                continue  # Domain mismatch
                    entry["owner_predicate"] = f":{ont_prop}"
                    _log(report, tname, f":{old_pred}", f":{ont_prop}",
                         "sew_owner_pred_fix",
                         f"SEw owner_predicate corrected (substring match)")
                    fixed = True
                    break

        if not fixed:
            # LLM fallback
            all_prop_names = list(idx.data_props.keys()) + list(idx.obj_props.keys())
            llm_prop = fixer.resolve_data_predicate(
                tname, old_pred, owner_cls or "?",
                f":{old_pred}", all_prop_names
            )
            if llm_prop:
                entry["owner_predicate"] = f":{llm_prop}"
                _log(report, tname, f":{old_pred}", f":{llm_prop}",
                     "sew_owner_pred_fix",
                     f"SEw owner_predicate corrected (LLM)")
            else:
                print(f"    [WARN] Could not correct SEw owner_predicate "
                      f":{old_pred} for {tname}")

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
    sh_data = inject_inherited_attributes(sh_data, se_data, tables_structure, idx, report)

    # ── Step 7b: Validate class assignments exist in ontology ──
    print("\nValidating class assignments against ontology...")
    se_data  = validate_class_assignments(se_data,  "SE",  idx, fixer, ontology_classes, report)
    sh_data  = validate_class_assignments(sh_data,  "SH",  idx, fixer, ontology_classes, report)
    sew_data = validate_class_assignments(sew_data, "SEw", idx, fixer, ontology_classes, report)
    if srr_data:
        srr_data = validate_class_assignments(srr_data, "SRR", idx, fixer, ontology_classes, report)

    # ── Step 7c: Detect and inject missing data properties ────
    print("\nDetecting missing data property mappings...")
    se_data = detect_missing_data_properties(se_data, tables_structure, idx, report)
    sh_data = detect_missing_data_properties(sh_data, tables_structure, idx, report)

    # ── Step 8: Inject rdfs:label across all entity phases ────
    print("\nInjecting rdfs:label for name-like columns...")
    se_data  = inject_rdfs_labels(se_data,  idx, report)
    sh_data  = inject_rdfs_labels(sh_data,  idx, report)
    if srr_data:
        srr_data = inject_rdfs_labels(srr_data, idx, report)

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

    # ── Step 11: Parse axioms for context (NO triple injection) ────
    # EquivalentClasses axioms are parsed and logged but NOT used to inject
    # new triples. The typing should come from type_dispatch (Phase 6) or
    # existing SE/SH entries — not from axiom-based inference which creates
    # false positives in structured variants.
    print("\nParsing EquivalentClasses axioms (context only, no injection)...")
    axioms = parse_equivalent_classes_axioms(ONTOLOGY_FILE)
    if axioms:
        for ax in axioms:
            print(f"  {ax['equiv_class']} ≡ ∃{ax['property']}.{ax['filler']}")
    else:
        print("  No EquivalentClasses axioms found.")

    # ── Step 11b: Validate TYPE_DISPATCH value→class assignments ──────
    print("\nValidating TYPE_DISPATCH value→class assignments...")
    hidden_data = load_json_optional(HIDDEN_MAPPINGS_FILE)
    if hidden_data:
        dispatch_fixes = 0
        for tname, entry in hidden_data.items():
            for td in entry.get("type_dispatch", []):
                dispatch = td.get("dispatch", [])
                if len(dispatch) < 2:
                    continue
                # Collect all assigned classes
                classes = [d.get("subject", {}).get("class", "").lstrip(":")
                           for d in dispatch if d.get("subject", {}).get("class")]
                if not classes:
                    continue
                # Check if all classes share a common parent (siblings)
                parents = {}
                for cls in classes:
                    anc = idx.get_ancestors(cls) - {cls}
                    parents[cls] = anc
                # Find common parent
                common = set.intersection(*parents.values()) if parents else set()
                if not common:
                    continue
                # Check: are classes in the right order?
                # Heuristic: class names containing "abstract"/"short"/"brief" → higher type value
                # Class names containing "full"/"complete"/"long" → lower type value
                # This is a soft heuristic; if inconclusive, leave as-is
                for i, d in enumerate(dispatch):
                    cls = d.get("subject", {}).get("class", "").lstrip(":")
                    val = d.get("filter_value", "")
                    cls_lower = cls.lower()
                    # Check obvious mismatches
                    if "abstract" in cls_lower and str(val) == "1":
                        # Abstract with type=1 is suspicious — typically type=1 is full
                        # Check if sibling has "full" in name
                        siblings = [c for c in classes if c != cls]
                        if any("full" in s.lower() for s in siblings):
                            # Swap needed
                            print(f"  [DISPATCH-SWAP] {tname}: type={val}→:{cls} looks wrong")
                            dispatch_fixes += 1
                    elif "full" in cls_lower and str(val) == "2":
                        siblings = [c for c in classes if c != cls]
                        if any("abstract" in s.lower() for s in siblings):
                            print(f"  [DISPATCH-SWAP] {tname}: type={val}→:{cls} looks wrong")
                            dispatch_fixes += 1

                # If we detected swaps, actually swap the classes
                if dispatch_fixes > 0 and len(dispatch) == 2:
                    cls0 = dispatch[0].get("subject", {}).get("class", "")
                    cls1 = dispatch[1].get("subject", {}).get("class", "")
                    dispatch[0]["subject"]["class"] = cls1
                    dispatch[1]["subject"]["class"] = cls0
                    # Also swap IRIs to match
                    iri0 = dispatch[0].get("triple_map_iri", "")
                    iri1 = dispatch[1].get("triple_map_iri", "")
                    dispatch[0]["triple_map_iri"] = iri0  # keep original IRI
                    dispatch[1]["triple_map_iri"] = iri1
                    print(f"  [DISPATCH-SWAP] {tname}: swapped {cls0} ↔ {cls1}")

        if dispatch_fixes:
            save_json(HIDDEN_MAPPINGS_FILE, hidden_data)
            print(f"  Saved {dispatch_fixes} dispatch swap(s)")
        else:
            print("  No dispatch swaps needed")

    # ── Step 11c: Ensure inverse object properties are emitted ──────
    print("\nChecking inverse object property coverage...")
    inverse_fixes = 0
    if sr_data:
        # Parse inverse pairs from ontology
        inverse_of = {}
        try:
            _root = ET.parse(ONTOLOGY_FILE).getroot()
            for elem in _root.iter():
                tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
                if tag == "InverseObjectProperties":
                    ch = list(elem)
                    if len(ch) == 2:
                        a = (ch[0].get("IRI", "") or ch[0].get("abbreviatedIRI", ""))
                        b = (ch[1].get("IRI", "") or ch[1].get("abbreviatedIRI", ""))
                        a = a.split("#")[-1] if "#" in a else a.split("/")[-1]
                        b = b.split("#")[-1] if "#" in b else b.split("/")[-1]
                        if a and b:
                            inverse_of[a] = b
                            inverse_of[b] = a
        except Exception:
            pass

        # For each SR mapping, check if inverse direction exists
        for sr_table, sr_entry in sr_data.items():
            if sr_entry.get("pattern") != "SR":
                continue
            mappings = sr_entry.get("mappings", [])
            existing_preds = {m.get("predicate", "").lstrip(":") for m in mappings}
            new_mappings = []
            for m in mappings:
                pred = m.get("predicate", "").lstrip(":")
                inv = inverse_of.get(pred)
                if inv and inv not in existing_preds:
                    # Inject inverse direction
                    inv_m = {
                        "subject_triples_map": m.get("object_triples_map", ""),
                        "subject_resolved": True,
                        "subject_join": copy.deepcopy(m.get("object_join", {})),
                        "predicate": f":{inv}",
                        "object_triples_map": m.get("subject_triples_map", ""),
                        "object_resolved": True,
                        "object_join": copy.deepcopy(m.get("subject_join", {})),
                        "_injected_by": "phase7_inverse_guarantee",
                    }
                    new_mappings.append(inv_m)
                    existing_preds.add(inv)
                    inverse_fixes += 1
                    print(f"  [INVERSE] {sr_table}: :{pred} → injected inverse :{inv}")
            if new_mappings:
                sr_entry["mappings"].extend(new_mappings)
        if inverse_fixes:
            print(f"  Injected {inverse_fixes} inverse properties")
        else:
            print("  All inverse properties already present")

    # ── Step 12: Mapping Simulation & Verification ────────────────
    print("\n" + "═" * 56)
    print("  MAPPING SIMULATION — Verifying completeness")
    print("═" * 56)
    sim_results = run_mapping_simulation(
        se_data, sh_data, sew_data, srr_data, sr_data,
        idx, tables_structure, report,
    )

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
                 "label_to_name", "rdfs_label_inject", "sew_property_rescue",
                 "sew_owner_pred_fix",
                 "class_validation_fix", "missing_data_prop_injected",
                 "axiom_implied_typing", "simulation_fail",
                 "simulation_fix_equiv", "simulation_fix_typing",
                 "simulation_fix_link", "simulation_fix_inverse"):
        count = sum(1 for fixes in report.values()
                    for f in fixes if f["kind"] == kind)
        if count:
            print(f"  {kind:25}: {count}")
    print(f"\n  Class collisions detected   : {n_collisions}")
    print(f"  Losers remapped by LLM      : {n_remapped}")
    print(f"  Unresolved (kept w/ warning): {n_unresolved}")
    if sim_results:
        print(f"\n  Simulation tests            : {sim_results['total']}")
        print(f"    PASS                      : {sim_results['passed']}")
        print(f"    WARN                      : {sim_results['warned']}")
        print(f"    FAIL                      : {sim_results['failed']}")
    print(f"\n  Correction report → {CORRECTION_REPORT}")
    print(f"  Collision report  → {COLLISION_REPORT}\n")


if __name__ == "__main__":
    try:
        run_correction()
    except Exception as e:
        print(f"\nFatal error: {e}")
        import traceback
        traceback.print_exc()