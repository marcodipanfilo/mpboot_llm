import os
import re
import json
import io
from contextlib import redirect_stdout
from groq import Groq
from dotenv import load_dotenv

load_dotenv()

# ============================
# Groq configuration
# ============================
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

def load_json_or_default(path: str, default):
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default

def capture_stdout(func, *args, **kwargs) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        func(*args, **kwargs)
    return buf.getvalue().strip()

# ============================
# Paths (local)
# ============================
DB_FOLDER = os.environ.get("MPBOOT_DB_JSON_OUT", "src/utils_io/DB_json_out")
UNDERSTANDING_MEMORY_PATH_V2 = os.path.join(DB_FOLDER, "Semantic_Memory", "Understanding_Memory.json")
ENRICHMENT_MEMORY_PATH_V3_XML = os.path.join(DB_FOLDER, "Semantic_Memory", "Enrichment_Memory.xml")

# ============================
# Optional: data samples hook
# ============================
def try_get_samples_text(table_name: str, limit: int = 5) -> str:
    if "get_table_samples" in globals() and callable(globals()["get_table_samples"]):
        try:
            s = globals()["get_table_samples"](table_name, limit=limit)
            if isinstance(s, (dict, list)):
                return json.dumps(s, ensure_ascii=False)[:2000]
            return str(s)[:2000]
        except Exception:
            return ""
    return ""

# ============================
# Very light sanity check
# ============================
def ensure_one_root_enrichment(xml_text: str, table_name: str) -> None:
    if f'<ENRICHMENT table="{table_name}">' not in xml_text:
        raise ValueError("Enrichment output missing <ENRICHMENT table=...> root.")
    if "</ENRICHMENT>" not in xml_text:
        raise ValueError("Enrichment output missing closing </ENRICHMENT>.")

# ============================
# Enrichment (your naming)
# ============================
def enrichment(table_name: str, limit_samples: int = 5) -> str:
    """
    Input: Understanding_Memory_v2.json entry for table_name
    Output: Minimal HTML-like tags per column
    Saves: appended block to Enrichment_Memory_v3.xml
    """

    u = load_json_or_default(UNDERSTANDING_MEMORY_PATH_V2, {"tables": {}})
    entry = u.get("tables", {}).get(table_name)
    if not entry:
        raise ValueError(
            f"No Understanding v2 entry found for table '{table_name}' in {UNDERSTANDING_MEMORY_PATH_V2}"
        )

    pattern   = entry.get("pattern", "")
    tdef      = entry.get("definition", "")
    tsyn      = entry.get("synonyms", [])
    cols      = entry.get("columns", {}) or {}

    col_lines = []
    for col, meta in cols.items():
        ctype = meta.get("type", "")
        cdef  = meta.get("definition", "")
        csyn  = meta.get("synonyms", [])
        csyn_s = ", ".join([str(x) for x in csyn[:4]])
        col_lines.append(f"- {col} | type={ctype} | def={cdef} | syn=[{csyn_s}]")
    cols_text = "\n".join(col_lines) if col_lines else "(no columns found)"

    samples_text = try_get_samples_text(table_name, limit=limit_samples)

    prompt = f"""
You are a mapping-oriented enrichment agent.

Task:
For EACH COLUMN in the table, output ONLY minimal HTML-like tags that help mapping.
Do NOT output explanations. Do NOT output JSON. Output only the XML-like block.

You must infer column semantics using:
- table definition, pattern
- column type + column definition + synonyms
- optional data samples (if present)

Output format STRICT:
<ENRICHMENT table="{table_name}">
  <PATTERN>{pattern}</PATTERN>
  <COLUMN name="{table_name}.colA">
    <intent>literal|link|discriminator|flag|label|status|decision|text|number|time</intent>
    <role>short role label (if applicable)</role>
    <range>xsd:string|xsd:integer|xsd:boolean|xsd:dateTime|IRI (optional)</range>
  </COLUMN>
  ...
</ENRICHMENT>

Rules:
- For each column: ALWAYS output <mapping> and <intent>.
- Use at most 3 extra tags among: <role>, <filter>, <range>.
- If column can support 2 possibilities, output a SECOND mapping+intent as alternatives:
    <mapping alt="1">...</mapping>
    <intent  alt="1">...</intent>
  (max one alternative).
- Keep tag values short.
- <filter> only when it changes table/class mapping (discriminator or flag).
- Be generic: do not assume any fixed domain (conference etc).

INPUT:
TABLE_DEF: {tdef}
TABLE_SYNONYMS: {", ".join([str(x) for x in tsyn])}

COLUMNS:
{cols_text}

DATA_SAMPLES (may be empty):
{samples_text}
""".strip()

    xml = ask_groq(prompt)
    ensure_one_root_enrichment(xml, table_name)

    os.makedirs(os.path.dirname(ENRICHMENT_MEMORY_PATH_V3_XML), exist_ok=True)
    with open(ENRICHMENT_MEMORY_PATH_V3_XML, "a", encoding="utf-8") as f:
        f.write(xml.strip() + "\n\n")

    return xml
