"""
Database Understanding Agent
Analyzes ALL database tables in one pass using an LLM.
For each table it produces:
  - a plain-English meaning of the table
  - a plain-English meaning of every column

Reads  : src/outputs/DB_as_json/tables_structure.json
         src/inputs/database/dump.sql   (for sample rows)
Writes : src/memory/understanding.json

Output shape (simple flat JSON):
{
  "table_name": {
    "table_meaning": "...",
    "columns": {
      "col_name": "...",
      ...
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
from typing import Dict, List, Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.llm_config import LLMConfig
from config.llm_config import SELECTED_PROVIDER

# ===== PATHS =====
TABLES_STRUCTURE_FILE = "src/outputs/DB_as_json/tables_structure.json"
DUMP_FILE             = "src/inputs/database/dump.sql"
OUTPUT_DIR            = "src/memory"
OUTPUT_FILE           = os.path.join(OUTPUT_DIR, "understanding.json")


class UnderstandingAgent:
    """Builds semantic definitions for every table and column in the database."""

    def __init__(self, provider: str = SELECTED_PROVIDER):
        self.provider = provider
        self.config   = LLMConfig.get_config(provider)
        print(f"Initialized UnderstandingAgent with provider: {provider}")
        print(f"Model: {self.config['model_name']}")

    # ------------------------------------------------------------------
    # LLM call methods
    # ------------------------------------------------------------------

    def strip_thinking_tags(self, text: str) -> str:
        return re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()

    def _has_json(self, text: str) -> bool:
        """Return True if text contains JSON content after stripping think tags."""
        cleaned = self.strip_thinking_tags(text)
        j_start = cleaned.find('{')
        j_end   = cleaned.rfind('}') + 1
        if j_start == -1 or j_end == 0:
            return False
        try:
            json.loads(cleaned[j_start:j_end])
            return True
        except Exception:
            return '"table_meaning"' in cleaned

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

        # If model only produced thinking with no JSON, do a follow-up
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
    # Sample row extractor (supports COPY and INSERT SQL formats)
    # ------------------------------------------------------------------

    def _get_sample_rows(self, dump_file: str, table_name: str, limit: int = 10) -> List[Dict]:
        """Extract up to `limit` sample rows for a table from a SQL dump."""
        try:
            with open(dump_file, 'r', encoding='utf-8', errors='ignore') as f:
                sql_content = f.read()

            # --- COPY format (PostgreSQL) ---
            copy_match = re.search(
                rf'COPY\s+{re.escape(table_name)}\s+\(([^)]+)\)\s+FROM\s+stdin;(.*?)(?=\\\.)',
                sql_content, re.IGNORECASE | re.DOTALL
            )
            if copy_match:
                col_names  = [c.strip() for c in copy_match.group(1).split(',')]
                data_lines = copy_match.group(2).strip().split('\n')
                rows = []
                for line in data_lines:
                    line = line.strip()
                    if not line or line.startswith('--'):
                        continue
                    values = line.split('\t')
                    row = {col_names[i]: (None if v == r'\N' else v)
                           for i, v in enumerate(values) if i < len(col_names)}
                    rows.append(row)
                    if len(rows) >= limit:
                        break
                if rows:
                    return rows

            # --- INSERT format ---
            insert_match = re.search(
                rf'INSERT INTO\s+{re.escape(table_name)}\s+\(([^)]+)\)\s+VALUES\s+(.*?);',
                sql_content, re.IGNORECASE | re.DOTALL
            )
            if insert_match:
                col_names     = [c.strip() for c in insert_match.group(1).split(',')]
                values_section = insert_match.group(2)
                tuples        = re.findall(r'\(([^)]+)\)', values_section)
                rows = []
                for tpl in tuples[:limit]:
                    raw_vals = [v.strip().strip("'\"") for v in tpl.split(',')]
                    row = {col_names[i]: (None if raw_vals[i].upper() == 'NULL' else raw_vals[i])
                           for i in range(min(len(col_names), len(raw_vals)))}
                    rows.append(row)
                if rows:
                    return rows

        except Exception as e:
            print(f"  [WARN] Could not extract samples for '{table_name}': {e}")

        return []

    # ------------------------------------------------------------------
    # Prompt builder
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        table_name: str,
        columns: List[Dict],
        sample_rows: List[Dict],
    ) -> str:
        col_names  = [c['name'] for c in columns]
        cols_str   = ", ".join(col_names)
        sample_str = json.dumps(sample_rows, indent=2, default=str) if sample_rows else "No sample data available"

        return f"""Analyze this database table and explain what it represents.

