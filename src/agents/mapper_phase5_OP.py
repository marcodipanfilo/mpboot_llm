"""
Ontology Mapper Agent — Phase 5b (SR FK Inference + Object Property Injection)

Bridges the gap left by Phase 5 when SR_mappings.json has tables with empty
participants/mappings arrays — meaning no junction join information was available
and all object properties fell back to unresolved `ontology_join` placeholders.

This script runs THREE sequential steps in one pass:

  STEP 1 — Identify unfilled SR tables:
    Reads SR_mappings.json and collects every table whose `mappings` list is
    empty (i.e. the junction join participants were never resolved).

  STEP 2 — LLM FK inference:
    Sends the full tables_structure.json schema + the list of unfilled SR tables
    to the LLM. The LLM infers which columns in each SR junction table are FKs
    pointing to which entity tables, and returns a strict JSON patch that is
    used to overwrite the `participants` and `mappings` arrays in SR_mappings.json.

    The LLM response is cached in a process cache file so re-running the script
    does not trigger another LLM call if the SR tables are already resolved.

  STEP 3 — Object property injection:
    With SR_mappings now populated, rebuilds the junction coverage index and
    re-runs the full Phase 5 object property injection logic against SE, SH,
    SEw, and SRR mapping files — this time resolving predicates to
    `junction_join` or direct `join` (FK) entries instead of falling back to
    `ontology_join`.

Reads  : src/outputs/mappings/SR_mappings.json
         src/outputs/mappings/SE_mappings.json
         src/outputs/mappings/SH_mappings.json
         src/outputs/mappings/SEw_mappings.json
         src/outputs/mappings/SRR_mappings.json
         src/outputs/mappings/HIDDEN_mappings.json
         src/outputs/DB_as_json/tables_structure.json
         src/inputs/ontology/ontology.owl
Writes : src/outputs/mappings/SR_mappings.json        (updated with FK info)
         src/outputs/mappings/SE_mappings.json         (new object property POMs)
         src/outputs/mappings/SH_mappings.json         (if changed)
         src/outputs/mappings/SEw_mappings.json        (if changed)
         src/outputs/mappings/SRR_mappings.json        (if changed)
         src/outputs/mappings_process_phase5b.json     (LLM cache)
"""

import json
import os
import re
import requests
import xml.etree.ElementTree as ET
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.llm_config import LLMConfig, SELECTED_PROVIDER

# ── Paths ─────────────────────────────────────────────────────────────────

MAPPINGS_DIR         = "src/outputs/mappings"
DB_JSON_DIR          = "src/outputs/DB_as_json"
OUTPUT_DIR           = "src/outputs"
ONTOLOGY_FILE        = "src/inputs/ontology/ontology.owl"

SR_MAPPINGS_FILE     = os.path.join(MAPPINGS_DIR, "SR_mappings.json")
HIDDEN_MAPPINGS_FILE = os.path.join(MAPPINGS_DIR, "HIDDEN_mappings.json")
TABLES_STRUCTURE     = os.path.join(DB_JSON_DIR,  "tables_structure.json")
PROCESS_CACHE_FILE   = os.path.join(OUTPUT_DIR,   "mappings_process_phase5b.json")
CONSTRAINT_META_FILE = "src/inputs/database/constraint_metadata.json"

PHASE_FILES: Dict[str, str] = {
    "SE":  os.path.join(MAPPINGS_DIR, "SE_mappings.json"),
    "SH":  os.path.join(MAPPINGS_DIR, "SH_mappings.json"),
    "SEw": os.path.join(MAPPINGS_DIR, "SEw_mappings.json"),
    "SRR": os.path.join(MAPPINGS_DIR, "SRR_mappings.json"),
}


# ══════════════════════════════════════════════════════════════════════════
# JSON helpers
# ══════════════════════════════════════════════════════════════════════════

