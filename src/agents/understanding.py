import json
import re
import io
import os
from contextlib import redirect_stdout
from groq import Groq
from dotenv import load_dotenv
from typing import Optional, List, Dict

from schema.pattern_retriever import get_table_pattern
from schema.units_retriever import get_semantic_unit
from schema.pairs_retriever import find_links_for_table
from schema.data_retriever import print_entity_continuity
from ontology.explorer import ontology_explorer

load_dotenv()

MODEL_NAME = "qwen/qwen3-32b"

_GROQ_KEY = os.environ.get("GROQ_API_KEY")
if not _GROQ_KEY:
    raise ValueError("GROQ_API_KEY is not set (put it in .env)")

client = Groq(api_key=_GROQ_KEY)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)

def strip_think(text: str) -> str:
    return _THINK_RE.sub("", text).strip()

def ask_groq(prompt: str) -> str:
    completion = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[{"role": "user", "content": prompt}],
    )
    return strip_think(completion.choices[0].message.content)

def capture_stdout(func, *args, **kwargs) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        func(*args, **kwargs)
    return buf.getvalue().strip()

def safe_parse_json(s: str) -> dict:
    try:
        return json.loads(s)
    except Exception:
        start = s.find("{")
        end = s.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(s[start:end+1])
    raise ValueError("LLM output is not valid JSON:\n" + s)

# ============================
# Paths (local)
# ============================
DB_FOLDER = os.environ.get("MPBOOT_DB_JSON_OUT", "src/utils_io/DB_json_out")
COLUMNS_PATH = os.path.join(DB_FOLDER, "columns.json")
LINKS_PATH = os.path.join(DB_FOLDER, "Semantic_Memory", "Unit_Links_Pairs.json")

UNDERSTANDING = os.path.join(
    DB_FOLDER, "Semantic_Memory", "Understanding_Memory.json"
)

def load_json_or_default(path: str, default):
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default

def save_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)

# ============================
# Schema helpers
# ============================
def get_column_types(table_name: str, columns_path: str = COLUMNS_PATH) -> dict:
    if not os.path.isfile(columns_path):
        return {}
    with open(columns_path, "r", encoding="utf-8") as f:
        cols_raw = json.load(f)

    t = table_name.strip()
    out = {}
    for c in cols_raw:
        if str(c.get("table", "")).strip() == t:
            cname = str(c.get("column", "")).strip()
            if cname:
                out[cname] = c.get("data_type")
    return out

def profile_table_columns(table_name: str) -> dict:
    return {}

def infer_signals_generic( col: str, dtype: Optional[str], stats: Optional[Dict]) -> List[str]: 
    lc = (col or "").strip().lower()
    dt = (dtype or "").lower()

    sig = set()

    if lc == "id" or lc.endswith("_id") or re.fullmatch(r"[a-z]*id", lc):
        sig.add("ID_LIKE")

    if any(x in dt for x in ["char", "text", "clob", "varchar"]):
        sig.add("TEXT_LIKE")
    if any(x in dt for x in ["int", "decimal", "numeric", "float", "double", "real"]):
        sig.add("NUMERIC_LIKE")
    if any(x in dt for x in ["date", "time", "timestamp"]):
        sig.add("TEMPORAL_LIKE")

    if isinstance(stats, dict):
        null_frac = stats.get("null_frac")
        if isinstance(null_frac, (int, float)) and null_frac > 0:
            sig.add("NULLABLE")

        if stats.get("is_boolean_like") is True:
            sig.add("FLAG_LIKE")

        if stats.get("is_low_cardinality") is True:
            sig.add("DISCRIMINATOR_LIKE")

        if stats.get("distinct") == 1:
            sig.add("CONSTANT_LIKE")

    if "DISCRIMINATOR_LIKE" in sig and ("TEXT_LIKE" in sig or "NUMERIC_LIKE" in sig):
        sig.add("CODE_LIKE")

    return sorted(sig)

def understanding(table_name: str) -> dict:
    pattern_label = get_table_pattern(table_name)
    col_types = get_column_types(table_name)
    stats_map = profile_table_columns(table_name) or {}

    links_text = capture_stdout(find_links_for_table, LINKS_PATH, table_name)
    continuity_text = capture_stdout(print_entity_continuity, table_name)
    unit_list = get_semantic_unit(table_name) or []

    col_types_lines = "\n".join([f"- {c}: {col_types[c]}" for c in sorted(col_types)]) or "(no datatype info found)"

    prompt = f"""
You are a semantic understanding agent for relational schemas.

Output ONLY valid JSON with:
- table_definition: <= 20 words
- table_synonyms: 2-4 short synonyms
- columns: for each column -> definition (<= 10 words) + 2-4 synonyms

TABLE: {table_name}
PATTERN: {pattern_label}

COLUMN TYPES:
{col_types_lines}

LINK EVIDENCE:
{links_text}

CONTINUITY EVIDENCE:
{continuity_text}

SEMANTIC UNIT:
{", ".join(unit_list)}

JSON schema:
{{
  "table_definition": "...",
  "table_synonyms": ["...","..."],
  "columns": {{
    "colA": {{"definition": "...", "synonyms": ["...","..."]}},
    "colB": {{"definition": "...", "synonyms": ["...","..."]}}
  }}
}}
""".strip()

    raw = ask_groq(prompt)
    parsed = safe_parse_json(raw)

    llm_cols = parsed.get("columns", {})
    if not isinstance(llm_cols, dict):
        llm_cols = {}

    cols_out = {}
    for col in sorted(col_types.keys()):
        llm_meta = llm_cols.get(col, {})
        if not isinstance(llm_meta, dict):
            llm_meta = {}
        cdef = (llm_meta.get("definition") or "").strip()
        csyn = llm_meta.get("synonyms") or []
        sigs = infer_signals_generic(col, col_types.get(col), stats_map.get(col))

        cols_out[col] = {
            "type": col_types.get(col),
            "definition": cdef,
            "synonyms": csyn,
        }

    try:
        _ = ontology_explorer(mode="classes") or []
    except Exception:
        pass

    entry = {
        "pattern": pattern_label,
        "definition": (parsed.get("table_definition") or "").strip(),
        "synonyms": parsed.get("table_synonyms") or [],
        "columns": cols_out,
        "relationships_evidence": {
            "links_text": links_text,
            "continuity_text": continuity_text,
            "semantic_unit": unit_list
        }
    }

    mem = load_json_or_default(UNDERSTANDING, {"tables": {}})
    mem.setdefault("tables", {})
    mem["tables"][table_name] = entry
    save_json(UNDERSTANDING, mem)

    return entry
