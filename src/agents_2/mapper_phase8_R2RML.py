"""
Ontology Mapper — Phase 7: R2RML TTL Generator

Generic R2RML generator that reads all available phase JSON mapping files
and produces a single valid R2RML Turtle file. Works with any schema —
not hardcoded to any specific database.

Processing pipeline:
  1.  Load all available phase files (SE required; all others optional)
  2.  Build unified IRI index: logical_table_name → triple_map_iri
      (used to fix any broken parent_triples_map references)
  3.  Detect class collisions across all entity phases (SE, SE_SH, SEw, SRR)
  4.  Resolve collisions by priority: SE(0) > SE_SH(1) > SEw(2) > SRR(3)
        - Winner: keeps its class unchanged
        - Priority tie (same pattern level): LLM remaps ALL tied tables
        - Lower-priority loser: LLM finds an alternative class;
          if none available → table is dropped from final TTL
  5.  Re-check for secondary collisions introduced by LLM remapping
  6.  Fix all broken parent_triples_map references using the IRI index
  7.  Generate TTL blocks:
        SE / SE_SH / SEw / SRR → rr:TriplesMap with rr:tableName
        SR                     → one TriplesMap per unique direction
                                 (deduplicates reversed A↔B duplicates)
        HIDDEN                 → rr:TriplesMap with rr:sqlQuery + WHERE filter
  8.  Write collision_report.json and mappings_r2rml.ttl

JSON input structures expected per phase:

  SE / SE_SH / SEw / SRR entries:
    {
      "triple_map_iri": "urn:r2rml:...",
      "logical_table":  "table_name",
      "subject": {
        "template": "http://base#table/{pk_col}",
        "class":    ":ClassName"
      },
      "predicate_object_maps": [
        { "predicate": ":pred",
          "object": {
            "type": "literal",
            "column": "col_name",
            "datatype": "xsd:string"
          }
        },
        { "predicate": ":pred",
          "object": {
            "type": "join",
            "parent_triples_map": "urn:r2rml:...",
            "resolved": true/false,
            "join_condition": { "child": "fk_col", "parent": "pk_col" }
          }
        }
      ]
    }

  SR entries:
    {
      "triple_map_iri": "urn:r2rml:SR_...",
      "logical_table":  "bridge_table",
      "participants":   [...],
      "mappings": [
        {
          "subject_triples_map": "urn:r2rml:...",
          "subject_resolved":    true/false,
          "subject_join":        { "child": "fk_col", "parent": "pk_col" },
          "predicate":           ":objectProperty",
          "object_triples_map":  "urn:r2rml:...",
          "object_resolved":     true/false,
          "object_join":         { "child": "fk_col", "parent": "pk_col" }
        }
      ]
    }

  HIDDEN entries:
    {
      "source_table": "table_name",
      "hidden_sh": [
        {
          "triple_map_iri":  "urn:r2rml:HIDDEN_SH_...",
          "trigger_column":  "col_name",
          "sql_filter":      "col_name IS NOT NULL",
          "subject": { "template": "...", "class": ":ClassName" },
          "predicate_object_maps": [...]
        }
      ],
      "type_dispatch": [
        {
          "discriminator_column": "col_name",
          "dispatch": [
            {
              "triple_map_iri": "urn:r2rml:HIDDEN_TD_...",
              "sql_filter":     "col_name = 1",
              "subject": { "template": "...", "class": ":ClassName" }
            }
          ]
        }
      ]
    }

Reads  : src2/outputs/mappings/SE_mappings.json          (required)
         src2/outputs/mappings/SH_mappings.json           (optional)
         src2/outputs/mappings/SEw_mappings.json          (optional)
         src2/outputs/mappings/SRR_mappings.json          (optional)
         src2/outputs/mappings/SR_mappings.json           (optional)
         src2/outputs/mappings/HIDDEN_mappings.json       (optional)
         src2/inputs/ontology/ontology.owl
Writes : src2/outputs/mappings/collision_report.json
         src2/outputs/mappings/mappings_r2rml.ttl
"""

import json
import re
import requests
import xml.etree.ElementTree as ET
from collections import defaultdict
from typing import Dict, List, Optional, Tuple, Set
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.llm_config import LLMConfig
from config.llm_config import SELECTED_PROVIDER
from parsers.ontology_explorer import ontology_explorer