def load_json_safe(path: str) -> Dict:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Required file not found: {path}")
    if os.path.getsize(path) == 0:
        raise ValueError(f"File is empty: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_json_optional(path: str) -> Dict:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        print(f"  [WARN] Could not parse '{path}' — treating as empty")
        return {}


def save_json(path: str, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    print(f"  ✓ Saved → {path}")


# ══════════════════════════════════════════════════════════════════════════
# OWL parser  (identical to Phase 5)
# ══════════════════════════════════════════════════════════════════════════

def _local(iri: str) -> str:
    if not iri:
        return ""
    return iri.split("#")[-1] if "#" in iri else iri.split("/")[-1]


class OWLObjectPropertyIndex:
    def __init__(self, owl_file: str):
        self.obj_props:   Dict[str, Dict[str, Set[str]]] = defaultdict(
            lambda: {"domains": set(), "ranges": set()}
        )
        self.subclass_of: Dict[str, Set[str]] = defaultdict(set)
        self._parse(owl_file)
        self._close_subclass()

    def _parse(self, owl_file: str):
        root = ET.parse(owl_file).getroot()

        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag != "Declaration":
                continue
            ch = list(elem)
            if not ch:
                continue
            ctag = ch[0].tag.split("}")[-1] if "}" in ch[0].tag else ch[0].tag
            iri  = ch[0].get("IRI", "") or ch[0].get("abbreviatedIRI", "")
            if ctag == "ObjectProperty" and iri:
                _ = self.obj_props[_local(iri)]

        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag

            if tag == "ObjectPropertyDomain":
                ch = list(elem)
                if len(ch) < 2:
                    continue
                p = _local(ch[0].get("IRI", "") or ch[0].get("abbreviatedIRI", ""))
                if p not in self.obj_props:
                    continue
                de  = ch[1]
                dt  = de.tag.split("}")[-1] if "}" in de.tag else de.tag
                if dt == "ObjectUnionOf":
                    for c in de:
                        d = _local(c.get("IRI", "") or c.get("abbreviatedIRI", ""))
                        if d:
                            self.obj_props[p]["domains"].add(d)
                else:
                    d = _local(de.get("IRI", ""))
                    if d:
                        self.obj_props[p]["domains"].add(d)

            elif tag == "ObjectPropertyRange":
                ch = list(elem)
                if len(ch) < 2:
                    continue
                p = _local(ch[0].get("IRI", "") or ch[0].get("abbreviatedIRI", ""))
                if p not in self.obj_props:
                    continue
                re_elem = ch[1]
                rt = re_elem.tag.split("}")[-1] if "}" in re_elem.tag else re_elem.tag
                if rt == "ObjectUnionOf":
                    for c in re_elem:
                        r = _local(c.get("IRI", "") or c.get("abbreviatedIRI", ""))
                        if r:
                            self.obj_props[p]["ranges"].add(r)
                else:
                    r = _local(re_elem.get("IRI", ""))
                    if r:
                        self.obj_props[p]["ranges"].add(r)

        for axiom_tag in ("EquivalentClasses", "SubClassOf"):
            for elem in root.iter():
                tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
                if tag != axiom_tag:
                    continue
                ch = list(elem)
                named_cls    = None
                restrictions = []
                for c in ch:
                    ctag = c.tag.split("}")[-1] if "}" in c.tag else c.tag
                    if ctag == "Class":
                        iri = c.get("IRI", "") or c.get("abbreviatedIRI", "")
                        named_cls = _local(iri)
                    elif ctag == "ObjectSomeValuesFrom":
                        restrictions.append(c)
                    elif ctag == "ObjectIntersectionOf":
                        for gc in c:
                            gctag = gc.tag.split("}")[-1] if "}" in gc.tag else gc.tag
                            if gctag == "ObjectSomeValuesFrom":
                                restrictions.append(gc)
                if not named_cls or not restrictions:
                    continue
                for restr in restrictions:
                    rch = list(restr)
                    if len(rch) < 2:
                        continue
                    prop_elem  = rch[0]
                    range_elem = rch[1]
                    ptag = prop_elem.tag.split("}")[-1] if "}" in prop_elem.tag else prop_elem.tag
                    rtag = range_elem.tag.split("}")[-1] if "}" in range_elem.tag else range_elem.tag
                    if ptag != "ObjectProperty":
                        continue
                    prop = _local(prop_elem.get("IRI", ""))
                    rng  = _local(range_elem.get("IRI", "")) if rtag == "Class" else ""
                    if prop and rng:
                        self.obj_props[prop]["domains"].add(named_cls)
                        self.obj_props[prop]["ranges"].add(rng)

        for elem in root.iter():
            if not elem.tag.endswith("}SubClassOf"):
                continue
            ch = list(elem)
            if len(ch) != 2:
                continue
            s0tag = ch[0].tag.split("}")[-1] if "}" in ch[0].tag else ch[0].tag
            s1tag = ch[1].tag.split("}")[-1] if "}" in ch[1].tag else ch[1].tag
            if s0tag != "Class" or s1tag != "Class":
                continue
            sub = _local(ch[0].get("IRI", "") or ch[0].get("abbreviatedIRI", ""))
            sup = _local(ch[1].get("IRI", "") or ch[1].get("abbreviatedIRI", ""))
            if sub and sup and sup not in ("Thing", ""):
                self.subclass_of[sub].add(sup)

        # ── RDF/XML fallback ──────────────────────────────────
        # If no Declaration tags were found (RDF/XML format), parse
        # <owl:ObjectProperty>, <owl:Class>, and rdfs:subClassOf directly.
        if not self.obj_props:
            print("  [OWLObjectPropertyIndex] No Declaration tags — trying RDF/XML parsing")
            OWL  = "http://www.w3.org/2002/07/owl#"
            RDF  = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
            RDFS = "http://www.w3.org/2000/01/rdf-schema#"

            # ── Classes + subClassOf ─────────────────────────
            for elem in root.iter(f"{{{OWL}}}Class"):
                iri = elem.get(f"{{{RDF}}}about", "")
                if not iri or "owl#" in iri:
                    continue
                cls_name = _local(iri)
                for sub_elem in elem.findall(f"{{{RDFS}}}subClassOf"):
                    parent_iri = sub_elem.get(f"{{{RDF}}}resource", "")
                    if parent_iri:
                        parent = _local(parent_iri)
                        if parent and parent != "Thing":
                            self.subclass_of[cls_name].add(parent)

            # ── Object properties with domain/range ──────────
            for elem in root.iter(f"{{{OWL}}}ObjectProperty"):
                iri = elem.get(f"{{{RDF}}}about", "")
                if not iri:
                    continue
                name = _local(iri)
                # Ensure the defaultdict entry is created
                entry = self.obj_props[name]
                dom_elem = elem.find(f"{{{RDFS}}}domain")
                if dom_elem is not None:
                    d = dom_elem.get(f"{{{RDF}}}resource", "")
                    if d:
                        entry["domains"].add(_local(d))
                rng_elem = elem.find(f"{{{RDFS}}}range")
                if rng_elem is not None:
                    r = rng_elem.get(f"{{{RDF}}}resource", "")
                    if r:
                        entry["ranges"].add(_local(r))

            print(f"  [OWLObjectPropertyIndex] RDF/XML: {len(self.obj_props)} obj props, "
                  f"{len(self.subclass_of)} subclass relations")

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

    def get_descendants(self, cls: str) -> Set[str]:
        """Return cls plus all classes that are subclasses of cls (transitively)."""
        result = {cls}
        # subclass_of is already transitively closed, so any child whose
        # ancestor set includes cls is a descendant
        for child, parents in self.subclass_of.items():
            if cls in parents:
                result.add(child)
        return result

    def props_for_class(self, cls: str) -> List[Tuple[str, str]]:
        """
        Return (prop_name, range_class) pairs for all object properties whose
        domain intersects the ancestor set of cls OR the descendant set of cls.

        KEY FIX: Also includes properties whose domain is a DESCENDANT (subclass)
        of cls. This is critical because e.g. Person table should also get
        :submit (domain=Author), :presentation (domain=Speaker), :obtain (domain=Author),
        since some Person rows ARE Authors/Speakers via hidden patterns.

        Specificity rule: when two properties share the same range class and
        one has a domain that is a direct subclass of the other's domain, prefer
        the MORE SPECIFIC one (i.e. the one whose domain is closest to cls in
        the hierarchy).

        Fallback: if no properties have domain+range declarations, return all
        properties that have at least a range declared.
        """
        ancestors     = self.get_ancestors(cls)
        descendants   = self.get_descendants(cls)
        all_classes   = ancestors | descendants

        # Build depth map: how many steps from cls to each ancestor/descendant
        depth: Dict[str, int] = {cls: 0}
        changed = True
        while changed:
            changed = False
            for c, d in list(depth.items()):
                # Walk up
                for parent in self.subclass_of.get(c, set()):
                    if parent not in depth:
                        depth[parent] = d + 1
                        changed = True
                # Walk down
                for child, parents in self.subclass_of.items():
                    if c in parents and child not in depth:
                        depth[child] = d + 1
                        changed = True

        # Check if ANY property has both domain and range
        has_domain_range = any(
            info["domains"] and info["ranges"]
            for info in self.obj_props.values()
        )

        if not has_domain_range:
            # No domain/range declarations in the ontology.
            # Return all properties that have at least a range, so the
            # injection loop can still try to match them via class_index.
            # If no ranges either, return empty (can't determine targets).
            result = []
            seen = set()
            for prop, info in self.obj_props.items():
                if info["ranges"]:
                    for rng in info["ranges"]:
                        key = (prop, rng)
                        if key not in seen:
                            seen.add(key)
                            result.append((prop, rng))
            return result

        # Collect all candidate (prop, range) pairs with their best domain depth
        # best_depth[(prop, rng)] = minimum depth among matching domains
        best_depth: Dict[Tuple[str, str], int] = {}
        for prop, info in self.obj_props.items():
            if not info["domains"] or not info["ranges"]:
                continue
            matching_domains = info["domains"] & all_classes
            if not matching_domains:
                continue
            min_d = min(depth.get(d, 9999) for d in matching_domains)
            for rng in info["ranges"]:
                key = (prop, rng)
                if key not in best_depth or min_d < best_depth[key]:
                    best_depth[key] = min_d

        if not best_depth:
            return []

        # For each range class, if multiple properties qualify, keep only those
        # at the minimum depth (most specific domain).
        # Group by range_cls, find the minimum depth across all props for that range.
        range_min_depth: Dict[str, int] = {}
        for (prop, rng), d in best_depth.items():
            if rng not in range_min_depth or d < range_min_depth[rng]:
                range_min_depth[rng] = d

        seen:   Set[Tuple[str, str]] = set()
        result: List[Tuple[str, str]] = []
        for (prop, rng), d in best_depth.items():
            # Only include this (prop, rng) if its depth equals the minimum for
            # this range class — i.e. it is among the most specific properties.
            if d == range_min_depth[rng]:
                key = (prop, rng)
                if key not in seen:
                    seen.add(key)
                    result.append(key)
        return result


# ══════════════════════════════════════════════════════════════════════════
# Class index helpers  (identical to Phase 5)
# ══════════════════════════════════════════════════════════════════════════

def build_class_index(
    all_mappings: Dict[str, Dict],
    hidden_mappings: Dict,
) -> Dict[str, Tuple[str, str]]:
    idx: Dict[str, Tuple[str, str]] = {}

    for phase_key, phase_data in all_mappings.items():
        for table_name, entry in phase_data.items():
            cls = entry.get("subject", {}).get("class", "").lstrip(":")
            if cls:
                idx[cls] = (table_name, phase_key)

    for table_name, entry in hidden_mappings.items():
        for item in entry.get("hidden_sh", []) or []:
            cls = item.get("subject", {}).get("class", "").lstrip(":")
            if cls and cls not in idx:
                idx[cls] = (table_name, "HIDDEN")
        for td in entry.get("type_dispatch", []) or []:
            for d in td.get("dispatch", []) or []:
                cls = d.get("subject", {}).get("class", "").lstrip(":")
                if cls and cls not in idx:
                    idx[cls] = (table_name, "HIDDEN")

    return idx


def get_hidden_triple_map_iri(
    hidden_mappings: Dict,
    table_name: str,
    cls: str,
) -> Optional[str]:
    entry = hidden_mappings.get(table_name, {})
    for item in entry.get("hidden_sh", []) or []:
        if item.get("subject", {}).get("class", "").lstrip(":") == cls:
            return item.get("triple_map_iri", "")
    for td in entry.get("type_dispatch", []) or []:
        for d in td.get("dispatch", []) or []:
            if d.get("subject", {}).get("class", "").lstrip(":") == cls:
                return d.get("triple_map_iri", "")
    return None


def already_mapped_predicates(entry: Dict) -> Set[str]:
    return {
        pm["predicate"]
        for pm in entry.get("predicate_object_maps", [])
        if pm.get("predicate")
    }


def find_fk_col(
    table_info: Dict,
    target_table: str,
) -> Optional[Tuple[str, str]]:
    for col in table_info.get("columns", []):
        ref = col.get("foreign_key_reference") or {}
        if ref.get("table") == target_table:
            return col["name"], ref.get("column", "id")
    return None


# ══════════════════════════════════════════════════════════════════════════
# Junction coverage index  (identical to Phase 5)
# ══════════════════════════════════════════════════════════════════════════

def build_relationship_coverage_index(
    sr_data: Dict,
    srr_data: Dict,
) -> Dict[Tuple[str, str], Dict]:
    covered: Dict[Tuple[str, str], Dict] = {}

    # SR
    for rel_table, rel_entry in sr_data.items():
        if rel_entry.get("pattern") != "SR":
            continue
        jt = rel_entry.get("logical_table", rel_table)
        for mapping in rel_entry.get("mappings", []):
            subj_iri  = mapping.get("subject_triples_map", "")
            obj_iri   = mapping.get("object_triples_map",  "")
            subj_join = mapping.get("subject_join", {})
            obj_join  = mapping.get("object_join",  {})
            if not (subj_iri and obj_iri):
                continue
            covered[(subj_iri, obj_iri)] = {
                "junction_table": jt, "subject_join": subj_join,
                "object_join": obj_join,
                "subject_triples_map": subj_iri,
                "object_triples_map":  obj_iri,
                "pattern": "SR",
            }
            covered[(obj_iri, subj_iri)] = {
                "junction_table": jt, "subject_join": obj_join,
                "object_join": subj_join,
                "subject_triples_map": obj_iri,
                "object_triples_map":  subj_iri,
                "pattern": "SR",
            }

    # SRR
    for rel_table, rel_entry in srr_data.items():
        if rel_entry.get("pattern") != "SRR":
            continue
        jt    = rel_entry.get("logical_table", rel_table)
        parts = []
        for pom in rel_entry.get("predicate_object_maps", []):
            obj = pom.get("object", {})
            if obj.get("type") == "join":
                piri = obj.get("parent_triples_map", "")
                jc   = obj.get("join_condition", {})
                if piri and jc:
                    parts.append({"iri": piri, "join": jc})
        for a in parts:
            for b in parts:
                if a["iri"] != b["iri"]:
                    covered[(a["iri"], b["iri"])] = {
                        "junction_table": jt,
                        "subject_join":   a["join"],
                        "object_join":    b["join"],
                        "subject_triples_map": a["iri"],
                        "object_triples_map":  b["iri"],
                        "pattern": "SRR",
                    }

    return covered


# ══════════════════════════════════════════════════════════════════════════
# LLM agent
# ══════════════════════════════════════════════════════════════════════════

class SRInferenceAgent:

    def __init__(self, provider: str = SELECTED_PROVIDER):
        self.provider = provider
        self.config   = LLMConfig.get_config(provider)
        print(f"  LLM provider : {provider}  model : {self.config['model_name']}")

    def _strip_think(self, text: str) -> str:
        return re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()

    def _call(self, prompt: str, max_tokens: int = 3000) -> str:
        if self.provider == "claude":
            h = {
                "Content-Type": "application/json",
                "x-api-key": self.config["api_key"],
                "anthropic-version": "2023-06-01",
            }
            d = {
                "model": self.config["model_name"],
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
            }
            return requests.post(
                self.config["api_url"], headers=h, json=d
            ).json()["content"][0]["text"]

        elif self.provider == "gemini":
            url = (
                f"{self.config['api_url']}/{self.config['model_name']}"
                f":generateContent?key={self.config['api_key']}"
            )
            d = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.1,
                    "maxOutputTokens": max_tokens,
                },
            }
            return (
                requests.post(
                    url,
                    headers={"Content-Type": "application/json"},
                    json=d,
                ).json()["candidates"][0]["content"]["parts"][0]["text"]
            )

        else:  # openai-compatible
            h = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config['api_key']}",
            }
            d = {
                "model": self.config["model_name"],
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": max_tokens,
            }
            raw = requests.post(
                self.config["api_url"], headers=h, json=d
            ).json()["choices"][0]["message"]["content"]
            if self.provider == "groq":
                raw = self._strip_think(raw)
            return raw

    def _parse(self, text: str) -> Optional[Any]:
        cleaned = re.sub(r'```json\s*', '', text)
        cleaned = re.sub(r'```\s*', '', cleaned).strip()
        j = cleaned.find("{")
        e = cleaned.rfind("}") + 1
        if j != -1 and e > 0:
            try:
                return json.loads(cleaned[j:e])
            except Exception:
                pass
        j2 = cleaned.find("[")
        e2 = cleaned.rfind("]") + 1
        if j2 != -1 and e2 > 0:
            try:
                return json.loads(cleaned[j2:e2])
            except Exception:
                pass
        return None

    def build_prompt(
        self,
        sr_tables: Dict[str, Dict],
        tables_structure: Dict,
        all_mappings: Dict[str, Dict],
        constraint_meta: Dict = None,
    ) -> str:

        # Collect known entity triple map IRIs for reference
        entity_iri_map: Dict[str, str] = {}
        for phase_data in all_mappings.values():
            for tbl, entry in phase_data.items():
                iri = entry.get("triple_map_iri", "")
                if iri:
                    entity_iri_map[tbl] = iri

        entity_block = "\n".join(
            f"  {tbl}: {iri}" for tbl, iri in sorted(entity_iri_map.items())
        )

        schema_block = json.dumps(tables_structure, indent=2)

        sr_block = json.dumps(
            {name: {
                "columns": [
                    c["name"] for c in
                    tables_structure.get(name, {}).get("columns", [])
                ],
                "primary_keys": tables_structure.get(name, {}).get("primary_keys", []),
            } for name in sr_tables},
            indent=2,
        )

        # Build constraint hints block for SR tables
        constraint_block = ""
        if constraint_meta:
            lines = []
            for sr_name in sr_tables:
                t_meta = constraint_meta.get(sr_name)
                if not t_meta:
                    for k, v in constraint_meta.items():
                        if k.lower() == sr_name.lower():
                            t_meta = v
                            break
                if t_meta:
                    pk_name = t_meta.get("pk_constraint_name", "")
                    fk_map = t_meta.get("fk_constraints", {})
                    if pk_name or fk_map:
                        lines.append(f"  {sr_name}:")
                        if pk_name:
                            lines.append(f"    PK: \"{pk_name}\"")
                        for col, info in fk_map.items():
                            cn = info.get("constraint_name", "")
                            rt = info.get("ref_table", "")
                            if cn:
                                lines.append(f"    FK {col}: \"{cn}\" → {rt}")
            if lines:
                constraint_block = (
                    "\nDATABASE CONSTRAINT NAMES (strong hints — property names and FK targets are embedded):\n"
                    + "\n".join(lines) + "\n"
                )

        return f"""You are a database schema expert helping to resolve foreign key relationships
for junction tables in a relational database.

FULL DATABASE SCHEMA (tables_structure.json):
{schema_block}

KNOWN ENTITY TABLES AND THEIR TRIPLE MAP IRIs:
{entity_block}

JUNCTION TABLES THAT NEED FK RESOLUTION (SR tables with empty mappings):
{sr_block}
{constraint_block}
TASK:
For each junction table listed above, analyse its column names against the full
schema and infer which entity table each column references (i.e. what its FK
target is). Column names in junction tables are typically named after the entity
table they reference (e.g. a column called "Person" in a junction table almost
certainly references the "Person" entity table via its primary key).

For EACH junction table return:
  - participants: list of {{table, column, references_table, references_column}}
    where references_column is the PK of the referenced entity table (usually "ID")
  - mappings: list of ALL DIRECTED PAIRS between participants, each as:
    {{
      "subject_triples_map": "<triple_map_iri of first participant>",
      "object_triples_map":  "<triple_map_iri of second participant>",
      "subject_join": {{"child": "<col in junction>", "parent": "<pk in subject table>"}},
      "object_join":  {{"child": "<col in junction>", "parent": "<pk in object table>"}}
    }}
    Generate ALL directed pairs — if there are 2 participants A and B, generate
    both A→B and B→A entries in mappings.

RULES:
  - Use ONLY triple map IRIs from the KNOWN ENTITY TABLES list above.
  - If a column name matches an entity table name exactly, that IS the FK target.
  - If a column name does not match any entity table, make your best inference
    from the column name semantics and the available entity tables.
  - For the "parent" field in join conditions, always use the primary key column
    of the referenced entity table (look it up in the schema above).
  - Do NOT invent new triple map IRIs — only use ones from the list above.
  - If a junction table has more than 2 participants, generate all pairwise
    directed mappings (n*(n-1) entries total).

Return ONLY a strict JSON object with this exact structure:
{{
  "<junction_table_name>": {{
    "participants": [
      {{
        "table":             "<junction_table_name>",
        "column":            "<column_name_in_junction>",
        "references_table":  "<entity_table_name>",
        "references_column": "<pk_column_in_entity_table>"
      }}
    ],
    "mappings": [
      {{
        "subject_triples_map": "<iri>",
        "object_triples_map":  "<iri>",
        "subject_join": {{"child": "<col>", "parent": "<col>"}},
        "object_join":  {{"child": "<col>", "parent": "<col>"}}
      }}
    ]
  }},
  ...
}}

Return ONLY the JSON. No explanations, no markdown fences, no preamble."""

    def infer_sr_fks(
        self,
        sr_tables: Dict[str, Dict],
        tables_structure: Dict,
        all_mappings: Dict[str, Dict],
        constraint_meta: Dict = None,
    ) -> Optional[Dict]:
        prompt = self.build_prompt(sr_tables, tables_structure, all_mappings,
                                   constraint_meta=constraint_meta)
        raw    = self._call(prompt, max_tokens=3000)
        result = self._parse(raw)
        if not result or not isinstance(result, dict):
            print(f"  [WARN] LLM parse failed. Raw[:400]:\n{raw[:400]}")
            return None
        return result