TABLE: {table_name}
COLUMNS: {cols_str}

SAMPLE DATA:
{sample_str}

Return ONLY a JSON object, no markdown, no extra text:

{{
  "table_meaning": "One or two sentences describing what this table stores.",
  "columns": {{
    "column_name": "Brief explanation of what this column stores.",
    ...
  }}
}}"""

    # ------------------------------------------------------------------
    # Response parser (with fallback)
    # ------------------------------------------------------------------

    def _parse_response(self, response: str, table_name: str) -> Dict:
        try:
            cleaned = re.sub(r'```json\s*', '', response)
            cleaned = re.sub(r'```\s*', '', cleaned).strip()
            j_start = cleaned.find('{')
            j_end   = cleaned.rfind('}') + 1
            if j_start != -1 and j_end > 0:
                obj = json.loads(cleaned[j_start:j_end])
                if 'table_meaning' in obj:
                    return obj
        except (json.JSONDecodeError, ValueError):
            pass

        print(f"  [WARN] Could not parse response for '{table_name}', storing raw text")
        return {
            "table_meaning": response.strip()[:300],
            "columns": {}
        }

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(
        self,
        tables_structure: Dict,
        dump_file: str = DUMP_FILE,
    ) -> Dict:
        """
        Analyze every table and return the full understanding dict.
        Saves incrementally to OUTPUT_FILE after each table.
        """
        # Load existing progress so re-runs skip completed tables
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        if os.path.exists(OUTPUT_FILE):
            with open(OUTPUT_FILE, 'r', encoding='utf-8') as f:
                understanding = json.load(f)
            print(f"  Loaded existing understanding.json ({len(understanding)} tables already done)")
        else:
            understanding = {}

        tables = list(tables_structure.keys())
        total  = len(tables)
        print(f"\nAnalyzing {total} tables...\n")

        for idx, table_name in enumerate(tables, 1):
            if table_name in understanding:
                print(f"[{idx:>3}/{total}] {table_name:<40} → already done, skipping")
                continue

            print(f"[{idx:>3}/{total}] {table_name:<40} ", end="", flush=True)

            columns     = tables_structure[table_name].get('columns', [])
            sample_rows = self._get_sample_rows(dump_file, table_name, limit=10)
            prompt      = self._build_prompt(table_name, columns, sample_rows)

            try:
                response = self.get_llm_response(prompt)
                result   = self._parse_response(response, table_name)
                understanding[table_name] = {
                    "table_meaning": result.get("table_meaning", ""),
                    "columns":       result.get("columns", {})
                }
                print(f"✓")
            except Exception as e:
                print(f"✗  ({e})")
                understanding[table_name] = {
                    "table_meaning": "error during analysis",
                    "columns": {}
                }

            # Incremental save after every table
            with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
                json.dump(understanding, f, indent=2)

        return understanding


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def run_understanding():
    print("=" * 55)
    print("  DATABASE UNDERSTANDING AGENT")
    print("=" * 55)

    if not os.path.exists(TABLES_STRUCTURE_FILE):
        raise FileNotFoundError(f"Tables structure not found: {TABLES_STRUCTURE_FILE}")

    with open(TABLES_STRUCTURE_FILE, 'r', encoding='utf-8') as f:
        tables_structure = json.load(f)

    print(f"  Tables to analyze: {len(tables_structure)}")

    agent = UnderstandingAgent(provider=SELECTED_PROVIDER)
    understanding = agent.run(tables_structure, dump_file=DUMP_FILE)

    print(f"\n{'='*55}")
    print(f"  DONE — {len(understanding)} tables analyzed")
    print(f"  Output → {OUTPUT_FILE}")
    print(f"{'='*55}\n")

    return understanding


if __name__ == "__main__":
    try:
        run_understanding()
    except Exception as e:
        print(f"\nFatal error: {e}")
        import traceback
        traceback.print_exc()