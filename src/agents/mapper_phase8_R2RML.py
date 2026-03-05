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

Reads  : src2/outputs/mappings/SE_mappings.json          (required)
         src2/outputs/mappings/SH_mappings.json           (optional)
         src2/outputs/mappings/SEw_mappings.json          (optional)
         src2/outputs/mappings/SRR_mappings.json          (optional)
         src2/outputs/mappings/SR_mappings.json           (optional)
         src2/outputs/mappings/HIDDEN_mappings.json       (optional)
         src2/inputs/ontology/ontology.owl
Writes : src2/outputs/mappings/mappings_r2rml.ttl
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
OUTPUT_DIR    = "src2/outputs"
MAPPINGS_DIR  = os.path.join(OUTPUT_DIR, "mappings")
ONTOLOGY_FILE = "src2/inputs/ontology/ontology.owl"
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

def _needs_sql_query(name: str, template_cols: list = None) -> bool:
    """
    True when rr:sqlQuery is required instead of rr:tableName:
    1. Table name is mixed-case or contains a hyphen.
    2. Any template column (PK) is mixed-case or contains a hyphen.
    """
    if "-" in name or name != name.lower():
        return True
    if template_cols:
        for col in template_cols:
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
    """Quote only column identifiers in a WHERE clause, not SQL keywords."""
    def _quoter(m):
        ident = m.group(1)
        if ident.upper() in _SQL_KEYWORDS:
            return m.group(0)
        return f'"{ident}"' + m.group(0)[len(ident):]
    return re.sub(r'(?<!")\b([A-Za-z_][A-Za-z0-9_-]*)\b(?!\s*")', _quoter, sql_filter)


def _build_safe_sql(table_name: str,
                    select_cols: list,
                    where_clause: str = None) -> str:
    """
    Build a SELECT with every identifier double-quoted and every column
    aliased to lowercase. select_cols is a list of (original_name, alias) pairs.
    """
    parts = [f'"{orig}" AS {alias}' for orig, alias in select_cols]
    sql   = f'SELECT {", ".join(parts)} FROM "{table_name}"'
    if where_clause:
        sql += f" WHERE {where_clause}"
    return sql


# ============================================================
# TTL building blocks
# ============================================================

def _pom_literal(pred: str, col: str, datatype: str) -> List[str]:
    if datatype in ("xsd:anyURI", "http://www.w3.org/2001/XMLSchema#anyURI"):
        return [
            f"    rr:predicateObjectMap [",
            f"        rr:predicate {pred} ;",
            f"        rr:objectMap  [",
            f"            rr:column    \"{_safe_alias(col)}\" ;",
            f"            rr:termType  rr:IRI ;",
            f"        ] ;",
            f"    ] ;",
            "",
        ]
    return [
        f"    rr:predicateObjectMap [",
        f"        rr:predicate {pred} ;",
        f"        rr:objectMap  [",
        f"            rr:column   \"{_safe_alias(col)}\" ;",
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
                  iri_index: Dict[str, str]) -> List[str]:
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
            jc = obj.get("join_condition", {})
            lines += _pom_join(pred, fixed,
                               jc.get("child", "id"), jc.get("parent", "id"))
    return lines


