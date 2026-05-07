"""
Ontology Mapper — Phase 8: R2RML TTL Generator

Pure TTL generation. Reads all phase JSON mapping files (already
collision-resolved by phase 7) and produces a single valid R2RML
Turtle file. Contains NO collision detection or LLM calls.

NOTE: Class collisions are fully resolved in phase 7. By the time
phase 8 runs, every table has a unique class assignment (or a
_collision_unresolved marker for rare edge cases, which is emitted
as a comment rather than silently dropping the table).

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
          "sql_filter":      null or "col = value",
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
              "sql_filter":     "col_name = value",
              "subject": { "template": "...", "class": ":ClassName" }
            }
          ]
        }
      ]
    }

Reads  : src/outputs/mappings/SE_mappings.json          (required)
         src/outputs/mappings/SH_mappings.json           (optional)
         src/outputs/mappings/SEw_mappings.json          (optional)
         src/outputs/mappings/SRR_mappings.json          (optional)
         src/outputs/mappings/SR_mappings.json           (optional)
         src/outputs/mappings/HIDDEN_mappings.json       (optional)
         src/inputs/ontology/ontology.owl
Writes : src/outputs/mappings/mappings_r2rml.ttl
"""

import json
import re
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Set
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from parsers.ontology_explorer import ontology_explorer

