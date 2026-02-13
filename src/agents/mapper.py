# src/agents/mapper.py
import os
import re
import json
from typing import Dict, Any, Optional, List

from groq import Groq
from dotenv import load_dotenv

from agents.mapper_planner import MapperPlanner
from ontology import explorer as onto_explorer_module
from ontology.explorer import ontology_explorer

from schema.pattern_retriever import get_table_pattern
from schema.units_retriever import get_semantic_unit
from schema.pairs_retriever import find_links_for_table
from schema.data_retriever import print_entity_continuity

load_dotenv()

MODEL_NAME = "qwen/qwen3-32b"
_GROQ_KEY = os.environ.get("GROQ_API_KEY")
if not _GROQ_KEY:
    raise ValueError("GROQ_API_KEY is not set (put it in .env)")
client = Groq(api_key=_GROQ_KEY)

# ============================
# Paths (local)
# ============================
DB_FOLDER = os.environ.get("MPBOOT_DB_JSON_OUT", "src/utils_io/DB_json_out")
SEM_MEM   = os.path.join(DB_FOLDER, "Semantic_Memory")

MAPPINGS_OUT_DIR = os.path.join(DB_FOLDER, "Mappings")
REPORTS_DIR      = os.path.join(MAPPINGS_OUT_DIR, "mapping_reports")

UNDERSTANDING_MEMORY_PATH = os.path.join(SEM_MEM, "Understanding_Memory.json")
ENRICHMENT_XML_PATH       = os.path.join(SEM_MEM, "Enrichment_Memory.xml")
UNIT_LINKS_PATH           = os.path.join(SEM_MEM, "Unit_Links_Pairs.json")

PATTERNS_DISCOVERY_PATH   = os.path.join(SEM_MEM, "Patterns_discovery.json")
ENTITY_CONTINUITY_PATH    = os.path.join(SEM_MEM, "SE_Entity_Continuity_WithData.json")

MAPPINGS_TTL_PATH         = os.path.join(MAPPINGS_OUT_DIR, "Mappings_r2rml.ttl")

# ============================
# Helpers
# ============================
def normalize_prefix_iri(base_prefix: str) -> str:
    if base_prefix.endswith("#") or base_prefix.endswith("/"):
        return base_prefix
    return base_prefix + "#"

def resolve_base_prefix(base_prefix: Optional[str] = None) -> str:
    if base_prefix and isinstance(base_prefix, str) and base_prefix.strip():
        return normalize_prefix_iri(base_prefix.strip())

    discovered = getattr(onto_explorer_module, "BASE_IRI", "")
    if not discovered:
        raise ValueError("Cannot discover BASE_IRI from ontology.explorer and no base_prefix was provided.")
    return normalize_prefix_iri(str(discovered).strip())

def _safe_name(name: str) -> str:
    name = (name or "").strip()
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name) or "table"

def load_json(path: str, default):
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default

def save_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)

