"""
Pattern Validator Agent
Validates and corrects table pattern classifications (SE, SEw, SR, SRR, SE_SH)
by sending each table's structural facts to an LLM for confirmation.

Reads  : memory/patterns.json          -> flat { "table": "pattern" }
Writes : memory/patterns_final.json    -> flat { "table": "pattern" }
"""

import json
import requests
import re
from typing import Dict, List, Tuple
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.llm_config import LLMConfig
from config.llm_config import SELECTED_PROVIDER
# ===== PATHS =====
DB_JSON_FOLDER        = "src2/outputs/DB_as_json"
MEMORY_FOLDER         = "src2/memory"

TABLE_RELATIONSHIPS_FILE  = os.path.join("src2/outputs/DB_as_json", "table_relationships.json")
TABLES_STRUCTURE_FILE     = os.path.join("src2/outputs/DB_as_json", "tables_structure.json")
RELATIONSHIP_SUMMARY_FILE = os.path.join("src2/outputs/DB_as_json", "relationship_summary.json")
PATTERNS_FILE             = os.path.join(MEMORY_FOLDER, "patterns.json")
OUTPUT_FILE               = os.path.join(MEMORY_FOLDER, "patterns_final.json")


class PatternValidatorAgent:
    """Agent that validates and corrects table pattern assignments using an LLM."""

    VALID_PATTERNS = {"SE", "SEw", "SR", "SRR", "SE_SH"}

    def __init__(self, provider: str = SELECTED_PROVIDER):
        self.provider = provider
        self.config = LLMConfig.get_config(provider)
        print(f"Initialized PatternValidatorAgent with provider: {provider}")
        print(f"Model: {self.config['model_name']}")

    def strip_thinking_tags(self, text: str) -> str:
        """Remove <think></think> tags from responses (common in Groq)"""
        cleaned = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
        return cleaned.strip()

    def get_llm_response(self, prompt: str) -> str:
        """Get response from the configured LLM"""
        if self.provider == "claude":
            return self._get_claude_response(prompt)
        elif self.provider == "gemini":
            return self._get_gemini_response(prompt)
        else:
            return self._get_openai_compatible_response(prompt)

    def _get_openai_compatible_response(self, prompt: str) -> str:
        """Handle OpenAI-compatible APIs (GPT, Groq)"""
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config['api_key']}"
        }
        data = {
            "model": self.config['model_name'],
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 512
        }
        response = requests.post(self.config['api_url'], headers=headers, json=data)
        if response.status_code != 200:
            raise Exception(f"API request failed: {response.status_code} - {response.text}")
        content = response.json()["choices"][0]["message"]["content"]
        if self.provider == "groq":
            content = self.strip_thinking_tags(content)
        return content

    def _get_claude_response(self, prompt: str) -> str:
        """Handle Claude API"""
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.config['api_key'],
            "anthropic-version": "2023-06-01"
        }
        data = {
            "model": self.config['model_name'],
            "max_tokens": 512,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1
        }
        response = requests.post(self.config['api_url'], headers=headers, json=data)
        if response.status_code != 200:
            raise Exception(f"Claude API request failed: {response.status_code}")
        return response.json()["content"][0]["text"]

    def _get_gemini_response(self, prompt: str) -> str:
        """Handle Gemini API"""
        url = f"{self.config['api_url']}/{self.config['model_name']}:generateContent?key={self.config['api_key']}"
        headers = {"Content-Type": "application/json"}
        data = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.1, "maxOutputTokens": 512}
        }
        response = requests.post(url, headers=headers, json=data)
        if response.status_code != 200:
            raise Exception(f"Gemini API request failed: {response.status_code}")
        return response.json()["candidates"][0]["content"]["parts"][0]["text"]

    def _build_sh_hints(self, table_name: str, tables_structure: Dict) -> List[Dict]:
        """Derive SH hints on the fly from structure (since patterns.json is now flat)"""
        hints = []
        info = tables_structure.get(table_name, {})
        pk_cols = set(info.get("primary_keys", []))
        for col in info.get("columns", []):
            col_name = col["name"]
            if col_name in pk_cols and col.get("is_foreign_key") and col.get("foreign_key_reference"):
                ref = col["foreign_key_reference"]
                hints.append({
                    "child":          table_name,
                    "parent":         ref["table"],
                    "pk_fk_column":   col_name,
                    "parent_pk_column": ref["column"]
                })
        return hints

    def _build_validation_prompt(
        self,
        table_name: str,
        current_pattern: str,
        structure: Dict,
        relationships: Dict,
        reverse_links: List[str],
        sh_hints: List[Dict],
    ) -> str:
        """Build the validation prompt for a single table"""

        cols    = structure.get("columns", [])
        pk_cols = structure.get("primary_keys", [])
        col_lines = []
        for c in cols:
            pk_marker = " [PK]" if c["name"] in pk_cols else ""
            fk_ref = ""
            if c.get("is_foreign_key") and c.get("foreign_key_reference"):
                ref = c["foreign_key_reference"]
                fk_ref = f"  →FK→ {ref['table']}.{ref['column']}"
            col_lines.append(f"    {c['name']} ({c['data_type']}){pk_marker}{fk_ref}")

        cols_block = "\n".join(col_lines) if col_lines else "    (no column info)"
        total_cols = structure.get("total_columns", len(cols))
        total_pks  = structure.get("total_primary_keys", len(pk_cols))
        total_fks  = structure.get("total_foreign_keys", 0)

        fk_details = relationships.get("foreign_key_details", [])
        fk_lines = [
            f"    {d['via_column']} → {d['links_to']}.{d['references_column']}"
            for d in fk_details
        ] or ["    (none)"]
        fk_block  = "\n".join(fk_lines)
        rev_block = ", ".join(reverse_links) if reverse_links else "(none)"

        sh_lines = [
            f"    {r['child']} inherits from {r['parent']} via column '{r['pk_fk_column']}'"
            for r in sh_hints
        ]
        sh_block = "\n".join(sh_lines) if sh_lines else "    (none)"

        prompt = f"""You are a database schema expert. Classify this database table into exactly one structural pattern.

PATTERN DEFINITIONS:
  SE     - Strong Entity: has its own independent PK (not borrowed from a parent).
             May have FK columns pointing to other tables, but those FKs are NOT part of its PK.
  SE_SH  - Strong Entity + Subclass (inheritance): its sole PK column is ALSO a FK to a parent
             table (shared/borrowed PK). It is still a full SE — it just also inherits from
             a parent entity. Use this when the table extends a parent with extra columns/roles.
  SEw    - Weak Entity: PK is composite and INCLUDES a FK to an owner table.
             Cannot exist independently; identity depends on the owner.
  SR     - Pure Relationship (junction): ALL columns are FKs that together form the PK.
             No meaningful non-key attributes. Simply connects two entity tables.
  SRR    - Reified Relationship: like SR but also carries meaningful non-FK attributes
             (e.g., date, score, status, role) as extra columns.

TABLE: {table_name}
Total columns : {total_cols}  |  Total PKs: {total_pks}  |  Total FKs: {total_fks}

Columns:
{cols_block}

FK relationships (this table → other tables):
{fk_block}

Tables that reference THIS table:
  {rev_block}

Inheritance hints:
{sh_block}

Currently assigned pattern: {current_pattern}

Return ONLY a JSON object, no markdown, no extra text:

{{
  "confirmed_pattern": "<SE | SE_SH | SEw | SR | SRR>",
  "changed": <true if you disagree with the current pattern, else false>,
  "reason": "<one concise sentence>"
}}"""

        return prompt


    def _parse_response(self, response: str, table_name: str):
        """
        Parse the LLM response - tries multiple strategies before giving up:
          1. Extract and parse the first {...} JSON block
          2. Regex-search for confirmed_pattern value anywhere in the text
          3. Scan the whole text for a bare pattern keyword as last resort
        """
        original = response

        # --- Strategy 1: standard JSON extraction ---
        try:
            cleaned = re.sub(r'''```json\s*''', "", response)
            cleaned = re.sub(r'''```\s*''', "", cleaned).strip()
            j_start = cleaned.find("{")
            j_end   = cleaned.rfind("}") + 1
            if j_start != -1 and j_end > 0:
                obj = json.loads(cleaned[j_start:j_end])
                if "confirmed_pattern" in obj:
                    return obj
        except (json.JSONDecodeError, ValueError):
            pass

        # --- Strategy 2: regex for confirmed_pattern key anywhere in text ---
        pattern_match = re.search(
            r'"confirmed_pattern"\s*:\s*"(SE_SH|SE|SEw|SR|SRR)"',
            response
        )
        if pattern_match:
            confirmed = pattern_match.group(1)
            changed_match = re.search(r'"changed"\s*:\s*(true|false)', response)
            reason_match  = re.search(r'"reason"\s*:\s*"([^"]*)"', response)
            print(f"  [INFO] Recovered via regex for {table_name}")
            return {
                "confirmed_pattern": confirmed,
                "changed": changed_match.group(1) == "true" if changed_match else False,
                "reason":  reason_match.group(1) if reason_match else "extracted via regex fallback"
            }

        # --- Strategy 3: bare pattern keyword scan (longest tokens first) ---
        bare_match = re.search(r"\b(SE_SH|SEw|SRR|SR|SE)\b", response)
        if bare_match:
            confirmed = bare_match.group(1)
            print(f"  [INFO] Recovered via bare keyword scan for {table_name}: {confirmed!r}")
            return {
                "confirmed_pattern": confirmed,
                "changed": False,
                "reason": "extracted via bare keyword fallback"
            }

        # All strategies failed
        print(f"  [WARN] All parse strategies failed for {table_name}")
        print(f"  [WARN] Raw response: {original[:400]}")
        return None

    def validate_all(
        self,
        table_relationships: Dict,
        tables_structure: Dict,
        table_patterns: Dict[str, str],   # flat { "table": "pattern" }
    ) -> Tuple[Dict[str, str], list, list]:
        """Validate every table and return corrected flat patterns dict"""

        # Build reverse-link map
        reverse_links: Dict[str, List[str]] = {t: [] for t in tables_structure}
        for src_table, rel in table_relationships.items():
            for linked in rel.get("links_to_tables", []):
                if linked in reverse_links:
                    reverse_links[linked].append(src_table)

        final_patterns: Dict[str, str] = {}
        changes = []
        errors  = []

        total = len(table_patterns)
        print(f"\nValidating {total} tables...\n")

        for idx, (table_name, current_pattern) in enumerate(table_patterns.items(), 1):
            print(f"[{idx:>3}/{total}] {table_name:<40} current={current_pattern}")

            structure     = tables_structure.get(table_name, {})
            relationships = table_relationships.get(table_name, {})
            rev           = reverse_links.get(table_name, [])
            sh_hints      = self._build_sh_hints(table_name, tables_structure)

            prompt = self._build_validation_prompt(
                table_name, current_pattern, structure, relationships, rev, sh_hints
            )

            try:
                raw_response = self.get_llm_response(prompt)
                result = self._parse_response(raw_response, table_name)

                if result is None:
                    print(f"         → parse failed, keeping: {current_pattern}")
                    final_patterns[table_name] = current_pattern
                    errors.append(table_name)
                    continue

                confirmed = result.get("confirmed_pattern", current_pattern)
                changed   = result.get("changed", False)
                reason    = result.get("reason", "")

                if confirmed not in self.VALID_PATTERNS:
                    print(f"         → invalid pattern '{confirmed}', keeping: {current_pattern}")
                    final_patterns[table_name] = current_pattern
                    errors.append(table_name)
                    continue

                final_patterns[table_name] = confirmed

                if changed:
                    print(f"         → CHANGED  {current_pattern} → {confirmed}  | {reason}")
                    changes.append({"table": table_name, "old": current_pattern, "new": confirmed, "reason": reason})
                else:
                    print(f"         → confirmed {confirmed}")

            except Exception as e:
                print(f"         → LLM error: {e}, keeping: {current_pattern}")
                final_patterns[table_name] = current_pattern
                errors.append(table_name)

        return final_patterns, changes, errors