# ===== PATHS =====
OUTPUT_DIR    = "src/outputs"
MAPPINGS_DIR  = os.path.join(OUTPUT_DIR, "mappings")
ONTOLOGY_FILE = "src/inputs/ontology/ontology.owl"
SE_FILE       = os.path.join(MAPPINGS_DIR, "SE_mappings.json")
SH_FILE       = os.path.join(MAPPINGS_DIR, "SH_mappings.json")
SEW_FILE      = os.path.join(MAPPINGS_DIR, "SEw_mappings.json")
SRR_FILE      = os.path.join(MAPPINGS_DIR, "SRR_mappings.json")
SR_FILE       = os.path.join(MAPPINGS_DIR, "SR_mappings.json")
HIDDEN_FILE   = os.path.join(MAPPINGS_DIR, "HIDDEN_mappings.json")
R2RML_FILE    = os.path.join(MAPPINGS_DIR, "mappings_r2rml.ttl")
DB_Structure = os.path.join(OUTPUT_DIR, "DB_as_json")
TABLES_STRUCTURE_FILE = os.path.join(DB_Structure, "tables_structure.json") 


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
    """
    Build a prefix map from the OWL file.

    For OWL/XML files: reads <Prefix name="..." IRI="..."/> elements.
    For RDF/XML files: reads xmlns: declarations from the root element.

    The empty-string key "" always holds the authoritative base IRI —
    the namespace that actually contains the ontology's own classes.
    This is obtained via ontology_explorer (which verifies the IRI against
    real class counts, correcting "document IRI != class namespace" cases
    like NPD where xml:base != the namespace of the classes).
    """
    import os as _os
    prefixes: Dict[str, str] = {}

    # ── Step 1: collect all declared prefix→IRI pairs from the file ──────────
    try:
        tree = ET.parse(owl_file)
        root = tree.getroot()

        # OWL/XML: explicit <Prefix> elements
        for elem in root.iter():
            local = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if local == "Prefix":
                name = elem.get("name", "")
                iri  = elem.get("IRI", "")
                if iri:
                    prefixes[name] = iri

        # RDF/XML: xmlns: declarations are invisible to ET — parse raw text
        if not prefixes:
            with open(owl_file, "r", encoding="utf-8", errors="replace") as f:
                head = f.read(4096)
            for m in re.finditer(
                r'xmlns(?::([a-zA-Z0-9_\-]*))?\s*=\s*"([^"]+)"', head
            ):
                ns_name = m.group(1) or ""
                ns_iri  = m.group(2)
                if ns_iri and ns_iri not in prefixes.values():
                    prefixes[ns_name] = ns_iri

    except ET.ParseError:
        # Fallback: regex scan
        try:
            with open(owl_file, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read(4096)
            for m in re.finditer(r'<Prefix\s+name="([^"]*)"\s+IRI="([^"]+)"', content):
                prefixes[m.group(1)] = m.group(2)
        except Exception:
            pass
    except FileNotFoundError:
        print(f"  [WARN] Ontology file not found: {owl_file}")
        return prefixes

    # ── Step 2: override "" with the authoritative base IRI from ontology_explorer
    # ontology_explorer verifies the IRI against actual class counts, so it
    # correctly handles cases where xml:base / ontologyIRI differs from the
    # namespace the classes actually live under (e.g. NPD: npd-v2-merge# vs npd-v2#).
    try:
        _prev = _os.environ.get("MPBOOT_ONTOLOGY_PATH", "")
        _os.environ["MPBOOT_ONTOLOGY_PATH"] = owl_file
        from parsers.ontology_explorer import (
            _load_ontology as _oe_load,
            _get_base_iri  as _oe_base,
            _OWL_ONTO, _BASE_IRI,
        )
        import parsers.ontology_explorer as _oe_mod
        # Reset if a different ontology was cached
        if _oe_mod._BASE_IRI and _oe_mod.ONTOLOGY_PATH != owl_file:
            _oe_mod._OWL_ONTO = None
            _oe_mod._BASE_IRI = None
            _oe_mod._LOADED_FROM = None
            _oe_mod._ALL_ONTOLOGY_NAMESPACES.clear()
        _oe_load()
        authoritative_base = _oe_base()
        if authoritative_base:
            prefixes[""] = authoritative_base
        if _prev:
            _os.environ["MPBOOT_ONTOLOGY_PATH"] = _prev
    except Exception as _e:
        print(f"  [WARN] ontology_explorer base IRI lookup failed: {_e} — using raw prefix")
        # Keep whatever "" we got from step 1 as fallback

    return prefixes


def get_base_iri(prefixes: Dict[str, str]) -> str:
    """Return the authoritative base IRI (always stored under key "")."""
    if "" in prefixes:
        iri = prefixes[""]
        return iri if iri.endswith("#") else iri.rstrip("/") + "#"
    standard = {"owl", "rdf", "rdfs", "xsd", "xml", "xsp", "swrl", "swrlb", "protege"}
    for name, iri in prefixes.items():
        if name not in standard:
            return iri if iri.endswith("#") else iri.rstrip("/") + "#"
    return "http://ontology#"


# ============================================================
# IRI index & broken reference fixer
# ============================================================

def build_iri_index(entity_entries: Dict, sr_raw: Dict) -> Dict[str, str]:
    """Build: logical_table_name → triple_map_iri"""
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
    If ref_iri is already in the defined set → return unchanged.
    Otherwise try to resolve via iri_index by progressively stripping
    leading underscore-separated segments from the IRI suffix.
    """
    if ref_iri in defined_iris:
        return ref_iri

    tail  = ref_iri.split(":")[-1]
    parts = tail.split("_")

    for start in range(1, len(parts)):
        candidate = "_".join(parts[start:])
        if candidate in iri_index:
            return iri_index[candidate]

    return ref_iri


# ============================================================
# Universal SQL identifier quoting & aliasing helpers
# ============================================================

# PostgreSQL reserved words that cannot be used as unquoted table names
# in rr:tableName — RODI passes them to the DB without quoting.
_PG_RESERVED = {
    "user", "order", "table", "select", "where", "group", "limit",
    "offset", "check", "column", "constraint", "default", "end",
    "from", "grant", "index", "into", "join", "like", "not", "null",
    "on", "only", "primary", "references", "set", "unique", "using",
    "values", "with", "all", "any", "as", "asc", "authorization",
    "between", "by", "case", "cast", "collate", "cross", "current_date",
    "current_time", "current_timestamp", "desc", "distinct", "else",
    "except", "exists", "fetch", "for", "foreign", "full", "having",
    "inner", "intersect", "interval", "is", "leading", "left", "local",
    "natural", "no", "outer", "over", "partition", "placing",
    "returning", "right", "row", "similar", "some", "symmetric",
    "then", "to", "trailing", "union", "verbose", "when", "window",
    "in", "and", "or", "true", "false",
}


def _needs_sql_query(name: str, template_cols: list = None,
                     extra_cols: list = None) -> bool:
    """
    True when rr:sqlQuery is required instead of rr:tableName:
    1. Table name is a PostgreSQL reserved word (RODI passes it unquoted).
    2. Table name is mixed-case or contains a hyphen.
    3. Any template column or extra column is mixed-case or contains a hyphen.
    """
    if name.lower() in _PG_RESERVED:
        return True
    if "-" in name or name != name.lower():
        return True
    for col in (template_cols or []) + (extra_cols or []):
        if col != col.lower() or "-" in col:
            return True
    return False


def _safe_alias(col: str) -> str:
    """Lowercase a column name and replace hyphens with underscores."""
    return col.lower().replace("-", "_")


def _lower_template(template: str) -> str:
    """Lowercase all {PLACEHOLDER} names and replace hyphens with underscores."""
    return re.sub(r"\{([^}]+)\}", lambda m: "{" + _safe_alias(m.group(1)) + "}", template)


def _extract_template_cols(template: str) -> list:
    """Return list of placeholder names from a template string."""
    return re.findall(r"\{([^}]+)\}", template)


_SQL_KEYWORDS = {"IS", "NOT", "NULL", "TRUE", "FALSE", "AND", "OR",
                 "IN", "LIKE", "BETWEEN", "AS", "WHERE", "SELECT", "FROM"}


def _quote_filter(sql_filter: str) -> str:
    """Quote only the column identifier in a WHERE clause (left side of = or IS).
    Never quotes words inside the right-hand side value.
    """
    def _quoter(m):
        ident = m.group(1)
        if ident.upper() in _SQL_KEYWORDS:
            return m.group(0)
        return '"' + ident + '"' + m.group(0)[len(ident):]
    # Only match identifiers immediately followed by = or IS (column names only)
    # Pattern built from parts to avoid escape corruption during file writes
    pattern = r'(?<!")\b([A-Za-z_][A-Za-z0-9_-]*)\b(?=\s*(=|IS[\s$]))'
    return re.sub(pattern, _quoter, sql_filter)

def _build_star_sql(table_name: str,
                    hyphen_cols: list = None,
                    where_clause: str = None) -> str:
    """
    Build a SELECT * query, with explicit aliases ONLY for columns whose
    original name contains a hyphen (e.g. "col-name" AS col_name).
    PostgreSQL treats '-' as subtraction, so those columns must be aliased
    so that R2RML templates using {col_name} (underscore form) resolve correctly.

    hyphen_cols : list of original column names that contain '-'
    where_clause: optional WHERE clause string (already quoted/safe)
    """
    if hyphen_cols:
        alias_parts = ', '.join(
            f'"{c}" AS {_safe_alias(c)}' for c in hyphen_cols
        )
        select_clause = f'*, {alias_parts}'
    else:
        select_clause = '*'

    sql = f'SELECT {select_clause} FROM "{table_name}"'
    if where_clause:
        sql += f" WHERE {where_clause}"
    return sql


def _collect_hyphen_cols(names: list) -> list:
    """Return only the names from the list that contain a hyphen."""
    return [n for n in names if '-' in n]


# ============================================================
# TTL building blocks
# ============================================================

# Datatypes that must be explicitly declared.
# xsd:string is intentionally EXCLUDED — plain literals match SQL strings correctly.
# Adding xsd:string causes "foo"^^xsd:string != "foo" mismatches in RODI comparison.
# xsd:decimal → xsd:double mapping:
# The ontology declares rdfs:range xsd:decimal for many properties. When RODI's
# reasoner infers types, DB values like 4.0E-4 get typed as xsd:decimal which
# Sesame rejects (xsd:decimal does not allow scientific notation).
# Solution: emit xsd:double explicitly for decimal columns — xsd:double accepts
# scientific notation and is semantically equivalent for petroleum measurements.
_DECIMAL_TO_DOUBLE = {"xsd:decimal", "xsd:numeric"}

_EMIT_DATATYPE = {
    "xsd:integer", "xsd:int", "xsd:long", "xsd:short",
    "xsd:float", "xsd:double",
    "xsd:boolean", "xsd:bool",
    # xsd:date and xsd:dateTime intentionally excluded:
    # some R2RML engines crash when casting NULL or non-standard
    # date strings to xsd:date even with IS NOT NULL filter.
    # Plain literals work correctly for date comparison in RODI.
    "xsd:anyURI",
    "xsd:byte", "xsd:nonNegativeInteger", "xsd:positiveInteger",
    "xsd:unsignedLong", "xsd:unsignedInt",
}


def _pom_literal(pred: str, col: str, datatype: str) -> List[str]:
    # Remap xsd:decimal → xsd:double to avoid Sesame rejecting scientific notation
    if datatype in _DECIMAL_TO_DOUBLE:
        datatype = "xsd:double"
    if datatype in ("xsd:anyURI", "http://www.w3.org/2001/XMLSchema#anyURI"):
        return [
            f"    rr:predicateObjectMap [",
            f"        rr:predicate {pred} ;",
            f"        rr:objectMap  [",
            f"            rr:column    \"{_safe_alias(col)}\" ;",
            f"            rr:termType  rr:IRI ;",
            f"        ] ;",
            f"    ] ;",
            f"",
        ]
    if datatype in _EMIT_DATATYPE:
        return [
            f"    rr:predicateObjectMap [",
            f"        rr:predicate {pred} ;",
            f"        rr:objectMap  [",
            f"            rr:column   \"{_safe_alias(col)}\" ;",
            f"            rr:datatype {datatype} ;",
            f"        ] ;",
            f"    ] ;",
            f"",
        ]
    # Plain literal — no rr:datatype (xsd:string and unknowns)
    return [
        f"    rr:predicateObjectMap [",
        f"        rr:predicate {pred} ;",
        f"        rr:objectMap  [",
        f"            rr:column   \"{_safe_alias(col)}\" ;",
        f"        ] ;",
        f"    ] ;",
        f"",
    ]

def _pom_join(pred: str, parent_iri: str,
              child_col: str, parent_col: str) -> List[str]:
    return [
        f"    rr:predicateObjectMap [",
        f"        rr:predicate {pred} ;",
        f"        rr:objectMap  [",
        f"            rr:parentTriplesMap <{parent_iri}> ;",
        f"            rr:joinCondition [",
        f"                rr:child  \"{_safe_alias(child_col)}\" ;",
        f"                rr:parent \"{_safe_alias(parent_col)}\" ;",
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
                  iri_index: Dict[str, str],
                  owner_iri: str = "") -> List[str]:
    """Build TTL lines for all predicate-object maps."""
    lines = []
    for pom in poms:
        pred  = pom.get("predicate", "")
        obj   = pom.get("object", {})
        otype = obj.get("type", "")

        if otype == "literal":
            lines += _pom_literal(pred, obj["column"], obj["datatype"])

        elif otype == "join":
            raw_ref = obj.get("parent_triples_map", "")
            fixed   = fix_iri(raw_ref, defined_iris, iri_index)
            if fixed != raw_ref:
                lines.append(f"    # [auto-fixed] {raw_ref} → {fixed}")
            jc        = obj.get("join_condition", {})
            child_col = jc.get("child", "")
            parent_col = jc.get("parent", "")
            if not child_col or not parent_col:
                lines.append(
                    f"    # [SKIPPED JOIN] {pred}: missing child/parent in join_condition "
                    f"(child={child_col!r}, parent={parent_col!r}) — "
                    f"would have emitted 'id' fallback causing unknown column error"
                )
                print(f"  [WARN] Skipping join POM for {pred!r}: "
                      f"join_condition missing child={child_col!r} parent={parent_col!r} "
                      f"in {owner_iri!r}")
                continue
            lines += _pom_join(pred, fixed, child_col, parent_col)
    return lines


def _make_table_line(table_name: str, all_col_names: list,
                     entry: Dict, sql_filter: Optional[str] = None) -> str:
    """Compute the rr:logicalTable line for a given table/entry/filter combo.

    IMPORTANT: always use triple-double-quote (\"\"\" ... \"\"\") as the Turtle
    long-string delimiter for rr:sqlQuery.  Triple-single-quote conflicts with
    SQL string literals that contain single quotes (e.g. WHERE col = 'value').
    """
    _q3 = "'''"
    if sql_filter:
        safe_filter = _quote_filter(sql_filter)
        hyphen_cols = _collect_hyphen_cols(list(dict.fromkeys(all_col_names)))
        sql         = _build_star_sql(table_name, hyphen_cols, safe_filter)
        return f"    rr:logicalTable [ rr:sqlQuery {_q3}{sql}{_q3} ] ;"
    elif entry.get("logical_table_sql"):
        sql = entry["logical_table_sql"]
        return f"    rr:logicalTable [ rr:sqlQuery {_q3}{sql}{_q3} ] ;"
    elif (entry.get("pattern") == "SE_SH"
          and entry.get("parent_table")
          and not entry.get("predicate_object_maps")):
        parent = entry["parent_table"]
        # Use the actual PK columns from the entry template instead of hardcoded "id"
        tmpl_cols = _extract_template_cols(entry.get("subject", {}).get("template", ""))
        pk_col    = tmpl_cols[0] if tmpl_cols else "id"
        sql = f'SELECT p.* FROM "{table_name}" t JOIN "{parent}" p ON t.{pk_col} = p.{pk_col}'
        return f"    rr:logicalTable [ rr:sqlQuery {_q3}{sql}{_q3} ] ;"
    elif _needs_sql_query(table_name, all_col_names):
        hyphen_cols = _collect_hyphen_cols(list(dict.fromkeys(all_col_names)))
        sql         = _build_star_sql(table_name, hyphen_cols)
        return f"    rr:logicalTable [ rr:sqlQuery {_q3}{sql}{_q3} ] ;"
    else:
        return f'    rr:logicalTable [ rr:tableName "{table_name}" ] ;'


def build_entity_block(table_name: str, entry: Dict,
                       defined_iris: Set[str], iri_index: Dict[str, str],
                       sql_filter: Optional[str] = None,
                       tables_structure: Dict = None) -> str:
    """
    Generic TTL block builder for SE, SE_SH, SEw, SRR, and HIDDEN patterns.

    Splitting rule (non-HIDDEN only):
      When a TriplesMap has BOTH rr:class AND any join-type POM (direct join
      or junction_join), it is split into two TriplesMap blocks:

        <iri>         typing map  -- rr:class + literal POMs, full table scan
        <iri>_joins   join map    -- join POMs only, no rr:class, same template

      This prevents R2RML engines that skip NULL-FK rows from also dropping
      the rdf:type triple, which causes CLASS COUNT queries to return 0.

      HIDDEN entries (sql_filter is not None) are never split -- they already
      have a WHERE clause guaranteeing the FK is NOT NULL.
    """
    iri  = entry["triple_map_iri"]
    cls  = entry["subject"].get("class")
    pat  = entry.get("pattern", "")
    _q3  = "'''"

    collision_note = entry.get("_collision_note", "")
    tmpl = _lower_template(entry["subject"]["template"])

    # Deduplicate predicate_object_maps: remove exact-duplicate POMs that
    # accumulate in the JSON from multiple phase 5b runs on the same file.
    # Two POMs are duplicates if they share the same predicate AND the same
    # object definition (type, parent_triples_map, join_condition, column).
    raw_poms = entry.get("predicate_object_maps", [])
    seen_pom_keys = set()
    poms = []
    for p in raw_poms:
        obj  = p.get("object", {})
        otype = obj.get("type", "")
        if otype == "literal":
            key = (p.get("predicate",""), otype, obj.get("column",""), obj.get("datatype",""))
        elif otype in ("join", "junction_join"):
            jc  = obj.get("join_condition", {})
            key = (p.get("predicate",""), otype,
                   obj.get("parent_triples_map",""),
                   jc.get("child",""), jc.get("parent",""))
        else:
            key = (p.get("predicate",""), otype, obj.get("parent_triples_map",""))
        if key not in seen_pom_keys:
            seen_pom_keys.add(key)
            poms.append(p)

    # Collect all referenced column names for sqlQuery / hyphen-alias decisions
    all_col_names = _extract_template_cols(entry["subject"]["template"])
    for p in poms:
        obj = p.get("object", {})
        if obj.get("type") == "literal":
            all_col_names.append(obj["column"])
        elif obj.get("type") == "join":
            jc = obj.get("join_condition", {})
            if jc.get("child"):
                all_col_names.append(jc["child"])

    # Classify POMs
    join_poms    = [p for p in poms
                    if p.get("object", {}).get("type") in ("join", "junction_join")
                    and p.get("object", {}).get("resolved", True)]
    literal_poms = [p for p in poms
                    if p.get("object", {}).get("type") == "literal"]
    other_poms   = [p for p in poms
                    if p.get("object", {}).get("type")
                    not in ("literal", "join", "junction_join")]

    # Split rule: only split when join POMs are present.
    # Literal POMs stay in the typing map — they are safe as plain literals
    # (xsd:date and xsd:dateTime are excluded from _EMIT_DATATYPE so they
    # always emit as plain literals, no engine cast crash).
    # Keeping literals in the typing map avoids the old problem where the
    # typing map had zero POMs but a separate prop map crashed on xsd:date,
    # aborting the entire engine run and killing the rdf:type triples too.
    needs_split = (
        bool(cls)                                      # has a class declaration
        and (bool(join_poms) or bool(literal_poms))   # split when any POMs exist
        and sql_filter is None                         # not a HIDDEN entry
    )

    sep = chr(9472) * max(0, 48 - len(pat) - len(table_name))

    # ── Inner helper: build one TriplesMap block ─────────────────────────
    def _block(block_iri, block_cls, block_poms, block_table_line, label):
        lines = []
        if collision_note:
            lines.append(f"# \u26a0 COLLISION WARNING: {collision_note}")
        lines += [
            f"# \u2500\u2500 {label} {sep}",
            f"<{block_iri}>",
            f"    a rr:TriplesMap ;",
            f"",
            block_table_line,
            f"",
            f"    rr:subjectMap [",
            f'        rr:template "{tmpl}" ;',
        ]
        if block_cls:
            lines.append(f"        rr:class     {block_cls} ;")
        lines += [f"    ] ;", f""]
        lines += _resolve_poms(block_poms, defined_iris, iri_index, owner_iri=block_iri)
        return _close(lines)

    if needs_split:
        result_blocks = []

        # ── Typing map: class + literals (stay together for RODI compatibility)
        # Literal POMs stay with the typing block — RODI needs class and data
        # properties in the same TriplesMap to satisfy queries like:
        # ?s rdf:type :C; :prop ?v  (without cross-TriplesMap SPARQL joins)
        # Only columns that exist in the real table are included.
        _own_col_set = None
        if tables_structure:
            _raw_cols = {
                c["name"].lower()
                for c in tables_structure.get(table_name, {}).get("columns", [])
            }
            if _raw_cols:
                _own_col_set = _raw_cols

        safe_literal_poms = []
        for p in literal_poms + other_poms:
            col_name = p.get("object", {}).get("column", "")
            if not col_name:
                continue
            if _own_col_set and col_name.lower() not in _own_col_set:
                print(f"  [SKIP-prop] {table_name}.{col_name} — not in table schema, came from JOIN")
                continue
            safe_literal_poms.append(p)

        typing_col_names = _extract_template_cols(entry["subject"]["template"])
        for p in safe_literal_poms:
            typing_col_names.append(p["object"]["column"])
        typing_table_line = _make_table_line(table_name, typing_col_names, entry)
        typing_block = _block(
            iri, cls,
            safe_literal_poms,
            typing_table_line,
            f"{pat}_{table_name} [typing]",
        )
        result_blocks.append(typing_block)

        # ── Join map: joins only, no class ────────────────────────────────
        # ALWAYS use sqlQuery for join maps — RODI rebuilds child subquery
        # from declared columns only, so rr:tableName hides FK child columns.
        if join_poms:
            join_col_names = _extract_template_cols(entry["subject"]["template"])
            for p in join_poms:
                obj = p.get("object", {})
                if obj.get("type") == "join":
                    jc = obj.get("join_condition", {})
                    if jc.get("child"):
                        join_col_names.append(jc["child"])
            hyphen_cols     = _collect_hyphen_cols(list(dict.fromkeys(join_col_names)))
            join_sql        = _build_star_sql(table_name, hyphen_cols)
            # ALWAYS use sqlQuery for join maps
            _q3j            = "'''"
            join_table_line = f"    rr:logicalTable [ rr:sqlQuery {_q3j}{join_sql}{_q3j} ] ;"
            join_iri        = iri + "_joins"
            join_block = _block(
                join_iri, None,
                join_poms,
                join_table_line,
                f"{pat}_{table_name} [joins]",
            )
            result_blocks.append(join_block)

        return "\n".join(result_blocks)

    else:
        # ── No split needed: single block (HIDDEN or no POMs) ────────────
        table_line = _make_table_line(table_name, all_col_names, entry, sql_filter)
        return _block(iri, cls, poms, table_line, f"{pat}_{table_name}")


def _get_subject_template(triple_map_iri: str,
                          entity_entries: Dict) -> Optional[str]:
    """Look up the subject template for a given TriplesMap IRI."""
    for entry in entity_entries.values():
        if entry.get("triple_map_iri") == triple_map_iri:
            return entry.get("subject", {}).get("template")
    return None


def _adapt_template_for_bridge(tmpl: str, s_join: Dict) -> str:
    """
    Replace entity PK placeholder in template with the bridge FK column.
    The bridge table only has FK columns, not the entity's own PK.
    """
    fk_col    = s_join.get("child", "")
    entity_pk = s_join.get("parent", "")
    if not fk_col or not entity_pk:
        print(f"  [WARN] _adapt_template_for_bridge: missing child={fk_col!r} "
              f"or parent={entity_pk!r} in join — template left unchanged: {tmpl!r}")
        return tmpl
    return tmpl.replace(f"{{{entity_pk}}}", f"{{{fk_col}}}")


def build_sr_section(sr_raw: Dict, entity_entries: Dict,
                     defined_iris: Set[str],
                     iri_index: Dict[str, str]) -> str:
    """Build TTL for all SR bridge tables."""
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
        # Skip axiom typing entries — handled by _build_axiom_typing_blocks
        if entry.get("pattern") == "SR_AXIOM_TYPING":
            continue

        bridge_name_blocks = []

        for m in entry.get("mappings", []):
            subj_iri = fix_iri(m["subject_triples_map"], defined_iris, iri_index)
            obj_iri  = fix_iri(m["object_triples_map"],  defined_iris, iri_index)
            pred     = m.get("predicate", "")

            pair_key = (frozenset([subj_iri, obj_iri]), pred)
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)

            subj_tmpl = _get_subject_template(subj_iri, entity_entries)
            if subj_tmpl is None:
                blocks.append(
                    f"# SKIPPED SR_{bridge_table}: "
                    f"subject template not found for <{subj_iri}>\n"
                )
                continue

            s_join = m.get("subject_join", {})
            o_join = m.get("object_join", {})

            subj_tmpl = _adapt_template_for_bridge(subj_tmpl, s_join)

            subj_tag = subj_iri.split("_")[-1]
            obj_tag  = obj_iri.split("_")[-1]
            # Include predicate name in IRI to prevent duplicates when
            # multiple predicates connect the same entity pair
            # (e.g. :submit AND :presentation both on persons→documents)
            pred_tag = pred.lstrip(":").replace("-", "_").replace(":", "_")
            sr_iri   = f"{entry['triple_map_iri']}_{subj_tag}_{obj_tag}_{pred_tag}"

            _q3b = "'''"
            s_child     = s_join.get("child", "")
            o_child_col = o_join.get("child", "")
            if not s_child or not o_child_col:
                blocks.append(
                    f"# SKIPPED SR_{bridge_table} ({subj_tag}→{obj_tag}): "
                    f"missing FK child column in join (s_child={s_child!r}, "
                    f"o_child={o_child_col!r})\n"
                )
                print(f"  [WARN] Skipping SR_{bridge_table}: "
                      f"s_child={s_child!r} o_child={o_child_col!r}")
                continue
            # SR: SELECT * so all bridge columns are available.
            # Aliases only for hyphenated FK column names.
            hyphen_cols = _collect_hyphen_cols(
                list(dict.fromkeys([s_child, o_child_col]))
            )
            _br_sql   = _build_star_sql(bridge_table, hyphen_cols)
            subj_tmpl = _lower_template(subj_tmpl)

            lines = [
                f"# ── SR_{bridge_table} ({subj_tag} → {obj_tag}) {'─' * 10}",
                f"<{sr_iri}>",
                f"    a rr:TriplesMap ;",
                f"",
                f"    rr:logicalTable [ rr:sqlQuery {_q3b}{_br_sql}{_q3b} ] ;",
                f"",
                f"    rr:subjectMap [",
                f'        rr:template "{subj_tmpl}" ;',
                f"    ] ;",
                f"",
            ]
            lines += _pom_join(pred, obj_iri,
                               _safe_alias(o_child_col),
                               _safe_alias(o_join.get("parent", "")))
            bridge_name_blocks.append(_close(lines))

        if bridge_name_blocks:
            blocks.extend(bridge_name_blocks)
        else:
            blocks.append(
                f"# SR_{bridge_table}: all directions deduplicated or skipped\n"
            )

    return "\n".join(blocks)


def _build_axiom_typing_blocks(sr_raw: Dict) -> str:
    """
    Build TTL for SR_AXIOM_TYPING entries — EquivalentClasses materialization.

    Each entry produces a simple TriplesMap that adds rdf:type to entities
    participating in SR relationships. For example:

        Author ≡ ∃submit.Paper
        → persons in document_person.pid get rdf:type :Author
        → documents in document_person.did get rdf:type :Paper

    Output:
        <urn:r2rml:AXIOM_document_person_Author>
            a rr:TriplesMap ;
            rr:logicalTable [ rr:sqlQuery '''SELECT DISTINCT "pid" FROM "document_person"''' ] ;
            rr:subjectMap [
                rr:template "http://sigkdd#persons/{pid}" ;
                rr:class :Author ;
            ] .
    """
    if not sr_raw:
        return ""

    # Collect axiom typing entries
    axiom_entries = {
        k: v for k, v in sr_raw.items()
        if v.get("pattern") == "SR_AXIOM_TYPING"
    }

    if not axiom_entries:
        return ""

    blocks = [
        "# " + "═" * 52,
        "# AXIOM TYPING — EquivalentClasses materialization",
        "# " + "═" * 52,
        "",
    ]

    _q3b = "'''"

    for entry_key, entry in axiom_entries.items():
        iri = entry.get("triple_map_iri", f"urn:r2rml:AXIOM_{entry_key}")
        logical_table = entry.get("logical_table", "")
        typing_class = entry.get("typing_class", "")
        subject_template = entry.get("subject_template", "")
        source_column = entry.get("source_column", "")
        axiom_info = entry.get("axiom", {})
        reason = entry.get("_reason", "")

        if not all([logical_table, typing_class, subject_template, source_column]):
            blocks.append(
                f"# SKIPPED {entry_key}: missing required fields "
                f"(table={logical_table!r}, class={typing_class!r}, "
                f"tmpl={subject_template!r}, col={source_column!r})\n"
            )
            continue

        # Lowercase the template placeholder for PostgreSQL compatibility
        subject_template_lower = subject_template.lower()

        # Build SQL: SELECT DISTINCT "col" FROM "table"
        sql = f'SELECT DISTINCT "{source_column}" FROM "{logical_table}"'

        # Comment with axiom info
        equiv = axiom_info.get("equiv_class", "?")
        prop = axiom_info.get("property", "?")
        filler = axiom_info.get("filler", "?")

        lines = [
            f"# ── AXIOM: {equiv} ≡ ∃{prop}.{filler} ──────────",
            f"<{iri}>",
            f"    a rr:TriplesMap ;",
            f"",
            f"    rr:logicalTable [ rr:sqlQuery {_q3b}{sql}{_q3b} ] ;",
            f"",
            f"    rr:subjectMap [",
            f'        rr:template "{subject_template_lower}" ;',
            f"        rr:class     {typing_class} ;",
            f"    ]  .",
            f"",
            f"",
        ]
        blocks.extend(lines)

    return "\n".join(blocks)


_BOOL_TYPES_P8 = {"boolean", "bool"}
_STR_TYPES_P8  = {"varchar", "text", "char", "character", "character varying",
                  "nvarchar", "nchar"}


def _make_filter_from_schema(col_name: str, filter_value: str,
                              data_type: str, is_bool_flag: bool = False) -> str:
    """
    Build a type-correct SQL WHERE clause from the authoritative data_type
    (tables_structure.json) and the filter_value stored by phase 6.

    filter_value is the ACTUAL value from DB samples (e.g. "t", "1", "true").
    data_type is the PostgreSQL column type from tables_structure.json.

    PostgreSQL-safe:
      boolean/bool  -> col = true   (boolean literal, valid for both t/f and true/false stored values)
      integer flag  -> col = 1      (integer literal; col=true raises type mismatch)
      string        -> col = 'val'
      other         -> col = <raw>
    """
    dt = data_type.lower().split("(")[0].strip() if data_type else ""
    fv = str(filter_value)

    if dt in _BOOL_TYPES_P8:
        bv = "true" if fv.lower() in ("1", "true", "t", "yes") else "false"
        return f"{col_name} = {bv}"

    if is_bool_flag:
        iv = 1 if fv.lower() in ("1", "true", "t", "yes") else 0
        return f"{col_name} = {iv}"

    if dt in _STR_TYPES_P8:
        # Escape every ' with \' so value is safe inside Turtle '''...''' delimiter
        fv = fv.replace("'", "\\'")
        return f"{col_name} = \\'{fv}\\'"

    return f"{col_name} = {fv}"


def _lookup_col_type(table_name: str, col_name: str, tables_structure: Dict) -> str:
    for col in tables_structure.get(table_name, {}).get("columns", []):
        if col["name"] == col_name:
            return col.get("data_type", "")
    return ""


def build_hidden_section(hidden_raw: Dict, entity_entries: Dict,
                         defined_iris: Set[str],
                         iri_index: Dict[str, str],
                         tables_structure: Dict = None) -> str:
    """Build TTL for HIDDEN pattern.

    Every sql_filter is re-derived from tables_structure.json at generation time.
    The sql_filter string stored in HIDDEN_mappings.json is never trusted for
    type correctness — it exists only as a last-resort fallback.

    Key fields read from HIDDEN_mappings.json:
      hidden_sh[].trigger_column_type  -> authoritative data_type
      hidden_sh[].filter_value         -> actual DB value for the WHERE clause
      type_dispatch[].discriminator_type
      type_dispatch[].dispatch[].filter_value
      type_dispatch[].dispatch[].filter_column_type
    """
    if not hidden_raw:
        return ""

    ts = tables_structure or {}

    blocks = [
        "# " + "=" * 52,
        "# HIDDEN - Hidden Subclasses & Type Dispatch",
        "# " + "=" * 52,
        "",
    ]

    for table_name, entry in hidden_raw.items():

        # ── BOOL_FLAG and HIDDEN_SH ──────────────────────────────────────────
        for hsh in entry.get("hidden_sh", []):
            subj = hsh.get("subject", {})
            if not subj.get("class") or not subj.get("template"):
                continue

            col_name     = hsh.get("trigger_column", "")
            pattern      = hsh.get("hidden_pattern", "HIDDEN_SH")
            stored_type  = hsh.get("trigger_column_type", "")
            data_type    = stored_type or _lookup_col_type(table_name, col_name, ts)
            filter_value = hsh.get("filter_value", "true")

            if col_name:
                if pattern == "BOOL_FLAG":
                    dt          = data_type.lower().split("(")[0].strip() if data_type else ""
                    is_int_bool = bool(dt) and dt not in _BOOL_TYPES_P8
                    sql_filter  = _make_filter_from_schema(
                        col_name, filter_value,
                        data_type or "integer",
                        is_bool_flag=is_int_bool
                    )
                else:
                    sql_filter = f"{col_name} IS NOT NULL"
            else:
                sql_filter = hsh.get("sql_filter") or None

            fake_entry = {
                "triple_map_iri":        hsh["triple_map_iri"],
                "subject":               subj,
                "pattern":               pattern,
                "predicate_object_maps": hsh.get("predicate_object_maps", []),
            }
            blocks.append(
                build_entity_block(table_name, fake_entry,
                                   defined_iris, iri_index,
                                   sql_filter=sql_filter)
            )

        # ── Type Dispatch ────────────────────────────────────────────────────
        # Look up the parent entity's template and data POMs
        parent_template = None
        parent_data_poms = []
        for phase_data in [entity_entries]:
            parent_entry = phase_data.get(table_name)
            if parent_entry:
                parent_template = parent_entry.get("subject", {}).get("template")
                for pom in parent_entry.get("predicate_object_maps", []):
                    if pom.get("object", {}).get("type") == "literal":
                        parent_data_poms.append(pom)
                break

        for td in entry.get("type_dispatch", []):
            col      = td.get("discriminator_column", "type")
            col_type = (td.get("discriminator_type", "")
                        or _lookup_col_type(table_name, col, ts))

            for dispatch in td.get("dispatch", []):
                subj = dispatch.get("subject", {})
                if not subj.get("class") or not subj.get("template"):
                    continue

                # Inherit template from parent SE/SE_SH entry
                if parent_template:
                    subj = {**subj, "template": parent_template}

                filter_value      = str(dispatch.get("filter_value", "?"))
                dispatch_col_type = dispatch.get("filter_column_type", col_type)

                if dispatch_col_type:
                    sql_filter = _make_filter_from_schema(
                        col, filter_value, dispatch_col_type, is_bool_flag=False
                    )
                else:
                    sql_filter = dispatch.get("sql_filter", f"{col} = {filter_value}")

                fake_entry = {
                    "triple_map_iri":        dispatch["triple_map_iri"],
                    "subject":               subj,
                    "pattern":               "TYPE_DISPATCH",
                    "predicate_object_maps": list(parent_data_poms),
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


def build_prefix_block(base_iri: str, extra_prefixes: Dict[str, str] = None) -> str:
    """
    Build the Turtle @prefix header.

    Always emits the four R2RML standard prefixes plus @prefix : <base_iri>.
    Also emits any additional ontology-specific namespaces from extra_prefixes
    so that classes/properties from non-base namespaces (e.g. npd-v2-ptl:,
    skos:, foaf:) are correctly resolvable in the generated TTL.

    Standard W3C prefixes (owl, rdf, rdfs, xsd, xml) that duplicate the
    already-emitted hardcoded lines are skipped to avoid duplicate declarations.
    """
    SKIP_NAMES = {"", "owl", "rdf", "rdfs", "xsd", "xml",
                  "xsp", "swrl", "swrlb", "protege"}
    SKIP_IRIS  = {
        "http://www.w3.org/2002/07/owl#",
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
        "http://www.w3.org/2000/01/rdf-schema#",
        "http://www.w3.org/2001/XMLSchema#",
        "http://www.w3.org/XML/1998/namespace",
        "http://www.w3.org/ns/r2rml#",
    }

    lines = [
        "@prefix rr:   <http://www.w3.org/ns/r2rml#> .",
        "@prefix xsd:  <http://www.w3.org/2001/XMLSchema#> .",
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .",
        "@prefix rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .",
        f"@prefix :     <{base_iri}> .",
    ]

    if extra_prefixes:
        for name, iri in sorted(extra_prefixes.items()):
            if name in SKIP_NAMES:
                continue
            ns = iri if iri.endswith("#") or iri.endswith("/") else iri + "#"
            if ns in SKIP_IRIS or ns == base_iri:
                continue
            # Sanitise prefix name: Turtle prefix must be a valid NCName
            safe_name = re.sub(r"[^a-zA-Z0-9_\-]", "_", name)
            if not safe_name:
                continue
            lines.append(f"@prefix {safe_name}:  <{ns}> .")

    lines.append("")
    return "\n".join(lines)


def _fix_id_templates(entity_entries: Dict, tables_structure: Dict) -> int:
    """
    Repair subject templates that use the generic {id} placeholder from
    SH mapper fallback. Only rebuilds templates that are DEMONSTRABLY broken
    (missing the table name, or using '{id}' where a real PK is expected).

    CRITICAL: Does NOT rebuild correct templates like 'document/{id}' just
    because they contain '{id}'. The template is correct IF:
      - It already ends with a {pk} that is a real PK column, AND
      - The base path matches either the table's own name OR an ancestor's
        name (for SE_SH inheritance: paper's template can use 'document/')

    Only rebuilds when:
      - The template ends in just '{id}' but 'id' is NOT actually a PK column,
      - OR the path component before {id} doesn't match this table OR any
        ancestor (via parent_table chain).
    """
    fixed = 0
    for table_name, entry in entity_entries.items():
        subj = entry.get("subject", {})
        tmpl = subj.get("template", "")
        if not tmpl or "{" not in tmpl:
            continue

        pks = tables_structure.get(table_name, {}).get("primary_keys", [])
        if not pks:
            continue

        # Extract the placeholder(s) currently in the template
        import re as _re
        placeholders = _re.findall(r'\{([^}]+)\}', tmpl)
        own_cols = {c["name"] for c in tables_structure.get(table_name, {}).get("columns", [])}

        # Is the template already correct?
        # It's correct if: all placeholders are real columns in THIS table,
        # AND the base path references this table or its parent chain.
        all_placeholders_valid = all(p in own_cols for p in placeholders)

        # Extract base path (everything before the first placeholder)
        base_match = _re.match(r'^(.*?)(?=/\{)', tmpl)
        base_path = base_match.group(1) if base_match else ""
        # Get last path segment (after # or /)
        last_seg = base_path.rsplit("/", 1)[-1].rsplit("#", 1)[-1]

        # Check if last_seg is this table or any ancestor (SE_SH parent chain)
        parent_chain = {table_name}
        parent_tbl = entry.get("parent_table", "")
        max_depth = 10
        while parent_tbl and max_depth > 0:
            parent_chain.add(parent_tbl)
            # Walk up — find parent's entry in entity_entries if present
            parent_entry = entity_entries.get(parent_tbl, {})
            parent_tbl = parent_entry.get("parent_table", "")
            max_depth -= 1

        path_is_valid = last_seg in parent_chain

        # If template is already correct, LEAVE IT ALONE
        if all_placeholders_valid and path_is_valid:
            continue

        # Template is broken — rebuild it properly
        cols = tables_structure.get(table_name, {}).get("columns", [])
        # Build FK set from multiple sources for robustness
        fk_names = set()
        for c in cols:
            if c.get("is_foreign_key"):
                fk_names.add(c["name"])
            if c.get("foreign_key_reference") or c.get("fk_references") or c.get("references"):
                fk_names.add(c["name"])
        for fk in tables_structure.get(table_name, {}).get("foreign_keys", []):
            col_name = fk.get("column") or fk.get("from_column") or fk.get("name")
            if col_name:
                fk_names.add(col_name)

        # Exclude FK PKs from template — they're relationships, not identity
        non_fk_pks = [pk for pk in pks if pk not in fk_names]
        template_pks = non_fk_pks if non_fk_pks else pks

        # Preserve parent's base path for SE_SH tables
        iri_base = _re.match(r'(https?://[^#]+#)', tmpl)
        prefix = iri_base.group(1) if iri_base else ""

        # If current base_path already references an ancestor, keep it
        if last_seg in parent_chain and last_seg != table_name:
            new_tmpl = base_path + "/" + "/".join(f"{{{pk}}}" for pk in template_pks)
        else:
            new_tmpl = f"{prefix}{table_name}/" + "/".join(f"{{{pk}}}" for pk in template_pks)

        if new_tmpl != tmpl:
            entry["subject"] = {**subj, "template": new_tmpl}
            print(f"  [fix-template] {table_name}: {tmpl} → {new_tmpl}")
            fixed += 1

    return fixed


# ============================================================
# Main entry point
# ============================================================

def run_r2rml_generation():

    print("=" * 56)
    print("  ONTOLOGY MAPPER — Phase 8 (R2RML TTL Generator)")
    print("=" * 56)
    os.makedirs(MAPPINGS_DIR, exist_ok=True)

    # ── Step 1: Load all phase files ────────────────────────
    # These files have already been collision-resolved by phase 7.
    print("\nLoading phase JSON files...")
    se_raw  = load_json_safe(SE_FILE)
    sh_raw  = load_json_optional(SH_FILE,    "SE_SH  ")
    sew_raw = load_json_optional(SEW_FILE,   "SEw    ")
    srr_raw = load_json_optional(SRR_FILE,   "SRR    ")
    sr_raw  = load_json_optional(SR_FILE,    "SR     ")
    hidden           = load_json_optional(HIDDEN_FILE, "HIDDEN ")
    tables_structure = load_json_optional(TABLES_STRUCTURE_FILE, "tables_structure")
    print(f"  SE     : {len(se_raw)} tables (required)")

    # ── Step 2: Ontology setup ───────────────────────────────
    print(f"\nParsing ontology from '{ONTOLOGY_FILE}' ...")
    prefixes = parse_ontology_prefixes(ONTOLOGY_FILE)
    base_iri = get_base_iri(prefixes)
    print(f"  Base IRI : {base_iri}")

    # Parse object vs data property names for SEw rescue (object props need joins, not literals)
    # Supports both OWL/XML (Declaration elements) and RDF/XML (owl:ObjectProperty elements).
    ont_obj_props:  Set[str] = set()
    ont_data_props: Set[str] = set()
    try:
        _ont_root = ET.parse(ONTOLOGY_FILE).getroot()
        _root_tag = _ont_root.tag.split("}")[-1] if "}" in _ont_root.tag else _ont_root.tag

        if _root_tag == "Ontology":
            # OWL/XML: properties declared as <Declaration><ObjectProperty .../></Declaration>
            for _el in _ont_root.iter():
                _tag = _el.tag.split("}")[-1] if "}" in _el.tag else _el.tag
                if _tag == "Declaration":
                    _ch = list(_el)
                    if _ch:
                        _ctag = _ch[0].tag.split("}")[-1] if "}" in _ch[0].tag else _ch[0].tag
                        _iri  = _ch[0].get("IRI", "") or _ch[0].get("abbreviatedIRI", "")
                        _name = _iri.split("#")[-1] if "#" in _iri else _iri.split("/")[-1]
                        if _ctag == "ObjectProperty" and _name:
                            ont_obj_props.add(_name)
                        elif _ctag == "DataProperty" and _name:
                            ont_data_props.add(_name)
        else:
            # RDF/XML: properties declared as top-level <owl:ObjectProperty rdf:about="..."/>
            # and <owl:DatatypeProperty rdf:about="..."/> elements
            OWL_NS = "http://www.w3.org/2002/07/owl#"
            RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
            _obj_tags  = {f"{{{OWL_NS}}}ObjectProperty"}
            _data_tags = {f"{{{OWL_NS}}}DatatypeProperty"}
            for _el in _ont_root:
                _iri = _el.get(f"{{{RDF_NS}}}about") or _el.get(f"{{{RDF_NS}}}ID", "")
                _name = _iri.split("#")[-1] if "#" in _iri else _iri.split("/")[-1]
                if not _name:
                    continue
                if _el.tag in _obj_tags:
                    ont_obj_props.add(_name)
                elif _el.tag in _data_tags:
                    ont_data_props.add(_name)

        print(f"  Object properties: {len(ont_obj_props)}, Data properties: {len(ont_data_props)}")
    except Exception as _e:
        print(f"  [WARN] Could not parse ontology properties: {_e}")
        ont_obj_props = set()
        ont_data_props = set()

    # ── Step 3: Merge entity entries, inject pattern tag ────
    entity_entries: Dict = {}
    for t, e in se_raw.items():
        entity_entries[t] = {**e, "pattern": e.get("pattern", "SE")}
    for t, e in sh_raw.items():
        entity_entries[t] = {**e, "pattern": e.get("pattern", "SE_SH")}
    for t, e in sew_raw.items():
        entity_entries[t] = {**e, "pattern": e.get("pattern", "SEw")}
    for t, e in srr_raw.items():
        entity_entries[t] = {**e, "pattern": e.get("pattern", "SRR")}

    # ── Step 3b: canonical_class_owner sanity check ──────────
    # Build SE/SE_SH class→table map. Clear class on any SEw/SRR entry
    # that was wrongly assigned a class already owned by SE/SE_SH
    # (phase 7 collision error, e.g. SEw_emails typed :Abstract).
    canonical_class_owner: Dict[str, str] = {}
    for t, e in se_raw.items():
        cls = e.get("subject", {}).get("class", "").lstrip(":")
        if cls:
            canonical_class_owner[cls] = t
    for t, e in sh_raw.items():
        cls = e.get("subject", {}).get("class", "").lstrip(":")
        if cls:
            canonical_class_owner[cls] = t
    for t in list(sew_raw.keys()) + list(srr_raw.keys()):
        if t not in entity_entries:
            continue
        e   = entity_entries[t]
        cls = e.get("subject", {}).get("class", "").lstrip(":")
        if cls and cls in canonical_class_owner and canonical_class_owner[cls] != t:
            owner = canonical_class_owner[cls]
            print(f"  [WARN] Class collision: :{cls} on '{t}' already owned by '{owner}' — clearing")
            entity_entries[t] = {**e, "subject": {**e["subject"], "class": ""}}

    # ── Step 3c: Repair {id} templates using actual PKs from tables_structure ──
    id_fixed = _fix_id_templates(entity_entries, tables_structure)
    if id_fixed:
        print(f"  [fix-id] Repaired {{id}} templates: {id_fixed} tables")

    # ── Step 4: Build IRI index for reference fixing ────────
    iri_index    = build_iri_index(entity_entries, sr_raw)
    defined_iris = {e["triple_map_iri"] for e in entity_entries.values()}

    # ── Step 5: Generate TTL ─────────────────────────────────
    # Phase 7 has already resolved all class collisions and written
    # the decisions back into the JSON files. Phase 8 just generates TTL.
    # Tables with _collision_unresolved=True get a warning comment but
    # are NEVER dropped — the user's data is always emitted.
    print("\nGenerating R2RML Turtle...")
    sections: List[str] = [build_prefix_block(base_iri, extra_prefixes=prefixes)]

    # SE
    sections.append(section_header("SE — Strong Entities"))
    for t in se_raw:
        sections.append(
            build_entity_block(t, entity_entries[t], defined_iris, iri_index,
                               tables_structure=tables_structure)
        )

    # SE_SH
    if sh_raw:
        sections.append(section_header("SE_SH — Subclass Entities"))
        for t in sh_raw:
            sections.append(
                build_entity_block(t, entity_entries[t], defined_iris, iri_index,
                                   tables_structure=tables_structure)
            )

    # SEw
    if sew_raw:
        sections.append(section_header("SEw — Weak Entities"))
        for t in sew_raw:
            entry = entity_entries[t]
            # Skip attribute-like SEw tables — they are fully represented
            # by the rescued property TriplesMap below. Emitting a standalone
            # entity block with composite-PK IRI and wrong class produces
            # spurious triples that confuse SPARQL engines.
            if entry.get("sew_type") == "property_of_owner":
                print(f"  [SEw] Skipping entity block for attribute-like table {t!r} "
                      f"(sew_type=property_of_owner — rescue map covers it)")
                continue
            # Also skip entries rescued by phase7 (pattern=SEw_rescued)
            if entry.get("pattern") == "SEw_rescued" or entry.get("_rescued_as_property"):
                continue
            sections.append(
                build_entity_block(t, entry, defined_iris, iri_index)
            )

        # ── SEw property rescue ──────────────────────────────────────────
        # Two paths:
        #
        # PATH A — sew_type=property_of_owner (set by phase 3):
        #   Phase 3 already identified this as an attribute-like SEw and stored
        #   all needed fields directly: owner_template, owner_fk_col,
        #   owner_data_col, owner_predicate, owner_table.
        #   Generate ONE TriplesMap: owner subject + property predicate + data literal.
        #
        # PATH B — composite PK detected here (fallback for entries phase 3
        #   processed before this fix or loaded from old cache):
        #   Detect from template placeholders + join POMs and derive predicate
        #   from stored owner_predicate or capitalise table name.
        import re as _re
        _q3 = "'''"

        for t, entry in sew_raw.items():

            # ── PATH A: clean phase3 attribute-like entry ─────────────────
            if entry.get("sew_type") == "property_of_owner":
                owner_tmpl   = entry.get("owner_template", "")
                owner_fk_col = entry.get("owner_fk_col", "")
                data_col     = entry.get("owner_data_col", "")
                predicate    = entry.get("owner_predicate", "")
                owner_tbl    = entry.get("owner_table", "")

                if not owner_tmpl:
                    # Try to recover owner_template from entity_entries
                    owner_iri = entry.get("owner_iri", "")
                    for oe in entity_entries.values():
                        if oe.get("triple_map_iri") == owner_iri:
                            owner_tmpl = oe.get("subject", {}).get("template", "")
                            break

                if not (owner_tmpl and owner_fk_col and data_col and predicate and owner_tbl):
                    print(f"  [SEw-rescue PATH-A] {t!r}: missing fields, skipping")
                    continue

                owner_pk_cols = _re.findall(r'\{([^}]+)\}', owner_tmpl)
                owner_pk      = owner_pk_cols[0] if owner_pk_cols else "id"
                rescue_iri    = f"urn:r2rml:SEw_{t}_prop_{data_col}"
                # Use rr:tableName directly on the SEw table.
                # Subject template uses the FK column {owner_fk_col} which holds
                # the owner PK value — resolves to the same IRI as the owner entity.
                # This avoids SQL JOINs, aliases, and reserved-word issues entirely.
                owner_tmpl_fk = owner_tmpl.replace(
                    f"{{{owner_pk}}}", f"{{{owner_fk_col}}}"
                )
                # Use plain SELECT col1, col2 WHERE col IS NOT NULL — no aliases.
                # Aliases cause reserved-word errors (e.g. AS value in PostgreSQL).
                # Only use hyphen-quoting when column names or table names contain hyphens.
                has_hyphen = "-" in owner_fk_col or "-" in data_col or "-" in t
                if has_hyphen:
                    hyphen_cols = _collect_hyphen_cols([owner_fk_col, data_col])
                    sql_q       = _build_star_sql(t, hyphen_cols,
                                                  f'"{data_col}" IS NOT NULL')
                else:
                    sql_q = (f'SELECT {owner_fk_col}, {data_col} '
                             f'FROM "{t}" '
                             f'WHERE {data_col} IS NOT NULL')
                table_line = f"    rr:logicalTable [ rr:sqlQuery {_q3}{sql_q}{_q3} ] ;"

                # ── KEY FIX: Check if predicate is OBJECT property or DATA property
                # If object property → emit rr:parentTriplesMap (join) instead of rr:column
                # Primary signal: Phase 3's owner_pred_is_object field
                # Fallback: check ontology declarations and FK status
                pred_local = predicate.lstrip(":")
                is_obj_prop = entry.get("owner_pred_is_object", False)
                target_iri  = ""
                target_pk   = "id"

                if not is_obj_prop:
                    # Check ontology: is this predicate declared as ObjectProperty?
                    if ont_obj_props and pred_local in ont_obj_props:
                        is_obj_prop = True
                    elif ont_data_props and pred_local not in ont_data_props:
                        # Not a declared data property — check if the column is an FK
                        ts_entry = tables_structure.get(t, {})
                        for col_info in ts_entry.get("columns", []):
                            if col_info.get("name") == data_col and col_info.get("is_foreign_key"):
                                is_obj_prop = True
                                break

                # If object property, resolve the target entity IRI
                if is_obj_prop:
                    # Helper: skip SEw entries that won't produce TriplesMap blocks
                    def _is_emittable(oe):
                        if oe.get("sew_type") == "property_of_owner":
                            return False
                        if oe.get("_rescued_as_property"):
                            return False
                        pat = oe.get("pattern", "")
                        if pat == "SEw_rescued":
                            return False
                        return True

                    # Strategy 0: read FK target from Phase 3's own attributes list
                    # This is the most reliable source — Phase 3 already resolved FKs
                    for attr in entry.get("attributes", []):
                        if attr.get("name") == data_col:
                            fk_ref = attr.get("fk_references", {})
                            ref_table_s0 = fk_ref.get("table", "")
                            if ref_table_s0:
                                target_pk = fk_ref.get("column", "id")
                                for oe_key, oe in entity_entries.items():
                                    if not _is_emittable(oe):
                                        continue
                                    if oe_key == ref_table_s0 or oe_key.lower() == ref_table_s0.lower():
                                        target_iri = oe.get("triple_map_iri", "")
                                        break
                                if not target_iri:
                                    # Also try matching logical_table
                                    for oe in entity_entries.values():
                                        if not _is_emittable(oe):
                                            continue
                                        if oe.get("logical_table", "").lower() == ref_table_s0.lower():
                                            target_iri = oe.get("triple_map_iri", "")
                                            break
                            break

                    ts_entry = tables_structure.get(t, {})
                    # Strategy 1: FK reference in tables_structure
                    for col_info in ts_entry.get("columns", []):
                        if col_info.get("name") == data_col and col_info.get("is_foreign_key"):
                            ref = col_info.get("foreign_key_reference", {})
                            ref_table = ref.get("table", "")
                            target_pk = ref.get("column", "id")
                            for oe in entity_entries.values():
                                if not _is_emittable(oe):
                                    continue
                                oe_tbl = oe.get("logical_table", "")
                                if oe_tbl == ref_table:
                                    target_iri = oe.get("triple_map_iri", "")
                                    break
                            if not target_iri:
                                for oe_key, oe in entity_entries.items():
                                    if not _is_emittable(oe):
                                        continue
                                    if oe_key == ref_table or oe_key.lower() == ref_table.lower():
                                        target_iri = oe.get("triple_map_iri", "")
                                        break
                            break

                    # Strategy 2: column name matches an entity table name
                    if not target_iri:
                        dc_lower = data_col.lower()
                        for oe_key, oe in entity_entries.items():
                            if not _is_emittable(oe):
                                continue
                            if oe_key.lower() == dc_lower:
                                target_iri = oe.get("triple_map_iri", "")
                                target_pk = "id"
                                break

                    # Strategy 3: column name is a substring of an entity key
                    if not target_iri:
                        for oe_key, oe in entity_entries.items():
                            if not _is_emittable(oe):
                                continue
                            oe_lower = oe_key.lower()
                            if dc_lower in oe_lower or oe_lower in dc_lower:
                                target_iri = oe.get("triple_map_iri", "")
                                target_pk = "id"
                                break

                    # Strategy 4: ontology property range → find entity for that class
                    # Supports subclass matching: if range is :ConferenceMember and
                    # no entity has that exact class, finds :Person (superclass)
                    if not target_iri and ont_obj_props:
                        try:
                            _ont_root2 = ET.parse(ONTOLOGY_FILE).getroot()

                            # Build subclass index for range matching
                            _subclass_pairs = []
                            for _el in _ont_root2.iter():
                                _tag = _el.tag.split("}")[-1] if "}" in _el.tag else _el.tag
                                if _tag == "SubClassOf":
                                    _ch = list(_el)
                                    if len(_ch) == 2:
                                        _s0 = _ch[0].tag.split("}")[-1] if "}" in _ch[0].tag else _ch[0].tag
                                        _s1 = _ch[1].tag.split("}")[-1] if "}" in _ch[1].tag else _ch[1].tag
                                        if _s0 == "Class" and _s1 == "Class":
                                            sub = (_ch[0].get("IRI", "") or _ch[0].get("abbreviatedIRI", ""))
                                            sup = (_ch[1].get("IRI", "") or _ch[1].get("abbreviatedIRI", ""))
                                            sub = sub.split("#")[-1] if "#" in sub else sub.split("/")[-1]
                                            sup = sup.split("#")[-1] if "#" in sup else sup.split("/")[-1]
                                            if sub and sup:
                                                _subclass_pairs.append((sub, sup))

                            for _el in _ont_root2.iter():
                                _tag = _el.tag.split("}")[-1] if "}" in _el.tag else _el.tag
                                if _tag == "ObjectPropertyRange":
                                    _ch = list(_el)
                                    if len(_ch) >= 2:
                                        _p = (_ch[0].get("IRI", "") or _ch[0].get("abbreviatedIRI", ""))
                                        _p = _p.split("#")[-1] if "#" in _p else _p.split("/")[-1]
                                        if _p == pred_local:
                                            _r = (_ch[1].get("IRI", "") or _ch[1].get("abbreviatedIRI", ""))
                                            _r = _r.split("#")[-1] if "#" in _r else _r.split("/")[-1]
                                            if _r:
                                                # Build set of classes to match: the range + all ancestors
                                                range_and_ancestors = {_r}
                                                # Also: any class whose subclass includes _r
                                                for sub, sup in _subclass_pairs:
                                                    if sub == _r:
                                                        range_and_ancestors.add(sup)
                                                # Transitive: ancestors of ancestors
                                                changed = True
                                                while changed:
                                                    changed = False
                                                    for sub, sup in _subclass_pairs:
                                                        if sub in range_and_ancestors and sup not in range_and_ancestors:
                                                            range_and_ancestors.add(sup)
                                                            changed = True

                                                # Try exact match first
                                                for oe_key, oe in entity_entries.items():
                                                    if not _is_emittable(oe):
                                                        continue
                                                    oe_cls = oe.get("subject", {}).get("class", "").lstrip(":")
                                                    if oe_cls == _r:
                                                        target_iri = oe.get("triple_map_iri", "")
                                                        target_pk = "id"
                                                        break

                                                # If no exact match, try ancestors
                                                if not target_iri:
                                                    for oe_key, oe in entity_entries.items():
                                                        if not _is_emittable(oe):
                                                            continue
                                                        oe_cls = oe.get("subject", {}).get("class", "").lstrip(":")
                                                        if oe_cls in range_and_ancestors:
                                                            target_iri = oe.get("triple_map_iri", "")
                                                            target_pk = "id"
                                                            break
                                            break
                        except Exception:
                            pass

                if is_obj_prop and target_iri:
                    # Resolve target_pk from the actual target entity rather than
                    # using the "id" default — which is wrong whenever the target
                    # table's PK column is named something other than "id" (e.g. "uri").
                    #
                    # Priority:
                    #   1. Extract from the target entity's subject template placeholders
                    #      (most reliable — phase 3 built it from actual PKs).
                    #   2. Look up primary_keys in tables_structure for the target table.
                    #   3. Keep "id" as last resort only.
                    resolved_target_pk = None

                    # Find the target entity entry to read its template
                    for oe in entity_entries.values():
                        if oe.get("triple_map_iri") == target_iri:
                            tgt_tmpl = oe.get("subject", {}).get("template", "")
                            tgt_cols = _re.findall(r'\{([^}]+)\}', tgt_tmpl)
                            if tgt_cols:
                                resolved_target_pk = tgt_cols[-1]  # last placeholder = PK
                            # Also check tables_structure for the target logical table
                            if not resolved_target_pk:
                                tgt_tbl = oe.get("logical_table", "")
                                tgt_pks = tables_structure.get(tgt_tbl, {}).get("primary_keys", [])
                                if tgt_pks:
                                    resolved_target_pk = tgt_pks[0]
                            break

                    if not resolved_target_pk and target_iri:
                        # Fallback: derive table name from IRI suffix and check tables_structure
                        tgt_tail = target_iri.split(":")[-1]
                        for pfx in ("SE_SH_", "SE_", "SEw_", "SR_", "SRR_"):
                            if tgt_tail.startswith(pfx):
                                tgt_tail = tgt_tail[len(pfx):]
                                break
                        tgt_pks = tables_structure.get(tgt_tail, {}).get("primary_keys", [])
                        if tgt_pks:
                            resolved_target_pk = tgt_pks[0]

                    if resolved_target_pk and resolved_target_pk != target_pk:
                        print(f"  [SEw-rescue] {t}: target_pk "
                              f"'{target_pk}' → '{resolved_target_pk}' "
                              f"(from target entity template/tables_structure)")
                        target_pk = resolved_target_pk
                    # Emit as OBJECT PROPERTY with join
                    rescue_block = "\n".join([
                        f"# ── SEw_{t} [property of {owner_tbl}: {data_col}] ────────────────────",
                        f"<{rescue_iri}>",
                        f"    a rr:TriplesMap ;",
                        f"",
                        table_line,
                        f"",
                        f"    rr:subjectMap [",
                        f'        rr:template "{owner_tmpl_fk}" ;',
                        f"    ] ;",
                        f"",
                        f"    rr:predicateObjectMap [",
                        f"        rr:predicate {predicate} ;",
                        f"        rr:objectMap  [",
                        f"            rr:parentTriplesMap <{target_iri}> ;",
                        f"            rr:joinCondition [",
                        f'                rr:child  "{_safe_alias(data_col)}" ;',
                        f'                rr:parent "{_safe_alias(target_pk)}" ;',
                        f"            ] ;",
                        f"        ] ;",
                        f"    ]  .",
                        f"",
                    ])
                    sections.append(rescue_block)
                    print(f"  [SEw-rescue PATH-A] {t}.{data_col} → {predicate} on {owner_tmpl} "
                          f"(OBJECT PROP → join to <{target_iri}>)")
                elif is_obj_prop and not target_iri:
                    # Object property but can't find target — emit as literal with warning
                    rescue_block = "\n".join([
                        f"# ── SEw_{t} [property of {owner_tbl}: {data_col}] ────────────────────",
                        f"# WARNING: {predicate} is an object property but target entity not found",
                        f"<{rescue_iri}>",
                        f"    a rr:TriplesMap ;",
                        f"",
                        table_line,
                        f"",
                        f"    rr:subjectMap [",
                        f'        rr:template "{owner_tmpl_fk}" ;',
                        f"    ] ;",
                        f"",
                        f"    rr:predicateObjectMap [",
                        f"        rr:predicate {predicate} ;",
                        f"        rr:objectMap  [",
                        f'            rr:column   "{_safe_alias(data_col)}" ;',
                        f"        ] ;",
                        f"    ]  .",
                        f"",
                    ])
                    sections.append(rescue_block)
                    print(f"  [SEw-rescue PATH-A] {t}.{data_col} → {predicate} on {owner_tmpl} "
                          f"(WARN: obj prop but no target found — emitted as literal)")
                else:
                    # Data property — emit as literal (original behavior)
                    rescue_block = "\n".join([
                        f"# ── SEw_{t} [property of {owner_tbl}: {data_col}] ────────────────────",
                        f"<{rescue_iri}>",
                        f"    a rr:TriplesMap ;",
                        f"",
                        table_line,
                        f"",
                        f"    rr:subjectMap [",
                        f'        rr:template "{owner_tmpl_fk}" ;',
                        f"    ] ;",
                        f"",
                        f"    rr:predicateObjectMap [",
                        f"        rr:predicate {predicate} ;",
                        f"        rr:objectMap  [",
                        f'            rr:column   "{_safe_alias(data_col)}" ;',
                        f"        ] ;",
                        f"    ]  .",
                        f"",
                    ])
                    sections.append(rescue_block)
                    print(f"  [SEw-rescue PATH-A] {t}.{data_col} → {predicate} on {owner_tmpl}")
                continue

            # ── PATH B: fallback composite-PK detection ───────────────────
            tmpl      = entry.get("subject", {}).get("template", "")
            tmpl_cols = _re.findall(r'\{([^}]+)\}', tmpl)
            if len(tmpl_cols) < 2:
                continue

            poms      = entry.get("predicate_object_maps", [])
            join_poms = [p for p in poms if p.get("object", {}).get("type") == "join"]
            if not join_poms:
                continue

            fk_cols  = {p["object"]["join_condition"]["child"]
                        for p in join_poms
                        if p.get("object", {}).get("join_condition", {}).get("child")}
            data_cols = [c for c in tmpl_cols if c not in fk_cols]
            if not data_cols:
                continue

            owner_iri = join_poms[0]["object"].get("parent_triples_map", "")
            if not owner_iri:
                continue

            owner_tmpl = None
            for oe in entity_entries.values():
                if oe.get("triple_map_iri") == owner_iri:
                    owner_tmpl = oe.get("subject", {}).get("template", "")
                    break
            if not owner_tmpl:
                continue

            owner_pk_cols = _re.findall(r'\{([^}]+)\}', owner_tmpl)
            owner_pk  = owner_pk_cols[0] if owner_pk_cols else "id"
            fk_col    = next(iter(fk_cols)) if fk_cols else None
            if not fk_col:
                continue

            # Prefer stored predicate from phase3/phase7, else capitalise table name
            phase3_pred = entry.get("owner_predicate") or entry.get("_rescue_property")
            if phase3_pred:
                predicate = phase3_pred if phase3_pred.startswith(":") else f":{phase3_pred}"
            else:
                raw_name  = t.replace("_", "-").replace(" ", "-")
                parts     = raw_name.split("-")
                # Capitalise only first segment: "e-mail" → "E-mail" not "E-Mail"
                predicate = ":" + "-".join(
                    [parts[0].capitalize()] + [p.lower() for p in parts[1:]]
                    if parts else [])

            owner_table = owner_iri.replace("urn:r2rml:SE_", "").replace("urn:r2rml:SE_SH_", "")

            for col in data_cols:
                rescue_iri = f"urn:r2rml:SEw_{t}_prop_{col}"
                # Use FK column in subject template to match owner IRI directly.
                # No JOIN or aliases needed — avoids reserved word issues.
                owner_tmpl_fk = owner_tmpl.replace(
                    f"{{{owner_pk}}}", f"{{{fk_col}}}"
                ) if owner_tmpl else f"{base_iri}{owner_table}/{{{fk_col}}}"
                needs_sql_b   = _needs_sql_query(t, [fk_col, col])
                if needs_sql_b:
                    hcols_b       = _collect_hyphen_cols([fk_col, col])
                    sql_b         = _build_star_sql(t, hcols_b, f'"{col}" IS NOT NULL')
                    table_line_b  = f"    rr:logicalTable [ rr:sqlQuery {_q3}{sql_b}{_q3} ] ;"
                else:
                    table_line_b  = f'    rr:logicalTable [ rr:tableName "{t}" ] ;'
                rescue_block = "\n".join([
                    f"# ── SEw_{t} [rescued property: {col}] {'─'*20}",
                    f"<{rescue_iri}>",
                    f"    a rr:TriplesMap ;",
                    f"",
                    table_line_b,
                    f"",
                    f"    rr:subjectMap [",
                    f'        rr:template "{owner_tmpl_fk}" ;',
                    f"    ] ;",
                    f"",
                    f"    rr:predicateObjectMap [",
                    f"        rr:predicate {predicate} ;",
                    f"        rr:objectMap  [",
                    f'            rr:column   "{_safe_alias(col)}" ;',
                    f"        ] ;",
                    f"    ]  .",
                    f"",
                ])
                sections.append(rescue_block)
                print(f"  [SEw-rescue PATH-B] {t}.{col} → {predicate} on {owner_tmpl}")

    # SRR
    if srr_raw:
        sections.append(section_header("SRR — Reified Relationships"))
        for t in srr_raw:
            sections.append(
                build_entity_block(t, entity_entries[t], defined_iris, iri_index)
            )

    # SR
    if sr_raw:
        sections.append(
            build_sr_section(sr_raw, entity_entries, defined_iris, iri_index)
        )

    # AXIOM TYPING (EquivalentClasses materialization)
    if sr_raw:
        axiom_ttl = _build_axiom_typing_blocks(sr_raw)
        if axiom_ttl.strip():
            sections.append(axiom_ttl)

    # HIDDEN
    if hidden:
        sections.append(
            build_hidden_section(hidden, entity_entries, defined_iris, iri_index,
                                 tables_structure=tables_structure)
        )

    # ── Step 6: Validate & fix broken parentTriplesMap references ──
    ttl_content = "\n".join(sections)

    # Extract all defined TriplesMap IRIs
    import re as _re_val
    defined_iris_final = set(_re_val.findall(r'^<(urn:r2rml:[^>]+)>', ttl_content, _re_val.MULTILINE))
    # Extract all referenced parentTriplesMap IRIs
    referenced_iris = set(_re_val.findall(r'rr:parentTriplesMap\s+<([^>]+)>', ttl_content))

    broken = referenced_iris - defined_iris_final
    if broken:
        print(f"\n  [WARN] {len(broken)} broken parentTriplesMap reference(s) found:")
        for br in sorted(broken):
            print(f"    ✗ {br}")
            # Try to find a valid replacement by matching the class
            # Extract the table name hint from the broken IRI
            tail = br.split(":")[-1]  # e.g. "SEw_program_committee_members"
            # Remove common prefixes
            for pfx in ("SEw_", "SE_SH_", "SE_", "SR_", "SRR_"):
                if tail.startswith(pfx):
                    tail = tail[len(pfx):]
                    break
            tail_lower = tail.lower()
            # Search defined IRIs for a match
            replacement = None
            for d_iri in sorted(defined_iris_final):
                d_tail = d_iri.split(":")[-1].lower()
                if tail_lower in d_tail or d_tail.endswith(tail_lower):
                    replacement = d_iri
                    break
            if not replacement:
                # Try matching by entity class
                for oe_key, oe in entity_entries.items():
                    oe_iri = oe.get("triple_map_iri", "")
                    if oe_iri in defined_iris_final:
                        if tail_lower in oe_key.lower() or oe_key.lower() in tail_lower:
                            replacement = oe_iri
                            break
            if replacement:
                print(f"      → auto-fixed to {replacement}")
                ttl_content = ttl_content.replace(f"<{br}>", f"<{replacement}>")
            else:
                print(f"      → NO replacement found — commenting out referencing block")
                # Comment out lines that reference this broken IRI
                ttl_content = ttl_content.replace(
                    f"rr:parentTriplesMap <{br}>",
                    f"# BROKEN REF: rr:parentTriplesMap <{br}>"
                )

    # ── Step 7: Write TTL ────────────────────────────────────
    with open(R2RML_FILE, "w", encoding="utf-8") as f:
        f.write(ttl_content)

    total_entity = len(entity_entries)

    print(f"\n{'=' * 56}")
    print("  PHASE 8 COMPLETE")
    print(f"{'=' * 56}")
    print(f"  Entity tables written  : {total_entity}")
    print(f"  SR bridge tables       : {len(sr_raw)}")
    print(f"\n  R2RML TTL → {R2RML_FILE}\n")


if __name__ == "__main__":
    try:
        run_r2rml_generation()
    except Exception as e:
        print(f"\nFatal error: {e}")
        import traceback
        traceback.print_exc()