# ══════════════════════════════════════════════════════════════════════════
# SR_mappings patcher
# ══════════════════════════════════════════════════════════════════════════

def patch_sr_mappings(
    sr_data: Dict,
    llm_result: Dict,
    tables_structure: Dict,
) -> Tuple[Dict, int]:
    """
    Apply LLM-inferred FK data into SR_mappings entries.
    Also patches tables_structure IN PLACE with foreign_key_reference info
    so that Phase 5's find_fk_col can benefit too.

    Returns (updated_sr_data, count_patched).
    """
    patched = 0
    for table_name, inferred in llm_result.items():
        if table_name not in sr_data:
            print(f"  [WARN] LLM returned unknown SR table '{table_name}' — skipping")
            continue

        entry        = sr_data[table_name]
        participants = inferred.get("participants", [])
        mappings     = inferred.get("mappings",     [])

        if not participants:
            print(f"  [WARN] No participants inferred for '{table_name}' — skipping")
            continue

        entry["participants"] = participants
        entry["mappings"]     = mappings

        # Also patch tables_structure so find_fk_col works for direct FK joins
        ts_entry = tables_structure.get(table_name, {})
        for col in ts_entry.get("columns", []):
            for p in participants:
                if col["name"] == p["column"]:
                    col["is_foreign_key"]        = True
                    col["foreign_key_reference"]  = {
                        "table":  p["references_table"],
                        "column": p["references_column"],
                    }

        print(f"  [PATCH] {table_name}: "
              f"{len(participants)} participant(s), {len(mappings)} mapping(s)")
        patched += 1

    return sr_data, patched