def validate_patterns():
    """Main function to run pattern validation"""

    def load_json(path: str) -> Dict:
        if not os.path.exists(path):
            raise FileNotFoundError(f"Required file not found: {path}")
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    print("=" * 60)
    print("  PATTERN VALIDATOR AGENT")
    print("=" * 60)

    print(f"\nLoading DB files from '{DB_JSON_FOLDER}/' ...")
    table_relationships = load_json(TABLE_RELATIONSHIPS_FILE)
    tables_structure    = load_json(TABLES_STRUCTURE_FILE)
    print(f"  ✓ table_relationships  ({len(table_relationships)} tables)")
    print(f"  ✓ tables_structure     ({len(tables_structure)} tables)")

    print(f"\nLoading patterns from '{PATTERNS_FILE}' ...")
    table_patterns = load_json(PATTERNS_FILE)   # flat { "table": "pattern" }
    print(f"  ✓ {len(table_patterns)} table entries")

    agent = PatternValidatorAgent(provider=SELECTED_PROVIDER)

    final_patterns, changes, errors = agent.validate_all(
        table_relationships,
        tables_structure,
        table_patterns,
    )

    # Save output — flat { "table": "pattern" }
    os.makedirs(MEMORY_FOLDER, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(final_patterns, f, indent=2)

    print("\n" + "=" * 60)
    print("  VALIDATION COMPLETE")
    print("=" * 60)
    print(f"  Total tables           : {len(final_patterns)}")
    print(f"  Patterns changed       : {len(changes)}")
    print(f"  Errors (kept original) : {len(errors)}")
    print(f"\n  Output saved → {OUTPUT_FILE}")

    if changes:
        print("\n  Changes made:")
        for c in changes:
            print(f"    {c['table']:<40} {c['old']} → {c['new']}")
            print(f"      reason: {c['reason']}")

    if errors:
        print(f"\n  Tables with errors (originals kept): {errors}")

    print()


if __name__ == "__main__":
    try:
        validate_patterns()
    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()