def read_text(path: str) -> str:
    if not os.path.isfile(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

def capture_printed(func, *args, **kwargs) -> str:
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        func(*args, **kwargs)
    return buf.getvalue().strip()

def escape_ttl_string(s: str) -> str:
    if s is None:
        return ""
    return s.replace("\\", "\\\\").replace('"', '\\"')

def local_name(x: str) -> str:
    x = (x or "").strip()
    if x.startswith(":"):
        x = x[1:]
    if "#" in x:
        x = x.split("#")[-1]
    if "/" in x:
        x = x.rsplit("/", 1)[-1]
    return x

# ============================
# Enrichment extraction
# ============================
def extract_enrichment_block(xml_path: str, table_name: str) -> str:
    if not os.path.isfile(xml_path):
        raise ValueError(f"Missing enrichment xml at {xml_path}")

    text = read_text(xml_path)
    pat = re.compile(
        r'<ENRICHMENT\s+table="{}"\s*>.*?</ENRICHMENT>'.format(re.escape(table_name)),
        re.DOTALL | re.IGNORECASE
    )
    blocks = pat.findall(text)
    if not blocks:
        raise ValueError(f'No <ENRICHMENT table="{table_name}"> found in {xml_path}')
    return blocks[-1].strip()

# ============================
# Understanding entry loader
# ============================
def get_understanding_entry(table_name: str) -> dict:
    mem = load_json(UNDERSTANDING_MEMORY_PATH, {"tables": {}})
    entry = mem.get("tables", {}).get(table_name)
    if not entry:
        raise ValueError(f"No understanding entry found for table '{table_name}' in {UNDERSTANDING_MEMORY_PATH}")
    return entry

# ============================
# Ontology texts
# ============================
def build_full_ontology_text() -> str:
    classes = ontology_explorer(mode="classes") or []
    return "CLASSES:\n" + "\n".join(classes)

def build_table_ontology_context(table_name: str, understanding_entry: dict, topk: int = 25) -> dict:
    import difflib

    classes = ontology_explorer(mode="classes") or []
    terms = [table_name] + (understanding_entry.get("synonyms") or [])

    def score(c: str) -> float:
        best = 0.0
        for t in terms:
            t = (t or "").lower().strip()
            if not t:
                continue
            best = max(best, difflib.SequenceMatcher(None, t, c.lower()).ratio())
        return best

    scored = [(c, score(c)) for c in classes]
    scored.sort(key=lambda x: x[1], reverse=True)
    top = [c for c, _ in scored[:topk]]

    ctx = {}
    for c in top[:10]:
        dprops = ontology_explorer(mode="data_properties", class_names=c).get(c, [])
        oprops = ontology_explorer(mode="object_properties", class_names=c).get(c, [])
        ctx[c] = {"data_properties": dprops, "object_properties": oprops}

    return {"top_candidates": top, "candidate_context": ctx}

# ============================
# Full schema text
# ============================
def build_full_schema_text() -> str:
    tables = load_json(os.path.join(DB_FOLDER, "tables.json"), [])
    cols = load_json(os.path.join(DB_FOLDER, "columns.json"), [])
    pks = load_json(os.path.join(DB_FOLDER, "primary_keys.json"), [])
    fks = load_json(os.path.join(DB_FOLDER, "foreign_keys.json"), [])

    tnames = []
    for t in tables:
        if isinstance(t, dict):
            tnames.append(str(t.get("table", "")).strip())
        else:
            tnames.append(str(t).strip())
    tnames = [t for t in tnames if t]

    out = []
    out.append("TABLES:")
    out.extend(sorted(set(tnames)))
    out.append("\nPRIMARY_KEYS:")
    for pk in pks:
        out.append(json.dumps(pk, ensure_ascii=False))
    out.append("\nFOREIGN_KEYS:")
    for fk in fks:
        out.append(json.dumps(fk, ensure_ascii=False))
    out.append("\nCOLUMNS (first 300):")
    for c in cols[:300]:
        out.append(json.dumps(c, ensure_ascii=False))
    return "\n".join(out)

# ============================
# Per-table bundle builder
# ============================
def build_table_bundle(table_name: str, base_prefix: str) -> Dict[str, Any]:
    understanding = get_understanding_entry(table_name)
    enrichment_xml = extract_enrichment_block(ENRICHMENT_XML_PATH, table_name)

    semantic_unit = get_semantic_unit(table_name)
    links_text = capture_printed(find_links_for_table, UNIT_LINKS_PATH, table_name)
    continuity_text = capture_printed(print_entity_continuity, table_name)

    patterns_discovery = load_json(PATTERNS_DISCOVERY_PATH, {})
    entity_cont_with_data = load_json(ENTITY_CONTINUITY_PATH, {}).get("entities", {}).get(table_name, {})

    ontology_ctx = build_table_ontology_context(table_name, understanding)

    return {
        "table_name": table_name,
        "base_prefix": base_prefix,
        "semantic_understanding": understanding,
        "semantic_enrichment_xml": enrichment_xml,
        "semantic_unit": semantic_unit,
        "unit_link_pairs_text": links_text,
        "entity_continuity_with_data": entity_cont_with_data,
        "patterns_discovery": {
            "pattern_label": get_table_pattern(table_name),
            "raw": patterns_discovery
        },
        "ontology_context": ontology_ctx,
        "relational_table_schema": {"columns": understanding.get("columns", {})},
        "continuity_text": continuity_text,
    }

# ============================
# TTL generation (prefixes once)
# ============================
PREFIX_TEMPLATE = """@prefix rr:  <http://www.w3.org/ns/r2rml#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix :   <{base_prefix}> .

"""

def ensure_prefixes(path: str, base_prefix: str) -> None:
    base_prefix = normalize_prefix_iri(base_prefix)

    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(PREFIX_TEMPLATE.format(base_prefix=base_prefix))
        return

    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    # if header already present, do nothing
    if "@prefix rr:" in content and "@prefix xsd:" in content and "@prefix :" in content:
        return

    with open(path, "w", encoding="utf-8") as f:
        f.write(PREFIX_TEMPLATE.format(base_prefix=base_prefix) + content.lstrip())

_PREFIX_LINE_RE = re.compile(r"^\s*@prefix\s+[^:]+:\s*<[^>]+>\s*\.\s*$", re.IGNORECASE)
_BASE_RE = re.compile(r"^\s*@base\s+<[^>]+>\s*\.\s*$", re.IGNORECASE)

def strip_ttl_header(ttl_text: str) -> str:
    lines = (ttl_text or "").splitlines()
    out = []
    for line in lines:
        if _PREFIX_LINE_RE.match(line) or _BASE_RE.match(line):
            continue
        out.append(line)
    return "\n".join(out).strip()

def build_ttl_from_final_struct(final_struct: dict) -> str:
    """
    Emits per-table TTL WITHOUT prefixes.
    - Uses :LocalName for classes and predicates
    - Uses rr:sqlQuery with SELECT * FROM "table" [+ optional WHERE expr]
    - Subject template: "<base_prefix><Class>/{id}" (single prefix, correct)
    """
    table = (final_struct.get("table") or "").strip()
    base_prefix = normalize_prefix_iri(final_struct.get("base_prefix") or "")
    chosen = final_struct.get("chosen") or {}
    chosen_kind = chosen.get("kind")
    chosen_name = local_name(chosen.get("name") or "")

    if not table or chosen_kind != "class" or not chosen_name:
        return ""

    sql = final_struct.get("sql") or {}
    sql_query = (sql.get("sqlQuery") or f'SELECT * FROM "{table}"').strip()

    filters = sql.get("filters") or []
    # apply ONLY if filters exist (planner decides); never auto-add "type=1"
    if filters:
        expr = (filters[0].get("expr") or "").strip()
        if expr and " where " not in sql_query.lower():
            sql_query = sql_query + " WHERE " + expr

    mappings = final_struct.get("mappings") or []

    tm = f"<urn:r2rml:{table}>"
    class_iri = f":{chosen_name}"

    # template is base_prefix + ClassLocalName + "/{id}"
    template = f"{base_prefix}{chosen_name}/{{id}}"

    ttl: List[str] = []
    ttl.append(f"{tm} a rr:TriplesMap ;")
    ttl.append("    rr:logicalTable [ a rr:R2RMLView ;")
    ttl.append(f'            rr:sqlQuery "{escape_ttl_string(sql_query)}" ] ;')
    ttl.append("    rr:subjectMap [ a rr:SubjectMap, rr:TermMap ;")
    ttl.append(f"            rr:class {class_iri} ;")
    ttl.append(f'            rr:template "{escape_ttl_string(template)}" ;')
    ttl.append("            rr:termType rr:IRI ] ;")

    pom_blocks = []
    for m in mappings:
        kind = (m.get("kind") or "").strip()
        if kind != "data_property":
            continue

        col_full = (m.get("column") or "").strip()
        col = col_full.split(".", 1)[1] if "." in col_full else col_full
        pred_local = local_name(m.get("predicate") or "")
        datatype = (m.get("datatype") or "xsd:string").strip()

        if not col or not pred_local:
            continue

        pom_blocks.append(
            "        [ a rr:PredicateObjectMap ;\n"
            "            rr:objectMap [ a rr:ObjectMap, rr:TermMap ;\n"
            f'                rr:column "{escape_ttl_string(col)}" ;\n'
            f"                rr:datatype {datatype} ;\n"
            "                rr:termType rr:Literal\n"
            "            ] ;\n"
            f"            rr:predicate :{pred_local}\n"
            "        ]"
        )

    if pom_blocks:
        ttl.append("    rr:predicateObjectMap")
        ttl.append(" ,\n".join(pom_blocks) + " .")
    else:
        ttl[-1] = ttl[-1].rstrip(" ;") + " ."

    ttl.append("")
    return "\n".join(ttl)

def append_ttl(ttl_block: str, base_prefix: str) -> str:
    ttl_block = strip_ttl_header(ttl_block)
    if not ttl_block:
        return ""

    ensure_prefixes(MAPPINGS_TTL_PATH, base_prefix)

    with open(MAPPINGS_TTL_PATH, "a", encoding="utf-8") as f:
        f.write("\n" + ttl_block + "\n")

    return MAPPINGS_TTL_PATH

# ============================
# Reports
# ============================
def save_mapping_report(table_name: str, report_obj: dict) -> str:
    os.makedirs(REPORTS_DIR, exist_ok=True)
    out_path = os.path.join(REPORTS_DIR, f"{_safe_name(table_name)}.json")
    save_json(out_path, report_obj)
    return out_path

# ============================
# Public API
# ============================
def mapper(table_name: str, base_prefix: Optional[str] = None) -> dict:
    base_prefix = resolve_base_prefix(base_prefix)

    full_schema_text = build_full_schema_text()
    full_ontology_text = build_full_ontology_text()

    bundle = build_table_bundle(table_name, base_prefix)

    planner = MapperPlanner(groq_client=client, model_name=MODEL_NAME)
    plan_out = planner.run(
        table_bundle=bundle,
        base_prefix=base_prefix,
        full_schema_text=full_schema_text,
        full_ontology_text=full_ontology_text,
    )

    final_struct = plan_out.get("final") or {}
    ttl_block = build_ttl_from_final_struct(final_struct)
    ttl_saved_to = append_ttl(ttl_block, base_prefix) if ttl_block else ""

    report = {
        "table": table_name,
        "base_prefix": base_prefix,
        "paths": {
            "db_folder": DB_FOLDER,
            "semantic_memory": SEM_MEM,
            "mappings_ttl": MAPPINGS_TTL_PATH,
            "reports_dir": REPORTS_DIR,
        },
        "planner": plan_out,
        "final_struct": final_struct,
        "ttl_emitted": bool(ttl_saved_to),
        "saved_to": ttl_saved_to,
    }
    report_path = save_mapping_report(table_name, report)

    return {
        "report_saved_to": report_path,
        "saved_to": ttl_saved_to,
        "r2rml_ttl": ttl_block,
        "final_struct": final_struct,
        "no_match": bool(plan_out.get("no_match")),
    }