def build_entity_block(table_name: str, entry: Dict,
                       defined_iris: Set[str], iri_index: Dict[str, str],
                       sql_filter: Optional[str] = None) -> str:
    """
    Generic TTL block builder for SE, SE_SH, SEw, SRR, and HIDDEN patterns.

    Logical table source priority:
      1. sql_filter argument          → HIDDEN pattern (WHERE clause)
      2. entry["logical_table_sql"]   → explicit SQL override from phase 7
      3. SE_SH + parent_table + no POMs → JOIN to parent for inheritance
      4. mixed-case / hyphenated name → rr:sqlQuery with aliased SELECT
      5. default                      → rr:tableName
    """
    iri  = entry["triple_map_iri"]
    cls  = entry["subject"].get("class")
    pat  = entry.get("pattern", "")

    _q3 = "'''"

    # Warn about unresolved collisions but still emit the block
    collision_note = entry.get("_collision_note", "")

    tmpl = _lower_template(entry["subject"]["template"])

    poms         = entry.get("predicate_object_maps", [])
    literal_cols = [(p["object"]["column"], _safe_alias(p["object"]["column"]))
                    for p in poms
                    if p.get("object", {}).get("type") == "literal"]

    if sql_filter:
        safe_filter   = _quote_filter(sql_filter)
        raw_tmpl_cols = _extract_template_cols(entry["subject"]["template"])

        fc_match = re.match(r'(?:"([^"]+)"|([A-Za-z_][A-Za-z0-9_-]*))', sql_filter.strip())
        filter_col_orig = (fc_match.group(1) or fc_match.group(2)) if fc_match else None

        seen = set()
        select_cols = []
        for orig in raw_tmpl_cols:
            alias = _safe_alias(orig)
            if alias not in seen:
                seen.add(alias)
                select_cols.append((orig, alias))
        if filter_col_orig:
            alias = _safe_alias(filter_col_orig)
            if alias not in seen:
                seen.add(alias)
                select_cols.append((filter_col_orig, alias))

        sql        = _build_safe_sql(table_name, select_cols, safe_filter)
        table_line = f"    rr:logicalTable [ rr:sqlQuery {_q3}{sql}{_q3} ] ;"

    elif entry.get("logical_table_sql"):
        sql        = entry["logical_table_sql"]
        table_line = f"    rr:logicalTable [ rr:sqlQuery {_q3}{sql}{_q3} ] ;"

    elif (entry.get("pattern") == "SE_SH"
          and entry.get("parent_table")
          and not entry.get("predicate_object_maps")):
        parent     = entry["parent_table"]
        sql        = f'SELECT p.* FROM "{table_name}" t JOIN "{parent}" p ON t.id = p.id'
        table_line = f"    rr:logicalTable [ rr:sqlQuery {_q3}{sql}{_q3} ] ;"

    elif _needs_sql_query(table_name, _extract_template_cols(entry["subject"]["template"])):
        raw_tmpl_cols = _extract_template_cols(entry["subject"]["template"])
        seen_aliases  = set()
        select_cols   = []
        for orig in raw_tmpl_cols:
            alias = _safe_alias(orig)
            if alias not in seen_aliases:
                seen_aliases.add(alias)
                select_cols.append((orig, alias))
        for orig, alias in literal_cols:
            if alias not in seen_aliases:
                seen_aliases.add(alias)
                select_cols.append((orig, alias))
        sql        = _build_safe_sql(table_name, select_cols)
        table_line = f"    rr:logicalTable [ rr:sqlQuery {_q3}{sql}{_q3} ] ;"

    else:
        table_line = f'    rr:logicalTable [ rr:tableName "{table_name}" ] ;'

    sep   = "─" * max(0, 48 - len(pat) - len(table_name))
    lines = []
    if collision_note:
        lines.append(f"# ⚠ COLLISION WARNING: {collision_note}")
    lines += [
        f"# ── {pat}_{table_name} {sep}",
        f"<{iri}>",
        f"    a rr:TriplesMap ;",
        f"",
        table_line,
        f"",
        f"    rr:subjectMap [",
        f'        rr:template "{tmpl}" ;',
    ]

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
    Replace entity PK placeholder in template with the bridge FK column.
    The bridge table only has FK columns, not the entity's own PK.
    """
    fk_col    = s_join.get("child", "")
    entity_pk = s_join.get("parent", "id")
    if fk_col and entity_pk:
        tmpl = tmpl.replace(f"{{{entity_pk}}}", f"{{{fk_col}}}")
    return tmpl


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
            sr_iri   = f"{entry['triple_map_iri']}_{subj_tag}_{obj_tag}"

            _q3b = "'''"
            s_child         = s_join.get("child", "id")
            o_child_col     = o_join.get("child", "id")
            bridge_cols_needed = list(dict.fromkeys([s_child, o_child_col]))
            bridge_select   = ", ".join(
                f'"{c}" AS {_safe_alias(c)}' for c in bridge_cols_needed
            )
            _br_sql  = f'SELECT {bridge_select} FROM "{bridge_table}"'
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
                               _safe_alias(o_join.get("child", "id")),
                               _safe_alias(o_join.get("parent", "id")))
            bridge_name_blocks.append(_close(lines))

        if bridge_name_blocks:
            blocks.extend(bridge_name_blocks)
        else:
            blocks.append(
                f"# SR_{bridge_table}: all directions deduplicated or skipped\n"
            )

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
        return f"{col_name} = '{fv}'"

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
        for td in entry.get("type_dispatch", []):
            col      = td.get("discriminator_column", "type")
            col_type = (td.get("discriminator_type", "")
                        or _lookup_col_type(table_name, col, ts))

            for dispatch in td.get("dispatch", []):
                subj = dispatch.get("subject", {})
                if not subj.get("class") or not subj.get("template"):
                    continue

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

    # ── Step 4: Build IRI index for reference fixing ────────
    iri_index    = build_iri_index(entity_entries, sr_raw)
    defined_iris = {e["triple_map_iri"] for e in entity_entries.values()}

    # ── Step 5: Generate TTL ─────────────────────────────────
    # Phase 7 has already resolved all class collisions and written
    # the decisions back into the JSON files. Phase 8 just generates TTL.
    # Tables with _collision_unresolved=True get a warning comment but
    # are NEVER dropped — the user's data is always emitted.
    print("\nGenerating R2RML Turtle...")
    sections: List[str] = [build_prefix_block(base_iri)]

    # SE
    sections.append(section_header("SE — Strong Entities"))
    for t in se_raw:
        sections.append(
            build_entity_block(t, entity_entries[t], defined_iris, iri_index)
        )

    # SE_SH
    if sh_raw:
        sections.append(section_header("SE_SH — Subclass Entities"))
        for t in sh_raw:
            sections.append(
                build_entity_block(t, entity_entries[t], defined_iris, iri_index)
            )

    # SEw
    if sew_raw:
        sections.append(section_header("SEw — Weak Entities"))
        for t in sew_raw:
            sections.append(
                build_entity_block(t, entity_entries[t], defined_iris, iri_index)
            )

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

    # HIDDEN
    if hidden:
        sections.append(
            build_hidden_section(hidden, entity_entries, defined_iris, iri_index,
                                 tables_structure=tables_structure)
        )

    # ── Step 6: Write TTL ────────────────────────────────────
    ttl_content = "\n".join(sections)
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