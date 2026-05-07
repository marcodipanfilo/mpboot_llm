"""
Database Enrichment Agent
Enriches ALL database tables with semantic metadata for ontology mapping.

Reads  : src/outputs/DB_as_json/tables_structure.json
         src/outputs/DB_as_json/relationship_summary.json
         src/memory/understanding.json
         src/inputs/database/dump.sql
Writes : src/memory/enrichment.json

Output shape (simple flat JSON):
{
  "table_name": {
    "entity_type": "core_entity",
    "column_enrichment": {
      "col_name": { "role": "...", "mapping_hint": "..." },
      ...
    },
    "enum_interpretations": {
      "col_name": { "value": "meaning", ... }
    }
  },
  ...
}
"""

import json
import os
import re
import sys
import requests
from typing import Dict, List, Any, Optional
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.llm_config import LLMConfig
from config.llm_config import SELECTED_PROVIDER
# ===== PATHS =====
TABLES_STRUCTURE_FILE  = "src/outputs/DB_as_json/tables_structure.json"
RELATIONSHIP_FILE      = "src/outputs/DB_as_json/relationship_summary.json"
UNDERSTANDING_FILE     = "src/memory/understanding.json"
DUMP_FILE              = "src/inputs/database/dump.sql"
OUTPUT_DIR             = "src/memory"
OUTPUT_FILE            = os.path.join(OUTPUT_DIR, "enrichment.json")