# ══════════════════════════════════════════════════════════════════════════
# Phase 5 object property injection  (ported from phase5, now with SR data)
# ══════════════════════════════════════════════════════════════════════════

def inject_object_properties(
    all_mappings:   Dict[str, Dict],
    hidden_mappings: Dict,
    tables_structure: Dict,
    owl_idx:         OWLObjectPropertyIndex,
    rel_coverage:    Dict[Tuple[str, str], Dict],
) -> Tuple[Dict[str, bool], int, int]:
    """
    Inject object property predicate_object_maps into every phase entry.
    Mirrors Phase 5 logic exactly, but now benefits from a populated
    rel_coverage (junction pairs) from the freshly patched SR_mappings.

    Returns (phase_dirty dict, added_total, skipped_total).
    """
    class_index   = build_class_index(all_mappings, hidden_mappings)
    phase_dirty   = {k: False for k in all_mappings}
    added_total   = 0
    skipped_total = 0

    for phase_key, phase_data in all_mappings.items():
        if not phase_data:
            continue
        print(f"\n{'─'*60}")
        print(f"  Phase : {phase_key}  ({len(phase_data)} tables)")
        print(f"{'─'*60}")

        for table_name, entry in phase_data.items():
            cls = entry.get("subject", {}).get("class", "").lstrip(":")
            if not cls:
                continue

            # Skip pure subclass extension tables — SE_SH tables whose only
            # column is the parent FK (e.g. paper_abstracts has only "id").
            # These tables have no own FK columns to form joins with other tables.
            # All relational properties belong to the parent table, not here.
            if entry.get("pattern") == "SE_SH":
                tbl_cols = tables_structure.get(table_name, {}).get("columns", [])
                own_fk_cols = [
                    c for c in tbl_cols
                    if c.get("is_foreign_key")
                    and c["name"] != entry.get("subject", {}).get("template", "").split("{")[-1].rstrip("}")
                ]
                # If the table has zero own FK columns beyond its parent link,
                # it is a pure subclass marker — skip object property injection
                if tbl_cols and not own_fk_cols:
                    print(f"  [SKIP-SE_SH] {table_name!r} has no own FK cols — pure subclass, skipping OP injection")
                    continue

            obj_props = owl_idx.props_for_class(cls)
            if not obj_props:
                continue

            this_iri   = entry.get("triple_map_iri", "")
            used_preds = already_mapped_predicates(entry)
            added_here = 0

            print(f"\n  [{phase_key}] {table_name!r}  →  :{cls}")

            for prop_name, range_cls in obj_props:
                predicate = f":{prop_name}"

                if predicate in used_preds:
                    skipped_total += 1
                    continue

                if range_cls not in class_index:
                    print(f"    [SKIP] {predicate}  range :{range_cls} not mapped")
                    skipped_total += 1
                    continue

                range_table, range_phase = class_index[range_cls]

                if range_phase == "HIDDEN":
                    range_iri = (
                        get_hidden_triple_map_iri(hidden_mappings, range_table, range_cls)
                        or ""
                    )
                else:
                    range_entry = all_mappings[range_phase][range_table]
                    range_iri   = range_entry.get("triple_map_iri", "")

                # ── Case 1: junction table covers this pair ──────────────
                # First try the direct IRI of this table, then try the IRIs
                # of ancestor-class tables (subclass tables share junctions
                # with their parent entity but have different triple_map_iris).
                junction_info = rel_coverage.get((this_iri, range_iri))
                if not junction_info:
                    ancestors_cls = owl_idx.get_ancestors(cls) - {cls}
                    for anc_cls in ancestors_cls:
                        anc_loc = class_index.get(anc_cls)
                        if not anc_loc:
                            continue
                        anc_table, anc_phase = anc_loc
                        if anc_phase == "HIDDEN":
                            continue
                        anc_entry = all_mappings.get(anc_phase, {}).get(anc_table, {})
                        anc_iri   = anc_entry.get("triple_map_iri", "")
                        if anc_iri:
                            junction_info = rel_coverage.get((anc_iri, range_iri))
                            if junction_info:
                                break
                if junction_info:
                    jt        = junction_info["junction_table"]
                    subj_join = junction_info["subject_join"]
                    obj_join  = junction_info["object_join"]
                    new_pom = {
                        "predicate": predicate,
                        "object": {
                            "type":               "junction_join",
                            "junction_table":     jt,
                            "subject_join":       subj_join,
                            "object_join":        obj_join,
                            "parent_triples_map": range_iri,
                            "resolved":           True,
                            "_op_source": (
                                f"phase5b:{prop_name} via junction '{jt}'"
                            ),
                        },
                    }
                    entry.setdefault("predicate_object_maps", []).append(new_pom)
                    used_preds.add(predicate)
                    phase_dirty[phase_key] = True
                    added_here  += 1
                    added_total += 1
                    print(f"    [ADD-JUNC] {predicate}  junction='{jt}'")
                    continue

                # ── Case 2: direct FK in source table ───────────────────
                fk_hit = find_fk_col(
                    tables_structure.get(table_name, {}), range_table
                )
                if fk_hit:
                    join_col, parent_col = fk_hit
                    # Guard: skip if this FK child column is already used in an
                    # existing join POM (even with a different predicate name).
                    # This prevents duplicate joins like :holdedBy and :holded_by
                    # both mapping the same FK column to the same parent.
                    already_joined = any(
                        p.get("object", {}).get("join_condition", {}).get("child") == join_col
                        for p in entry.get("predicate_object_maps", [])
                        if p.get("object", {}).get("type") == "join"
                    )
                    if already_joined:
                        print(f"    [SKIP-FK-DUP] {predicate}  FK={join_col!r} already has a join POM")
                        skipped_total += 1
                        continue
                    new_pom = {
                        "predicate": predicate,
                        "object": {
                            "type":               "join",
                            "parent_triples_map": range_iri,
                            "resolved":           True,
                            "join_condition": {
                                "child":  join_col,
                                "parent": parent_col,
                            },
                            "_op_source": (
                                f"phase5b:{prop_name} via FK '{join_col}'"
                            ),
                        },
                    }
                    entry.setdefault("predicate_object_maps", []).append(new_pom)
                    used_preds.add(predicate)
                    phase_dirty[phase_key] = True
                    added_here  += 1
                    added_total += 1
                    print(f"    [ADD-FK]   {predicate}  FK={join_col!r}→{parent_col!r}")
                    continue

                # ── Case 3: ontology asserts it but no join available ────
                new_pom = {
                    "predicate": predicate,
                    "object": {
                        "type":               "ontology_join",
                        "parent_triples_map": range_iri,
                        "range_table":        range_table,
                        "resolved":           False,
                        "_op_source": (
                            f"phase5b:{prop_name} no-fk/no-junction"
                        ),
                    },
                }
                entry.setdefault("predicate_object_maps", []).append(new_pom)
                used_preds.add(predicate)
                phase_dirty[phase_key] = True
                added_here  += 1
                added_total += 1
                print(f"    [ADD-ONT]  {predicate}  (ontology only, no direct join)")

            if added_here:
                print(f"    → Added {added_here} predicate(s) to '{table_name}'")

    return phase_dirty, added_total, skipped_total