# ===== PATHS =====
OUTPUT_DIR       = "src2/outputs"
MAPPINGS_DIR     = os.path.join(OUTPUT_DIR, "mappings")
ONTOLOGY_FILE    = "src2/inputs/ontology/ontology.owl"
SE_FILE          = os.path.join(MAPPINGS_DIR, "SE_mappings.json")
SH_FILE          = os.path.join(MAPPINGS_DIR, "SH_mappings.json")
SEW_FILE         = os.path.join(MAPPINGS_DIR, "SEw_mappings.json")
SRR_FILE         = os.path.join(MAPPINGS_DIR, "SRR_mappings.json")
SR_FILE          = os.path.join(MAPPINGS_DIR, "SR_mappings.json")
HIDDEN_FILE      = os.path.join(MAPPINGS_DIR, "HIDDEN_mappings.json")
COLLISION_REPORT = os.path.join(MAPPINGS_DIR, "collision_report.json")
R2RML_FILE       = os.path.join(MAPPINGS_DIR, "mappings_r2rml.ttl")


# Priority: lower = wins collision. Only entity patterns have classes.
PHASE_PRIORITY = {"SE": 0, "SE_SH": 1, "SEw": 2, "SRR": 3}


# ============================================================
# File loaders
# ============================================================

def load_json_safe(path: str) -> Dict:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Required file not found: {path}")
    if os.path.getsize(path) == 0:
        raise ValueError(f"File is empty: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_json_optional(path: str, label: str = "") -> Dict:
    """Load optional JSON file. Returns {} if missing, empty, or corrupt."""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        print(f"  {label or path}: not found — skipping")
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        print(f"  {label or path}: {len(data)} entries")
        return data
    except json.JSONDecodeError:
        print(f"  [WARN] Could not parse '{path}' — skipping")
        return {}


# ============================================================
# Ontology helpers
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
            base = (root.get("{http://www.w3.org/XML/1998/namespace}base")
                    or root.get("ontologyIRI", ""))
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


def get_base_iri(prefixes: Dict[str, str]) -> str:
    if "" in prefixes:
        return prefixes[""]
    standard = {"owl", "rdf", "rdfs", "xsd", "xml", "xsp", "swrl", "swrlb", "protege"}
    for name, iri in prefixes.items():
        if name not in standard:
            return iri
    return "http://ontology#"


# ============================================================
# IRI index & broken reference fixer
# ============================================================

def build_iri_index(entity_entries: Dict, sr_raw: Dict) -> Dict[str, str]:
    """
    Build: logical_table_name → triple_map_iri
    This index is used to fix references like 'urn:r2rml:SE_papers'
    when the actual IRI is 'urn:r2rml:SE_SH_papers'.
    The JSON files may have been written with wrong prefix at map time
    if the target table was mapped in a later phase.
    """
    index: Dict[str, str] = {}
    for entry in entity_entries.values():
        tname = entry.get("logical_table")
        iri   = entry.get("triple_map_iri")
        if tname and iri:
            index[tname] = iri
    for entry in sr_raw.values():
        tname = entry.get("logical_table")
        iri   = entry.get("triple_map_iri")
        if tname and iri:
            index[tname] = iri
    return index


def fix_iri(ref_iri: str, defined_iris: Set[str], iri_index: Dict[str, str]) -> str:
    """
    If ref_iri is in the defined set → return unchanged.
    Otherwise, extract the table name from the IRI suffix by progressively
    dropping leading underscore-separated segments until a match is found
    in iri_index.
    Example: 'urn:r2rml:SE_papers' → tail='SE_papers' → try 'papers'
             → iri_index['papers'] = 'urn:r2rml:SE_SH_papers' → fixed.
    If no match found → return original (will appear as comment warning in TTL).
    """
    if ref_iri in defined_iris:
        return ref_iri

    # Extract the suffix after the last colon
    tail  = ref_iri.split(":")[-1]   # e.g. "SE_SH_conf_members" or "SE_papers"
    parts = tail.split("_")

    for start in range(1, len(parts)):
        candidate = "_".join(parts[start:])
        if candidate in iri_index:
            return iri_index[candidate]

    return ref_iri  # unresolvable — return as-is


# ============================================================
# Collision detection & resolution
# ============================================================

def detect_collisions(entity_entries: Dict,
                      exclude: Set[str] = None) -> Dict[str, List[str]]:
    """
    Find classes shared by more than one table.
    Returns { ":ClassName": ["table_a", "table_b", ...] }
    """
    exclude       = exclude or set()
    class_to_tabs = defaultdict(list)
    for tname, entry in entity_entries.items():
        if tname in exclude:
            continue
        cls = entry.get("subject", {}).get("class", "")
        if cls:
            class_to_tabs[cls].append(tname)
    return {cls: tabs for cls, tabs in class_to_tabs.items() if len(tabs) > 1}


def resolve_winner(tables: List[str],
                   entity_entries: Dict) -> Tuple[Optional[str], List[str]]:
    """
    Given tables sharing a class, return (winner_or_None, losers).
    Sorted by PHASE_PRIORITY. If multiple tables share the top priority
    (a tie), winner=None and all are losers (LLM remaps all).
    """
    ranked  = sorted(tables,
                     key=lambda t: PHASE_PRIORITY.get(
                         entity_entries[t].get("pattern", ""), 99))
    top_pri = PHASE_PRIORITY.get(entity_entries[ranked[0]].get("pattern", ""), 99)
    winners = [t for t in ranked
               if PHASE_PRIORITY.get(entity_entries[t].get("pattern", ""), 99) == top_pri]

    if len(winners) > 1:
        return None, winners      # full tie — remap all
    return winners[0], ranked[1:]


# ============================================================
# LLM remap agent
# ============================================================

class RemapAgent:

    def __init__(self, provider: str = SELECTED_PROVIDER):
        self.provider = provider
        self.config   = LLMConfig.get_config(provider)

    def _strip(self, text: str) -> str:
        return re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()

    def _has_json(self, text: str) -> bool:
        j = text.find("{"); e = text.rfind("}") + 1
        if j == -1 or e == 0:
            return False
        try:
            json.loads(text[j:e])
            return True
        except Exception:
            return False

    def _call(self, prompt: str) -> str:
        if self.provider == "claude":
            h = {"Content-Type": "application/json",
                 "x-api-key": self.config["api_key"],
                 "anthropic-version": "2023-06-01"}
            d = {"model": self.config["model_name"], "max_tokens": 1024,
                 "messages": [{"role": "user", "content": prompt}],
                 "temperature": 0.3}
            return requests.post(self.config["api_url"], headers=h,
                                 json=d).json()["content"][0]["text"]

        elif self.provider == "gemini":
            url = (f"{self.config['api_url']}/{self.config['model_name']}"
                   f":generateContent?key={self.config['api_key']}")
            d = {"contents": [{"parts": [{"text": prompt}]}],
                 "generationConfig": {"temperature": 0.3, "maxOutputTokens": 1024}}
            return (requests.post(url, headers={"Content-Type": "application/json"},
                                  json=d).json()
                    ["candidates"][0]["content"]["parts"][0]["text"])

        else:  # openai-compatible (groq, openai, etc.)
            h = {"Content-Type": "application/json",
                 "Authorization": f"Bearer {self.config['api_key']}"}
            d = {"model": self.config["model_name"],
                 "messages": [{"role": "user", "content": prompt}],
                 "temperature": 0.3, "max_tokens": 1024}
            raw = requests.post(self.config["api_url"], headers=h,
                                json=d).json()["choices"][0]["message"]["content"]
            if self.provider == "groq":
                raw = self._strip(raw)
                if not self._has_json(raw):
                    d2 = {"model": self.config["model_name"], "messages": [
                        {"role": "user",      "content": prompt},
                        {"role": "assistant", "content": raw},
                        {"role": "user",      "content": "Output ONLY the JSON object now."}
                    ], "temperature": 0.1, "max_tokens": 512}
                    raw = self._strip(
                        requests.post(self.config["api_url"], headers=h,
                                      json=d2).json()["choices"][0]["message"]["content"]
                    )
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

    def remap_single(self, table_name: str, entry: Dict,
                     used_classes: Set[str],
                     ontology_classes: List[str]) -> Tuple[Optional[str], Optional[Dict]]:
        """
        Ask LLM to find a different class for a loser table.
        Returns (new_class_without_colon_or_None, llm_raw_result).
        """
        available = [c for c in ontology_classes if f":{c}" not in used_classes]
        if not available:
            return None, {"reasoning": "No available classes remaining in ontology"}

        prompt = f"""You are an ontology mapping expert.

TABLE: {table_name}  (pattern: {entry.get('pattern', '?')})
CURRENT CLASS (already taken by another table): {entry['subject']['class']}
TABLE MEANING: {entry.get('table_meaning', 'not available')}

This table needs a DIFFERENT ontology class because its current class is already
assigned to a higher-priority table.
Return null for new_class if no suitable alternative exists.

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
                return nc, result
        return None, result

    def remap_tied(self, tables: List[str], entity_entries: Dict,
                   used_classes: Set[str],
                   ontology_classes: List[str]) -> Tuple[Dict[str, Optional[str]], Optional[Dict]]:
        """
        All tables in a tie share the same class and same priority.
        Ask LLM to assign distinct classes to each.
        The shared class is still available — LLM can assign it to one of them.
        Returns ({table: new_class_or_None}, llm_raw_result).
        """
        shared_cls    = entity_entries[tables[0]]["subject"]["class"]
        # Free the shared class so LLM can reassign it to exactly one table
        free_pool     = used_classes - {shared_cls}
        available     = [c for c in ontology_classes if f":{c}" not in free_pool]

        table_descs = "\n".join(
            f"  - {t}: meaning: {entity_entries[t].get('table_meaning', 'not available')}"
            for t in tables
        )
        # Build the JSON template with one key per table
        json_template = "{\n" + "\n".join(
            f'  "{t}": "ClassName or null",' for t in tables
        ) + '\n  "reasoning": "explanation"\n}'

        prompt = f"""You are an ontology mapping expert.
These tables all currently share the SAME ontology class '{shared_cls}'.
Each needs a DISTINCT class assignment. You may keep the shared class for
at most one of them.

TABLES:
{table_descs}

AVAILABLE CLASSES: {', '.join(available)}

Return ONLY a JSON object with one key per table name:
{json_template}"""

        raw    = self._call(prompt)
        result = self._parse(raw)
        if result:
            assignments = {
                t: (result.get(t) if result.get(t) and str(result.get(t)).lower() != "null"
                    else None)
                for t in tables
            }
            return assignments, result
        return {t: None for t in tables}, None


# ============================================================
# TTL generation helpers
# ============================================================

def _pom_literal(pred: str, col: str, datatype: str) -> List[str]:
    # xsd:anyURI cannot be used as rr:datatype — the R2RML engine does not recognize it.
    # Map anyURI columns as IRI-valued objects using rr:template instead of rr:column+datatype.
    # The column value is assumed to be a valid absolute IRI string.
    if datatype in ("xsd:anyURI", "http://www.w3.org/2001/XMLSchema#anyURI"):
        return [
            f"    rr:predicateObjectMap [",
            f"        rr:predicate {pred} ;",
            f"        rr:objectMap  [",
            f"            rr:column    \"{col}\" ;",
            f"            rr:termType  rr:IRI ;",
            f"        ] ;",
            f"    ] ;",
            "",
        ]
    return [
        f"    rr:predicateObjectMap [",
        f"        rr:predicate {pred} ;",
        f"        rr:objectMap  [",
        f"            rr:column   \"{col}\" ;",
        f"            rr:datatype {datatype} ;",
        f"        ] ;",
        f"    ] ;",
        "",
    ]


def _pom_join(pred: str, parent_iri: str,
              child_col: str, parent_col: str) -> List[str]:
    return [
        f"    rr:predicateObjectMap [",
        f"        rr:predicate {pred} ;",
        f"        rr:objectMap  [",
        f"            rr:parentTriplesMap <{parent_iri}> ;",
        f"            rr:joinCondition [",
        f"                rr:child  \"{child_col}\" ;",
        f"                rr:parent \"{parent_col}\" ;",
        f"            ] ;",
        f"        ] ;",
        f"    ] ;",
        "",
    ]


def _close(lines: List[str]) -> str:
    """Replace the last trailing ';' in the block with '.' and return joined string."""
    for i in range(len(lines) - 1, -1, -1):
        s = lines[i].rstrip()
        if s.endswith(";"):
            lines[i] = s[:-1] + " ."
            break
    lines.append("")
    return "\n".join(lines)


def _resolve_poms(poms: List[Dict],
                  defined_iris: Set[str],
                  iri_index: Dict[str, str]) -> List[str]:
    """
    Build TTL lines for all predicate-object maps.
    Fixes any broken parent_triples_map references automatically.
    """
    lines = []
    for pom in poms:
        pred = pom.get("predicate", "")
        obj  = pom.get("object", {})
        otype = obj.get("type", "")

        if otype == "literal":
            lines += _pom_literal(pred, obj["column"], obj["datatype"])

        elif otype == "join":
            raw_ref = obj.get("parent_triples_map", "")
            fixed   = fix_iri(raw_ref, defined_iris, iri_index)
            if fixed != raw_ref:
                lines.append(
                    f"    # [auto-fixed] {raw_ref} → {fixed}"
                )
            jc = obj.get("join_condition", {})
            lines += _pom_join(pred, fixed,
                               jc.get("child", "id"), jc.get("parent", "id"))
    return lines


def build_entity_block(table_name: str, entry: Dict,
                       defined_iris: Set[str], iri_index: Dict[str, str],
                       sql_filter: Optional[str] = None) -> str:
    """
    Generic TTL block builder for SE, SE_SH, SEw, SRR, and HIDDEN patterns.
    Priority for logical table source:
      1. sql_filter argument (HIDDEN pattern — WHERE clause on the table)
      2. entry["logical_table_sql"] (inheritance fix — full custom SQL, e.g. JOIN)
      3. entry["logical_table"] table name (standard case — rr:tableName)
    """
    iri  = entry["triple_map_iri"]
    cls  = entry["subject"].get("class")   # may be None for classless SEw joins
    tmpl = entry["subject"]["template"]
    pat  = entry.get("pattern", "")

    # Determine the logical table declaration
    if sql_filter:
        # HIDDEN pattern: filter on a single table
        table_line = (f"    rr:logicalTable [ rr:sqlQuery \"\"\""
                      f"SELECT * FROM {table_name} WHERE {sql_filter}\"\"\" ] ;")
    elif entry.get("logical_table_sql"):
        # Inheritance fix: custom SQL that JOINs parent table to get extra columns
        sql = entry["logical_table_sql"]
        table_line = f"    rr:logicalTable [ rr:sqlQuery \"\"\"{sql}\"\"\" ] ;"
    else:
        table_line = f"    rr:logicalTable [ rr:tableName \"{table_name}\" ] ;"

    sep   = "─" * max(0, 48 - len(pat) - len(table_name))
    lines = [
        f"# ── {pat}_{table_name} {sep}",
        f"<{iri}>",
        f"    a rr:TriplesMap ;",
        f"",
        table_line,
        f"",
        f"    rr:subjectMap [",
        f"        rr:template \"{tmpl}\" ;",
    ]

    # Only emit rr:class when one is set (classless entries are pure join bridges)
    if cls:
        lines.append(f"        rr:class     {cls} ;")

    lines += [f"    ] ;", f""]

    pom_lines = _resolve_poms(
        entry.get("predicate_object_maps", []), defined_iris, iri_index
    )
    lines += pom_lines

    return _close(lines)


def _get_subject_template(triple_map_iri: str,
                          entity_entries: Dict) -> Optional[str]:
    """Look up the subject template for a given TriplesMap IRI."""
    for entry in entity_entries.values():
        if entry.get("triple_map_iri") == triple_map_iri:
            return entry.get("subject", {}).get("template")
    return None


def _adapt_template_for_bridge(tmpl: str, s_join: Dict) -> str:
    """
    The subject template comes from the entity TriplesMap and uses the entity's
    own PK column (e.g. {id}). The bridge table does NOT have that column —
    it only has the FK column that references the entity (e.g. {aid}, {cid}, {pid}).

    Replace every {entity_pk} placeholder in the template with {bridge_fk_col}
    using subject_join:  child = FK column in bridge,  parent = PK column in entity.

    Examples:
      tmpl="http://cmt#persons/{id}",     s_join={"child":"aid","parent":"id"}
        → "http://cmt#persons/{aid}"
      tmpl="http://cmt#conferences/{id}", s_join={"child":"cid","parent":"id"}
        → "http://cmt#conferences/{cid}"
      tmpl="http://cmt#documents/{id}",   s_join={"child":"pid","parent":"id"}
        → "http://cmt#documents/{pid}"
    """
    fk_col    = s_join.get("child", "")
    entity_pk = s_join.get("parent", "id")
    if fk_col and entity_pk:
        tmpl = tmpl.replace(f"{{{entity_pk}}}", f"{{{fk_col}}}")
    return tmpl


def build_sr_section(sr_raw: Dict, entity_entries: Dict,
                     defined_iris: Set[str],
                     iri_index: Dict[str, str]) -> str:
    """
    SR bridge tables have no class of their own.
    Each bridge table generates one TriplesMap per unique (subject, predicate, object)
    direction. Duplicate reversed directions (same pair + predicate) are suppressed.

    The TriplesMap for each direction:
      - rr:logicalTable  = bridge table name
      - rr:subjectMap    = entity's template with PK placeholder replaced by bridge FK col
                           CRITICAL: bridge table has FK cols (aid/cid/pid), NOT entity PK (id)
      - rr:predicateObjectMap = join to the object entity via its FK col in the bridge
    """
    if not sr_raw:
        return ""

    blocks = [
        "# " + "═" * 52,
        "# SR — Simple Relationships (Bridge Tables)",
        "# " + "═" * 52,
        "",
    ]

    seen_pairs: Set = set()

    for bridge_table, entry in sr_raw.items():
        bridge_name_blocks = []

        for m in entry.get("mappings", []):
            subj_iri = fix_iri(m["subject_triples_map"], defined_iris, iri_index)
            obj_iri  = fix_iri(m["object_triples_map"],  defined_iris, iri_index)
            pred     = m.get("predicate", "")

            # Deduplication: unordered pair + predicate
            pair_key = (frozenset([subj_iri, obj_iri]), pred)
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)

            # Resolve subject template from entity_entries
            subj_tmpl = _get_subject_template(subj_iri, entity_entries)
            if subj_tmpl is None:
                blocks.append(
                    f"# SKIPPED SR_{bridge_table}: "
                    f"subject template not found for <{subj_iri}>\n"
                )
                continue

            s_join = m.get("subject_join", {})
            o_join = m.get("object_join", {})

            # CRITICAL FIX: replace entity PK placeholder with the bridge's FK column.
            # The bridge table only has FK columns (e.g. aid, cid, pid) — never the
            # entity's own PK column (id). Without this, the engine throws
            # "Unknown column: id" because `id` doesn't exist in the bridge table.
            subj_tmpl = _adapt_template_for_bridge(subj_tmpl, s_join)

            subj_tag = subj_iri.split("_")[-1]
            obj_tag  = obj_iri.split("_")[-1]
            sr_iri   = f"{entry['triple_map_iri']}_{subj_tag}_{obj_tag}"

            lines = [
                f"# ── SR_{bridge_table} ({subj_tag} → {obj_tag}) {'─' * 10}",
                f"<{sr_iri}>",
                f"    a rr:TriplesMap ;",
                f"",
                f"    rr:logicalTable [ rr:tableName \"{bridge_table}\" ] ;",
                f"",
                f"    rr:subjectMap [",
                f"        rr:template \"{subj_tmpl}\" ;",
                f"    ] ;",
                f"",
            ]
            lines += _pom_join(pred, obj_iri,
                               o_join.get("child", "id"),
                               o_join.get("parent", "id"))
            bridge_name_blocks.append(_close(lines))

        if bridge_name_blocks:
            blocks.extend(bridge_name_blocks)
        else:
            blocks.append(
                f"# SR_{bridge_table}: all directions deduplicated or skipped\n"
            )

    return "\n".join(blocks)


def build_hidden_section(hidden_raw: Dict, entity_entries: Dict,
                         defined_iris: Set[str],
                         iri_index: Dict[str, str]) -> str:
    """
    HIDDEN patterns use rr:sqlQuery with a WHERE filter instead of rr:tableName.
    HIDDEN_SH: WHERE trigger_column IS NOT NULL
    TYPE_DISPATCH: WHERE discriminator_col = value
    """
    if not hidden_raw:
        return ""

    blocks = [
        "# " + "═" * 52,
        "# HIDDEN — Hidden Subclasses & Type Dispatch",
        "# " + "═" * 52,
        "",
    ]

    for table_name, entry in hidden_raw.items():

        # Pattern A: Hidden Subclass
        for hsh in entry.get("hidden_sh", []):
            subj = hsh.get("subject", {})
            if not subj.get("class") or not subj.get("template"):
                continue
            sql_filter = hsh.get(
                "sql_filter",
                f"{hsh.get('trigger_column', 'col')} IS NOT NULL"
            )
            fake_entry = {
                "triple_map_iri":        hsh["triple_map_iri"],
                "subject":               subj,
                "pattern":               "HIDDEN_SH",
                "predicate_object_maps": hsh.get("predicate_object_maps", []),
            }
            blocks.append(
                build_entity_block(table_name, fake_entry,
                                   defined_iris, iri_index,
                                   sql_filter=sql_filter)
            )

        # Pattern B: Type Dispatch
        for td in entry.get("type_dispatch", []):
            col = td.get("discriminator_column", "type")
            for dispatch in td.get("dispatch", []):
                subj = dispatch.get("subject", {})
                if not subj.get("class") or not subj.get("template"):
                    continue
                sql_filter = dispatch.get(
                    "sql_filter",
                    f"{col} = {dispatch.get('filter_value', '?')}"
                )
                fake_entry = {
                    "triple_map_iri":        dispatch["triple_map_iri"],
                    "subject":               subj,
                    "pattern":               "TYPE_DISPATCH",
                    "predicate_object_maps": [],
                }
                blocks.append(
                    build_entity_block(table_name, fake_entry,
                                       defined_iris, iri_index,
                                       sql_filter=sql_filter)
                )

    return "\n".join(blocks)


def section_header(title: str) -> str:
    return "\n".join([
        "# " + "═" * 52,
        f"# {title}",
        "# " + "═" * 52,
        "",
    ])


def build_prefix_block(base_iri: str) -> str:
    return "\n".join([
        "@prefix rr:   <http://www.w3.org/ns/r2rml#> .",
        "@prefix xsd:  <http://www.w3.org/2001/XMLSchema#> .",
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .",
        "@prefix rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .",
        f"@prefix :     <{base_iri}> .",
        "",
    ])


# ============================================================
# Main entry point
# ============================================================

def run_r2rml_generation():

    print("=" * 56)
    print("  ONTOLOGY MAPPER — Phase 7 (R2RML TTL Generator)")
    print("=" * 56)
    os.makedirs(MAPPINGS_DIR, exist_ok=True)

    # ── Step 1: Load all phase files ────────────────────────
    print("\nLoading phase JSON files...")
    se_raw  = load_json_safe(SE_FILE)
    sh_raw  = load_json_optional(SH_FILE,    "SE_SH  ")
    sew_raw = load_json_optional(SEW_FILE,   "SEw    ")
    srr_raw = load_json_optional(SRR_FILE,   "SRR    ")
    sr_raw  = load_json_optional(SR_FILE,    "SR     ")
    hidden  = load_json_optional(HIDDEN_FILE,"HIDDEN ")
    print(f"  SE     : {len(se_raw)} tables (required)")

    # ── Step 2: Ontology setup ───────────────────────────────
    print(f"\nParsing ontology from '{ONTOLOGY_FILE}' ...")
    prefixes         = parse_ontology_prefixes(ONTOLOGY_FILE)
    base_iri         = get_base_iri(prefixes)
    ontology_classes = ontology_explorer(mode="classes")["classes"]
    print(f"  Base IRI : {base_iri}")
    print(f"  Classes  : {len(ontology_classes)}")

    # ── Step 3: Merge entity entries, inject pattern tag ────
    entity_entries: Dict = {}
    for t, e in se_raw.items():
        entity_entries[t] = {**e, "pattern": "SE"}
    for t, e in sh_raw.items():
        entity_entries[t] = {**e, "pattern": "SE_SH"}
    for t, e in sew_raw.items():
        entity_entries[t] = {**e, "pattern": "SEw"}
    for t, e in srr_raw.items():
        entity_entries[t] = {**e, "pattern": "SRR"}

    # ── Step 4: Build IRI index for reference fixing ────────
    iri_index    = build_iri_index(entity_entries, sr_raw)
    defined_iris = {e["triple_map_iri"] for e in entity_entries.values()}

    # ── Step 5: Collision detection & resolution ────────────
    print("\nDetecting class collisions...")
    dropped_tables: Set[str] = set()
    remapped:       Dict     = {}
    collision_log:  Dict     = {}
    used_classes             = {e["subject"]["class"] for e in entity_entries.values()}

    collisions = detect_collisions(entity_entries)
    if not collisions:
        print("  No collisions found.")
    else:
        print(f"  Found {len(collisions)} collision(s).")
        agent = RemapAgent(provider=SELECTED_PROVIDER)

        for cls, tables in collisions.items():
            print(f"\n  Collision: {cls}")
            for t in tables:
                print(f"    [{entity_entries[t]['pattern']}] {t}")

            winner, losers = resolve_winner(tables, entity_entries)

            if winner is None:
                # ── Tie: remap all ───────────────────────────
                print(f"    → Priority tie — LLM remaps all {losers}")
                new_map, llm_res = agent.remap_tied(
                    losers, entity_entries, used_classes, ontology_classes
                )
                for t, new_cls in new_map.items():
                    _apply_remap(t, new_cls, entity_entries, used_classes,
                                 remapped, dropped_tables)
                collision_log[cls] = {
                    "type": "tie", "tables": losers,
                    "assignments": new_map, "llm_result": llm_res
                }
            else:
                # ── Clear winner ─────────────────────────────
                print(f"    → Winner: [{entity_entries[winner]['pattern']}] "
                      f"{winner} keeps {cls}")
                collision_log[cls] = {
                    "type": "priority_win",
                    "winner": winner,
                    "losers": {}
                }
                for loser in losers:
                    print(f"    → Loser:  [{entity_entries[loser]['pattern']}] "
                          f"{loser} — asking LLM...", end="", flush=True)
                    new_cls, llm_res = agent.remap_single(
                        loser, entity_entries[loser], used_classes, ontology_classes
                    )
                    _apply_remap(loser, new_cls, entity_entries, used_classes,
                                 remapped, dropped_tables,
                                 print_result=True)
                    collision_log[cls]["losers"][loser] = {
                        "new_class": new_cls, "llm_result": llm_res
                    }

        # ── Step 5b: Re-check for secondary collisions ──────
        secondary = detect_collisions(entity_entries, exclude=dropped_tables)
        if secondary:
            print(f"\n  Secondary collisions detected (from LLM remapping):")
            for cls2, tables2 in secondary.items():
                winner2, losers2 = resolve_winner(tables2, entity_entries)
                to_drop = losers2 if winner2 else tables2[1:]
                for t in to_drop:
                    print(f"    Secondary drop: {t} (duplicates {cls2})")
                    dropped_tables.add(t)
                collision_log[f"SECONDARY_{cls2}"] = {
                    "type":    "secondary",
                    "winner":  winner2 or tables2[0],
                    "dropped": to_drop
                }

    # Save collision report
    with open(COLLISION_REPORT, "w", encoding="utf-8") as f:
        json.dump(collision_log, f, indent=2)
    print(f"\n  Collision report → {COLLISION_REPORT}")
    if dropped_tables:
        print(f"  Dropped tables   : {sorted(dropped_tables)}")
    if remapped:
        print(f"  Remapped         : { {t: c for t, c in remapped.items()} }")

    # Refresh defined_iris after resolution
    defined_iris = {
        e["triple_map_iri"]
        for t, e in entity_entries.items()
        if t not in dropped_tables
    }

    # ── Step 6: Generate TTL ─────────────────────────────────
    print("\nGenerating R2RML Turtle...")
    sections: List[str] = [build_prefix_block(base_iri)]

    # SE
    sections.append(section_header("SE — Strong Entities"))
    for t in se_raw:
        if t in dropped_tables:
            sections.append(
                f"# DROPPED: SE_{t} — class collision, no alternative found\n"
            )
        else:
            sections.append(
                build_entity_block(t, entity_entries[t], defined_iris, iri_index)
            )

    # SE_SH
    if sh_raw:
        sections.append(section_header("SE_SH — Subclass Entities"))
        for t in sh_raw:
            if t in dropped_tables:
                sections.append(
                    f"# DROPPED: SE_SH_{t} — class collision, no alternative found\n"
                )
            else:
                sections.append(
                    build_entity_block(t, entity_entries[t], defined_iris, iri_index)
                )

    # SEw
    if sew_raw:
        sections.append(section_header("SEw — Weak Entities"))
        for t in sew_raw:
            if t in dropped_tables:
                sections.append(
                    f"# DROPPED: SEw_{t} — class collision, no alternative found\n"
                )
            else:
                sections.append(
                    build_entity_block(t, entity_entries[t], defined_iris, iri_index)
                )

    # SRR
    if srr_raw:
        sections.append(section_header("SRR — Reified Relationships"))
        for t in srr_raw:
            if t in dropped_tables:
                sections.append(
                    f"# DROPPED: SRR_{t} — class collision, no alternative found\n"
                )
            else:
                sections.append(
                    build_entity_block(t, entity_entries[t], defined_iris, iri_index)
                )

    # SR
    if sr_raw:
        sections.append(
            build_sr_section(sr_raw, entity_entries, defined_iris, iri_index)
        )

    # HIDDEN
    if hidden:
        sections.append(
            build_hidden_section(hidden, entity_entries, defined_iris, iri_index)
        )

    # ── Step 7: Write TTL ────────────────────────────────────
    ttl_content = "\n".join(sections)
    with open(R2RML_FILE, "w", encoding="utf-8") as f:
        f.write(ttl_content)

    total_entity = len(entity_entries)
    written      = total_entity - len(dropped_tables)

    print(f"\n{'=' * 56}")
    print("  PHASE 7 COMPLETE")
    print(f"{'=' * 56}")
    print(f"  Entity tables total    : {total_entity}")
    print(f"  Collisions detected    : {len(collisions)}")
    print(f"  Remapped by LLM        : {len(remapped)}")
    print(f"  Dropped (no alt found) : {len(dropped_tables)}")
    print(f"  SR bridge tables       : {len(sr_raw)}")
    print(f"  Written to TTL         : {written} entity + {len(sr_raw)} SR")
    print(f"\n  Collision report → {COLLISION_REPORT}")
    print(f"  R2RML TTL        → {R2RML_FILE}\n")


# ============================================================
# Helper: apply a remap decision to entity_entries
# ============================================================

def _apply_remap(table_name: str, new_cls: Optional[str],
                 entity_entries: Dict, used_classes: Set[str],
                 remapped: Dict, dropped_tables: Set[str],
                 print_result: bool = False):
    """Apply a single LLM remap decision. Mutates entity_entries and tracking sets."""
    if new_cls:
        colon_cls = f":{new_cls}"
        if colon_cls in used_classes:
            # LLM suggested a class already taken → drop
            if print_result:
                print(f" ⚠ LLM suggested {colon_cls} but it's already used → drop")
            dropped_tables.add(table_name)
        else:
            if print_result:
                print(f" → :{new_cls}")
            entity_entries[table_name]["subject"]["class"] = colon_cls
            remapped[table_name] = new_cls
            used_classes.add(colon_cls)
    else:
        if print_result:
            print(f" → no suitable alternative → drop")
        dropped_tables.add(table_name)


if __name__ == "__main__":
    try:
        run_r2rml_generation()
    except Exception as e:
        print(f"\nFatal error: {e}")
        import traceback
        traceback.print_exc()