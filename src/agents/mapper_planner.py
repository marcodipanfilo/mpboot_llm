# src/agents/mapper_planner.py
import re
import json
from typing import Dict, Any, Optional, List
from groq import Groq

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def strip_think(text: str) -> str:
    return _THINK_RE.sub("", (text or "")).strip()


def safe_parse_json(s: str) -> dict:
    if not isinstance(s, str):
        raise ValueError("LLM output is not a string")
    s = s.strip()

    # strip markdown fences
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s).strip()

    try:
        return json.loads(s)
    except Exception:
        pass

    start = s.find("{")
    if start == -1:
        raise ValueError("LLM output has no JSON object:\n" + s[:500])

    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        else:
            if ch == '"':
                in_str = True
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = s[start : i + 1]
                    return json.loads(candidate)

    raise ValueError("Unterminated JSON object in LLM output:\n" + s[:800])


class MapperPlanner:
    """
    Revised planning:
      Step0: Analyze global schema + ontology (cached)
      Step1:
        p0: Decide match (class or none), pick best + alt, with scores
        p1: Decide whether a discriminator/filter is needed (ONLY if strong evidence)
        p2: Column-to-property mapping (prefer provided ontology context)
        p3: Build final_struct for TTL generator (no full IRIs, use local names)
    """

    def __init__(self, groq_client: Groq, model_name: str):
        self.client = groq_client
        self.model = model_name
        self._step0_cache: Optional[dict] = None

    def _ask(self, prompt: str) -> str:
        completion = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
        )
        return strip_think(completion.choices[0].message.content)

    # ---------- Step 0 ----------
    def step0_analyze_schema_and_ontology(
        self,
        full_schema_text: str,
        full_ontology_text: str,
        force: bool = False,
    ) -> dict:
        if self._step0_cache is not None and not force:
            return self._step0_cache

        prompt = f"""
You will be used later to generate R2RML mappings.
First, analyze BOTH the relational schema and the ontology.

Return ONLY JSON with:
{{
  "schema_summary": {{
    "main_entity_tables": ["..."],
    "relationship_tables": ["..."],
    "notable_discriminators": ["table.col", "..."]
  }},
  "ontology_summary": {{
    "main_classes": ["..."],
    "high_value_object_properties": ["propName", "..."]
  }},
  "notes": ["..."]
}}

RELATIONAL_SCHEMA:
{full_schema_text}

ONTOLOGY (high level):
{full_ontology_text}
""".strip()

        raw = self._ask(prompt)
        self._step0_cache = safe_parse_json(raw)
        return self._step0_cache

    # ---------- Step 1 / p0 ----------
    def p0_match_or_no_match(self, table_bundle: Dict[str, Any], base_prefix: str) -> dict:
        """
        Output JSON:
        {
          "table":"...",
          "no_match": false,
          "best": {"kind":"class", "name":"LocalClassName", "score":0..1, "why":"..."},
          "alt":  {"kind":"class", "name":"LocalClassName", "score":0..1, "why":"..."},
          "stop_reason": null
        }

        Rules:
        - ONLY choose kind="class" for table-level mapping.
        - If table is clearly SR/bridge-only, set no_match=true (mapper will skip TTL).
        - name must be LOCAL NAME ONLY (no prefix, no full IRI).
        """
        # keep bundle bounded
        bundle_text = json.dumps(table_bundle, ensure_ascii=False)
        if len(bundle_text) > 14000:
            bundle_text = bundle_text[:14000]

        prompt = f"""
You are a mapper planner.

Goal:
Decide whether TABLE maps to an ontology CLASS.
Return ONLY JSON. No text.

STRICT rules:
- Output class names as LOCAL NAMES only (e.g., "Person"), NEVER as "http://...#Person" and NEVER as ":Person".
- If the table looks like a pure relationship/bridge table (SR/SRR), set no_match=true.
- Use ontology_context.top_candidates and candidate_context as the primary ontology evidence.

BASE_PREFIX (informative only): {base_prefix}

JSON schema:
{{
  "table":"<table>",
  "no_match": false,
  "best": {{"kind":"class","name":"ClassLocalName","score":0.0,"why":"..."}},
  "alt":  {{"kind":"class","name":"ClassLocalName","score":0.0,"why":"..."}},
  "stop_reason": null
}}

INPUT_BUNDLE (single table):
{bundle_text}
""".strip()

        raw = self._ask(prompt)
        out = safe_parse_json(raw)

        # Minimal hardening
        out.setdefault("table", table_bundle.get("table_name"))
        out.setdefault("no_match", False)
        for k in ["best", "alt"]:
            if isinstance(out.get(k), dict):
                # strip any accidental IRI/prefix
                name = (out[k].get("name") or "").strip()
                if "#" in name:
                    name = name.split("#")[-1]
                if "/" in name:
                    name = name.rsplit("/", 1)[-1]
                if name.startswith(":"):
                    name = name[1:]
                out[k]["name"] = name
                out[k]["kind"] = "class"
        return out

    # ---------- Step 1 / p1 ----------
    def p1_discriminator_and_sql_strategy(self, table_bundle: Dict[str, Any], match_p0: dict) -> dict:
        """
        Output JSON:
        {
          "table":"...",
          "use_filter": true|false,
          "filters":[{"expr":"col = <VAL>","reason":"...","needs_code_discovery":true|false}],
          "use_join": true|false,
          "join_hints":[{"target_table":"...","source_column":"table.col","target_column":"other.pk","reason":"..."}],
          "notes":[...]
        }

        Critical rule:
        - DO NOT add filters unless strong evidence (column name/type/synonyms + enrichment + continuity/links + known discriminator semantics).
        - If values are unknown numeric codes: prefer use_filter=false unless there is explicit evidence it is a type/discriminator column.
        """
        bundle_text = json.dumps(table_bundle, ensure_ascii=False)
        if len(bundle_text) > 14000:
            bundle_text = bundle_text[:14000]

        prompt = f"""
You are a mapper planner.

Task:
Decide SQL strategy for the TABLE mapping:
- base query: SELECT * FROM "table"
- optional WHERE filter (ONLY if strongly justified)
- optional JOIN (ONLY if needed to realize an object property mapping or to disambiguate class)

Return ONLY JSON.

STRICT rules:
- Default: use_filter=false, use_join=false.
- Add WHERE filter only if:
  (a) a column is clearly a discriminator/category/status AND
  (b) filtering is required to map the table to the chosen class (otherwise don't).
- If codes are numeric and meaning unknown:
  - set needs_code_discovery=true
  - still propose expr with <VAL> placeholder (NOT "1") unless you have explicit evidence.
- Prefer <VAL> placeholder over hardcoding 1.

JSON schema:
{{
  "table":"<table>",
  "use_filter": false,
  "filters":[{{"expr":"...","reason":"...","needs_code_discovery":false}}],
  "use_join": false,
  "join_hints":[{{"target_table":"...","source_column":"table.col","target_column":"other.pk","reason":"..."}}],
  "notes":["..."]
}}

MATCH_DECISION (p0):
{json.dumps(match_p0, ensure_ascii=False)}

INPUT_BUNDLE:
{bundle_text}
""".strip()

        raw = self._ask(prompt)
        out = safe_parse_json(raw)
        out.setdefault("table", table_bundle.get("table_name"))
        out.setdefault("use_filter", False)
        out.setdefault("filters", [])
        out.setdefault("use_join", False)
        out.setdefault("join_hints", [])
        out.setdefault("notes", [])
        return out

    # ---------- Step 1 / p2 ----------
    def p2_column_mapping(self, table_bundle: Dict[str, Any], match_p0: dict) -> dict:
        """
        Output JSON:
        {
          "table":"...",
          "class":"LocalClassName",
          "column_mappings":[
            {"column":"table.col","kind":"data_property","predicate":"propLocalName","datatype":"xsd:string","confidence":0..1,"notes":"..."},
            {"column":"table.col","kind":"object_property","predicate":"propLocalName","object_class":"LocalClassName","join":{...},"confidence":0..1,"notes":"..."},
            {"column":"table.col","kind":"unmapped","confidence":0..1,"notes":"..."}
          ]
        }

        Rules:
        - predicate MUST be local name only (no prefix, no IRI).
        - class/object_class MUST be local name only.
        - Prefer ontology_context.candidate_context[class] properties.
        """
        bundle_text = json.dumps(table_bundle, ensure_ascii=False)
        if len(bundle_text) > 14000:
            bundle_text = bundle_text[:14000]

        prompt = f"""
You are a mapper planner.

Task:
Map each table column to ontology properties of the chosen class.

STRICT output format rules:
- Return ONLY JSON.
- Use LOCAL NAMES only:
  - class: "Person" not ":Person" not "http://...#Person"
  - predicate: "hasName" not ":hasName" not "http://...#hasName"
- If you can't find a suitable predicate: kind="unmapped".

JOIN rule:
- Only output kind="object_property" if you can justify a join via:
  unit_link_pairs_text and/or entity_continuity_with_data and/or foreign_keys.
- Provide join with target_table/source_column/target_column.

JSON schema:
{{
  "table":"<table>",
  "class":"LocalClassName",
  "column_mappings":[
    {{"column":"table.col","kind":"data_property","predicate":"propLocal","datatype":"xsd:string","confidence":0.0,"notes":"..."}},
    {{"column":"table.col","kind":"object_property","predicate":"propLocal","object_class":"LocalClassName","join":{{"target_table":"T","source_column":"table.c","target_column":"T.pk"}},"confidence":0.0,"notes":"..."}},
    {{"column":"table.col","kind":"unmapped","confidence":0.0,"notes":"..."}}
  ]
}}

MATCH_DECISION (p0):
{json.dumps(match_p0, ensure_ascii=False)}

INPUT_BUNDLE:
{bundle_text}
""".strip()

        raw = self._ask(prompt)
        out = safe_parse_json(raw)

        # harden local names
        cls = (out.get("class") or (match_p0.get("best") or {}).get("name") or "").strip()
        if "#" in cls:
            cls = cls.split("#")[-1]
        if cls.startswith(":"):
            cls = cls[1:]
        out["class"] = cls

        for m in out.get("column_mappings", []) if isinstance(out.get("column_mappings"), list) else []:
            pred = (m.get("predicate") or "").strip()
            if pred.startswith(":"):
                pred = pred[1:]
            if "#" in pred:
                pred = pred.split("#")[-1]
            if "/" in pred:
                pred = pred.rsplit("/", 1)[-1]
            m["predicate"] = pred

            oc = (m.get("object_class") or "").strip()
            if oc.startswith(":"):
                oc = oc[1:]
            if "#" in oc:
                oc = oc.split("#")[-1]
            if "/" in oc:
                oc = oc.rsplit("/", 1)[-1]
            if oc:
                m["object_class"] = oc

        out.setdefault("table", table_bundle.get("table_name"))
        out.setdefault("column_mappings", [])
        return out

    # ---------- Step 1 / p3 ----------
    def p3_build_final_struct(
        self,
        table_name: str,
        base_prefix: str,
        match_p0: dict,
        sql_p1: dict,
        colmap_p2: dict,
    ) -> dict:
        """
        Output JSON consumed by mapper.py generator:
        {
          "table":"...",
          "base_prefix":"...#",
          "chosen":{"kind":"class","name":"LocalClassName"},
          "sql":{"sqlQuery":"SELECT * FROM \"table\"","filters":[...],"assumptions":[...],"needs_code_discovery":[...]},
          "mappings":[...],
          "planner_notes":[...]
        }

        Rules:
        - chosen.name is local name only
        - data_property predicate is local name only
        - filters are included only if sql_p1.use_filter=true
        """
        prompt = f"""
You are a mapper planner.

Task:
Build FINAL_STRUCT JSON for a mapper generator.

STRICT rules:
- class and predicates must be LOCAL NAMES only.
- Include WHERE filters only if sql_p1.use_filter=true.
- If needs_code_discovery is true, include that column in needs_code_discovery and use <VAL>.

Return ONLY JSON.

Inputs:
MATCH_P0:
{json.dumps(match_p0, ensure_ascii=False)}

SQL_P1:
{json.dumps(sql_p1, ensure_ascii=False)}

COLMAP_P2:
{json.dumps(colmap_p2, ensure_ascii=False)}

Required schema:
{{
  "table":"{table_name}",
  "base_prefix":"{base_prefix}",
  "chosen":{{"kind":"class","name":"LocalClassName"}},
  "sql":{{
    "sqlQuery":"SELECT * FROM \\"{table_name}\\"",
    "filters":[{{"expr":"...","reason":"...","needs_code_discovery":false}}],
    "assumptions":["..."],
    "needs_code_discovery":["{table_name}.col", "..."]
  }},
  "mappings":[
    {{"kind":"data_property","column":"{table_name}.col","predicate":"propLocal","datatype":"xsd:string"}},
    {{"kind":"object_property","column":"{table_name}.col","predicate":"propLocal","object_class":"ClassLocal","join":{{"target_table":"T","source_column":"{table_name}.c","target_column":"T.pk"}}}}
  ],
  "planner_notes":["..."]
}}
""".strip()

        raw = self._ask(prompt)
        out = safe_parse_json(raw)

        # harden
        out["table"] = table_name
        out["base_prefix"] = base_prefix
        chosen = out.get("chosen") if isinstance(out.get("chosen"), dict) else {}
        cname = (chosen.get("name") or (match_p0.get("best") or {}).get("name") or "").strip()
        if cname.startswith(":"):
            cname = cname[1:]
        if "#" in cname:
            cname = cname.split("#")[-1]
        if "/" in cname:
            cname = cname.rsplit("/", 1)[-1]
        out["chosen"] = {"kind": "class", "name": cname}

        # ensure sql defaults
        sql = out.get("sql") if isinstance(out.get("sql"), dict) else {}
        sql.setdefault("sqlQuery", f'SELECT * FROM "{table_name}"')
        sql.setdefault("filters", [])
        sql.setdefault("assumptions", [])
        sql.setdefault("needs_code_discovery", [])
        out["sql"] = sql

        # normalize predicate locals
        maps = out.get("mappings") if isinstance(out.get("mappings"), list) else []
        for m in maps:
            pred = (m.get("predicate") or "").strip()
            if pred.startswith(":"):
                pred = pred[1:]
            if "#" in pred:
                pred = pred.split("#")[-1]
            if "/" in pred:
                pred = pred.rsplit("/", 1)[-1]
            m["predicate"] = pred
            oc = (m.get("object_class") or "").strip()
            if oc.startswith(":"):
                oc = oc[1:]
            if "#" in oc:
                oc = oc.split("#")[-1]
            if "/" in oc:
                oc = oc.rsplit("/", 1)[-1]
            if oc:
                m["object_class"] = oc
        out["mappings"] = maps
        out.setdefault("planner_notes", [])
        return out

    # ---------- run ----------
    def run(
        self,
        table_bundle: Dict[str, Any],
        base_prefix: str,
        full_schema_text: str,
        full_ontology_text: str,
    ) -> dict:
        step0 = self.step0_analyze_schema_and_ontology(full_schema_text, full_ontology_text)

        p0 = self.p0_match_or_no_match(table_bundle, base_prefix=base_prefix)
        no_match = bool(p0.get("no_match"))

        if no_match:
            table_name = table_bundle.get("table_name") or ""
            final = {
                "table": table_name,
                "base_prefix": base_prefix,
                "chosen": {"kind": None, "name": None},
                "sql": {
                    "sqlQuery": f'SELECT * FROM "{table_name}"',
                    "filters": [],
                    "assumptions": ["no_match=true; stopped at p0"],
                    "needs_code_discovery": []
                },
                "mappings": [],
                "planner_notes": ["no_match=true"]
            }
            return {"step0": step0, "p0": p0, "p1": {}, "p2": {}, "final": final, "no_match": True}

        p1 = self.p1_discriminator_and_sql_strategy(table_bundle, match_p0=p0)
        p2 = self.p2_column_mapping(table_bundle, match_p0=p0)
        final = self.p3_build_final_struct(
            table_name=table_bundle.get("table_name") or "",
            base_prefix=base_prefix,
            match_p0=p0,
            sql_p1=p1,
            colmap_p2=p2,
        )

        return {"step0": step0, "p0": p0, "p1": p1, "p2": p2, "final": final, "no_match": False}