# ══════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════

def run_phase5b():
    print("=" * 60)
    print("  ONTOLOGY MAPPER — Phase 5b (SR FK Inference)")
    print("  LLM infers FK relationships for SR junction tables,")
    print("  then injects object properties into phase mapping files.")
    print("=" * 60)

    # ── Load all inputs ───────────────────────────────────────────────────
    tables_structure = load_json_safe(TABLES_STRUCTURE)
    sr_data          = load_json_safe(SR_MAPPINGS_FILE)
    hidden_mappings  = load_json_optional(HIDDEN_MAPPINGS_FILE)
    process_cache    = load_json_optional(PROCESS_CACHE_FILE)

    # Load constraint metadata from Phase 0
    constraint_meta = {}
    if os.path.exists(CONSTRAINT_META_FILE):
        try:
            with open(CONSTRAINT_META_FILE, "r", encoding="utf-8") as f:
                constraint_meta = json.load(f)
            print(f"  Constraint metadata: {len(constraint_meta)} tables")
        except Exception:
            print(f"  [WARN] Could not load constraint_metadata.json")

    print(f"Loading phase mapping files ...")
    all_mappings:  Dict[str, Dict] = {}
    for phase_key, path in PHASE_FILES.items():
        data = load_json_optional(path)
        all_mappings[phase_key] = data
        print(f"  {phase_key:6s} : {len(data)} tables")

    srr_data = load_json_optional(PHASE_FILES["SRR"])

    # ── STEP 1: Identify unfilled SR tables ───────────────────────────────
    print(f"\n{'─'*60}")
    print("  STEP 1 — Identify unfilled SR tables")
    print(f"{'─'*60}")

    unfilled_sr: Dict[str, Dict] = {}
    for table_name, entry in sr_data.items():
        if entry.get("pattern") == "SR":
            mappings     = entry.get("mappings",     [])
            participants = entry.get("participants", [])
            if not mappings or not participants:
                unfilled_sr[table_name] = entry
                print(f"  [UNFILLED] {table_name}")

    if not unfilled_sr:
        print("  All SR tables already have participants/mappings filled.")
        print("  Skipping LLM call — proceeding directly to Step 3.")
    else:
        print(f"  Found {len(unfilled_sr)} unfilled SR table(s).")

    # ── STEP 2: LLM FK inference (with cache) ─────────────────────────────
    print(f"\n{'─'*60}")
    print("  STEP 2 — LLM FK inference")
    print(f"{'─'*60}")

    cache_key    = "__sr_fk_inference__"
    llm_result   = None
    patched      = 0

    if unfilled_sr:
        if cache_key in process_cache:
            llm_result = process_cache[cache_key]
            print(f"  Cache hit — using cached LLM result "
                  f"({len(llm_result)} table(s))")
        else:
            agent      = SRInferenceAgent(provider=SELECTED_PROVIDER)
            llm_result = agent.infer_sr_fks(
                unfilled_sr, tables_structure, all_mappings,
                constraint_meta=constraint_meta
            )
            if llm_result:
                process_cache[cache_key] = llm_result
                os.makedirs(OUTPUT_DIR, exist_ok=True)
                save_json(PROCESS_CACHE_FILE, process_cache)
                print(f"  LLM result cached → {PROCESS_CACHE_FILE}")
            else:
                print("  [ERROR] LLM inference failed — cannot proceed.")
                print("  SR_mappings.json NOT modified.")
                return

        # Apply LLM result to SR_mappings and tables_structure
        sr_data, patched = patch_sr_mappings(sr_data, llm_result, tables_structure)
        if patched > 0:
            save_json(SR_MAPPINGS_FILE, sr_data)
        else:
            print("  [WARN] No SR tables were patched.")
    else:
        print("  No unfilled SR tables — LLM call skipped.")

    # ── STEP 3: Object property injection ─────────────────────────────────
    print(f"\n{'─'*60}")
    print("  STEP 3 — Object property injection")
    print(f"{'─'*60}")

    # Rebuild junction coverage with the now-populated SR data
    rel_coverage = build_relationship_coverage_index(sr_data, srr_data)
    print(f"  Junction pairs available : {len(rel_coverage)}")

    print(f"\nParsing OWL from '{ONTOLOGY_FILE}' ...")
    owl_idx = OWLObjectPropertyIndex(ONTOLOGY_FILE)
    with_dr = sum(
        1 for i in owl_idx.obj_props.values()
        if i["domains"] and i["ranges"]
    )
    print(f"  Object properties declared   : {len(owl_idx.obj_props)}")
    print(f"  With domain+range resolved   : {with_dr}")

    # Log HIDDEN class count
    hidden_class_count = 0
    for entry in hidden_mappings.values():
        for item in entry.get("hidden_sh", []) or []:
            if item.get("subject", {}).get("class", ""):
                hidden_class_count += 1
        for td in entry.get("type_dispatch", []) or []:
            for d in td.get("dispatch", []) or []:
                if d.get("subject", {}).get("class", ""):
                    hidden_class_count += 1
    print(f"  HIDDEN classes available     : {hidden_class_count}")

    phase_dirty, added_total, skipped_total = inject_object_properties(
        all_mappings,
        hidden_mappings,
        tables_structure,
        owl_idx,
        rel_coverage,
    )

    # ── Save updated phase files ───────────────────────────────────────────
    print(f"\n{'='*60}")
    print("  Writing updated mapping files ...")
    for phase_key, dirty in phase_dirty.items():
        if dirty:
            save_json(PHASE_FILES[phase_key], all_mappings[phase_key])
        else:
            print(f"  {phase_key:6s} : no changes")

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print("  PHASE 5b COMPLETE")
    print(f"{'='*60}")
    print(f"  SR tables inferred    : {patched}")
    print(f"  Junction pairs built  : {len(rel_coverage)}")
    print(f"  New predicates added  : {added_total}")
    print(f"  Skipped               : {skipped_total}")
    print(f"\n  Cache  → {PROCESS_CACHE_FILE}")
    print(f"  SR     → {SR_MAPPINGS_FILE}\n")


if __name__ == "__main__":
    try:
        run_phase5b()
    except Exception as e:
        print(f"\nFatal error: {e}")
        import traceback
        traceback.print_exc()