class EnrichmentAgent:
    """Enriches every table with semantic metadata for ontology mapping."""

    def __init__(self, provider: str = SELECTED_PROVIDER):
        self.provider = provider
        self.config   = LLMConfig.get_config(provider)
        print(f"Initialized EnrichmentAgent with provider: {provider}")
        print(f"Model: {self.config['model_name']}")

    # ------------------------------------------------------------------
    # LLM call methods
    # ------------------------------------------------------------------

    def strip_thinking_tags(self, text: str) -> str:
        return re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()

    def _has_json(self, text: str) -> bool:
        """Return True if text contains a parseable JSON object after stripping think tags."""
        cleaned = self.strip_thinking_tags(text)
        j_start = cleaned.find('{')
        j_end   = cleaned.rfind('}') + 1
        if j_start == -1 or j_end == 0:
            return False
        try:
            json.loads(cleaned[j_start:j_end])
            return True
        except Exception:
            # Even partial JSON counts — check for key fields
            return '"entity_type"' in cleaned

    def get_llm_response(self, prompt: str) -> str:
        if self.provider == "claude":
            return self._get_claude_response(prompt)
        elif self.provider == "gemini":
            return self._get_gemini_response(prompt)
        else:
            return self._get_openai_compatible_response(prompt)

    def _get_openai_compatible_response(self, prompt: str) -> str:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config['api_key']}"
        }

        # --- First attempt ---
        data = {
            "model": self.config['model_name'],
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": 1024
        }
        response = requests.post(self.config['api_url'], headers=headers, json=data)
        if response.status_code != 200:
            raise Exception(f"API request failed: {response.status_code} - {response.text}")

        raw = response.json()["choices"][0]["message"]["content"]

        # If the model produced a think block but no JSON after it, do a follow-up
        # asking it to output only the JSON — no reasoning allowed.
        if self.provider == "groq" and not self._has_json(raw):
            print(f"\n  [RETRY] Model only returned thinking, requesting JSON output...", end="", flush=True)
            retry_data = {
                "model": self.config['model_name'],
                "messages": [
                    {"role": "user",      "content": prompt},
                    {"role": "assistant", "content": raw},
                    {"role": "user",      "content": (
                        "You only produced reasoning but no JSON output. "
                        "Now output ONLY the JSON object as instructed. "
                        "No thinking, no explanation, just the raw JSON."
                    )}
                ],
                "temperature": 0.1,
                "max_tokens": 1024
            }
            retry_resp = requests.post(self.config['api_url'], headers=headers, json=retry_data)
            if retry_resp.status_code == 200:
                raw = retry_resp.json()["choices"][0]["message"]["content"]

        if self.provider == "groq":
            raw = self.strip_thinking_tags(raw)

        return raw

    def _get_claude_response(self, prompt: str) -> str:
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.config['api_key'],
            "anthropic-version": "2023-06-01"
        }
        data = {
            "model": self.config['model_name'],
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3
        }
        response = requests.post(self.config['api_url'], headers=headers, json=data)
        if response.status_code != 200:
            raise Exception(f"Claude API request failed: {response.status_code}")
        return response.json()["content"][0]["text"]

    def _get_gemini_response(self, prompt: str) -> str:
        url = f"{self.config['api_url']}/{self.config['model_name']}:generateContent?key={self.config['api_key']}"
        headers = {"Content-Type": "application/json"}
        data = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.3, "maxOutputTokens": 1024}
        }
        response = requests.post(url, headers=headers, json=data)
        if response.status_code != 200:
            raise Exception(f"Gemini API request failed: {response.status_code}")
        return response.json()["candidates"][0]["content"]["parts"][0]["text"]

    # ------------------------------------------------------------------
    # Enum / pattern detector (reads SQL dump)
    # ------------------------------------------------------------------

    def _detect_enums(self, dump_file: str, table_name: str, columns: List[Dict]) -> Dict[str, Dict]:
        """
        For each column, check if it has low cardinality (≤15 distinct values).
        Returns { col_name: { "distinct_values": [...], "distribution": {...} } }
        """
        enums = {}
        try:
            with open(dump_file, 'r', encoding='utf-8', errors='ignore') as f:
                sql_content = f.read()

            copy_match = re.search(
                rf'COPY\s+{re.escape(table_name)}\s+\(([^)]+)\)\s+FROM\s+stdin;(.*?)(?=\\\.)',
                sql_content, re.IGNORECASE | re.DOTALL
            )
            if not copy_match:
                return enums

            col_names  = [c.strip() for c in copy_match.group(1).split(',')]
            data_lines = copy_match.group(2).strip().split('\n')

            # Collect values per column
            col_values: Dict[str, List[str]] = {c['name']: [] for c in columns}

            for line in data_lines[:200]:
                line = line.strip()
                if not line or line.startswith('--'):
                    continue
                parts = line.split('\t')
                for col in columns:
                    if col['name'] in col_names:
                        idx = col_names.index(col['name'])
                        if idx < len(parts):
                            val = parts[idx].strip()
                            if val != r'\N':
                                col_values[col['name']].append(val)

            for col_name, vals in col_values.items():
                if not vals:
                    continue
                counts   = Counter(vals)
                distinct = len(counts)
                if distinct <= 15:
                    enums[col_name] = {
                        "distinct_values":  list(counts.keys()),
                        "distribution":     dict(counts.most_common())
                    }

        except Exception as e:
            pass  # dump may not exist or table may have no COPY block — silently skip

        return enums

    # ------------------------------------------------------------------
    # Prompt builder
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        table_name: str,
        columns: List[Dict],
        relationships: List[str],
        table_meaning: str,
        column_meanings: Dict[str, str],
        enum_patterns: Dict[str, Dict],
    ) -> str:

        # Columns block
        col_lines = []
        for col in columns:
            parts = [f"{col['name']} ({col.get('data_type', '?')})"]
            if col.get('is_primary_key'):
                parts.append("[PK]")
            if col.get('is_foreign_key') and col.get('foreign_key_reference'):
                ref = col['foreign_key_reference']
                parts.append(f"[FK → {ref['table']}]")
            if col['name'] in column_meanings:
                parts.append(f"— {column_meanings[col['name']]}")
            if col['name'] in enum_patterns:
                ep = enum_patterns[col['name']]
                parts.append(f"| distinct values: {ep['distinct_values']}")
            col_lines.append("  - " + " ".join(parts))

        cols_block = "\n".join(col_lines) if col_lines else "  (none)"
        rels_block = ", ".join(relationships) if relationships else "none"

        return f"""You are a database semantics expert. Enrich this table's metadata for ontology mapping.

TABLE: {table_name}
MEANING: {table_meaning}
RELATED TABLES: {rels_block}

COLUMNS:
{cols_block}

Return ONLY a JSON object, no markdown, no extra text:

{{
  "entity_type": "<core_entity | junction_table | lookup_table>",
  "column_enrichment": {{
    "col_name": {{
      "role": "<identifier | name | temporal | status | measurement | content | reference>",
      "mapping_hint": "<unique_identifier | primary_label | description_text | state_indicator | object_property | data_property>"
    }}
  }},
  "enum_interpretations": {{
    "col_name_with_enum": {{
      "value": "what this value means"
    }}
  }}
}}

Rules:
- Provide an entry in column_enrichment for EVERY column listed above.
- Only include columns that have "distinct values" listed in enum_interpretations.
- If no enum columns exist use: "enum_interpretations": {{}}"""

    # ------------------------------------------------------------------
    # Response parser (3-strategy fallback)
    # ------------------------------------------------------------------

    def _parse_response(self, response: str, table_name: str) -> Optional[Dict]:
        # Strategy 1 — JSON block
        try:
            cleaned = re.sub(r'```json\s*', '', response)
            cleaned = re.sub(r'```\s*', '', cleaned).strip()
            j_start = cleaned.find('{')
            j_end   = cleaned.rfind('}') + 1
            if j_start != -1 and j_end > 0:
                obj = json.loads(cleaned[j_start:j_end])
                if 'entity_type' in obj:
                    return obj
        except (json.JSONDecodeError, ValueError):
            pass

        # Strategy 2 — regex entity_type extraction
        et_match = re.search(r'"entity_type"\s*:\s*"([^"]+)"', response)
        if et_match:
            print(f"  [INFO] Enrichment recovered via regex for {table_name}")
            ce_match  = re.search(r'"column_enrichment"\s*:\s*(\{.*?\})\s*[,}]', response, re.DOTALL)
            ei_match  = re.search(r'"enum_interpretations"\s*:\s*(\{.*?\})\s*}', response, re.DOTALL)
            return {
                "entity_type": et_match.group(1),
                "column_enrichment": json.loads(ce_match.group(1)) if ce_match else {},
                "enum_interpretations": json.loads(ei_match.group(1)) if ei_match else {}
            }

        # Strategy 3 — give up
        print(f"  [WARN] Could not parse enrichment for {table_name}")
        print(f"  [WARN] Raw: {response[:300]}")
        return None

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(
        self,
        tables_structure: Dict,
        relationships_summary: Dict,
        understanding: Dict,
        dump_file: str = DUMP_FILE,
    ) -> Dict:
        """Enrich every table and return the full enrichment dict."""

        os.makedirs(OUTPUT_DIR, exist_ok=True)

        # Load existing progress for resumable runs
        if os.path.exists(OUTPUT_FILE):
            with open(OUTPUT_FILE, 'r', encoding='utf-8') as f:
                enrichment = json.load(f)
            print(f"  Loaded existing enrichment.json ({len(enrichment)} tables already done)")
        else:
            enrichment = {}

        tables = list(tables_structure.keys())
        total  = len(tables)
        print(f"\nEnriching {total} tables...\n")

        for idx, table_name in enumerate(tables, 1):
            if table_name in enrichment:
                print(f"[{idx:>3}/{total}] {table_name:<40} → already done, skipping")
                continue

            print(f"[{idx:>3}/{total}] {table_name:<40} ", end="", flush=True)

            columns       = tables_structure[table_name].get('columns', [])
            relationships = relationships_summary.get(table_name, [])
            table_und     = understanding.get(table_name, {})
            table_meaning = table_und.get('table_meaning', 'Not available')
            col_meanings  = table_und.get('columns', {})

            enum_patterns = self._detect_enums(dump_file, table_name, columns)

            prompt = self._build_prompt(
                table_name, columns, relationships,
                table_meaning, col_meanings, enum_patterns
            )

            try:
                response = self.get_llm_response(prompt)
                result   = self._parse_response(response, table_name)

                if result:
                    enrichment[table_name] = {
                        "entity_type":          result.get("entity_type", "unknown"),
                        "column_enrichment":    result.get("column_enrichment", {}),
                        "enum_interpretations": result.get("enum_interpretations", {})
                    }
                    print("✓")
                else:
                    enrichment[table_name] = {
                        "entity_type":          "unknown",
                        "column_enrichment":    {},
                        "enum_interpretations": {}
                    }
                    print("✗ (parse failed, stored empty record)")

            except Exception as e:
                print(f"✗  ({e})")
                enrichment[table_name] = {
                    "entity_type":          "unknown",
                    "column_enrichment":    {},
                    "enum_interpretations": {}
                }

            # Incremental save after every table
            with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
                json.dump(enrichment, f, indent=2)

        return enrichment


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def run_enrichment():
    print("=" * 55)
    print("  DATABASE ENRICHMENT AGENT")
    print("=" * 55)

    def load_json(path: str) -> Dict:
        if not os.path.exists(path):
            raise FileNotFoundError(f"Required file not found: {path}")
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)

    tables_structure     = load_json(TABLES_STRUCTURE_FILE)
    relationships_summary = load_json(RELATIONSHIP_FILE)
    understanding        = load_json(UNDERSTANDING_FILE)

    print(f"  Tables   : {len(tables_structure)}")
    print(f"  Understood: {len(understanding)}")

    agent = EnrichmentAgent(provider=SELECTED_PROVIDER)
    enrichment = agent.run(tables_structure, relationships_summary, understanding, dump_file=DUMP_FILE)

    print(f"\n{'='*55}")
    print(f"  DONE — {len(enrichment)} tables enriched")
    print(f"  Output → {OUTPUT_FILE}")
    print(f"{'='*55}\n")

    return enrichment


if __name__ == "__main__":
    try:
        run_enrichment()
    except Exception as e:
        print(f"\nFatal error: {e}")
        import traceback
        traceback.print_exc()