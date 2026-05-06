from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Optional

try:
    from src.evaluation.generate_rodi_f1_site_refactored import SourceRef, _discover_sources
    from src.evaluation.generate_rodi_paper_table import PAPER_ROWS
except ModuleNotFoundError:  # pragma: no cover - fallback for direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from src.evaluation.generate_rodi_f1_site_refactored import SourceRef, _discover_sources
    from src.evaluation.generate_rodi_paper_table import PAPER_ROWS


TABULAR_LINE_RE = re.compile(r"^(?P<label>[^|]+)\|(?P<f1>[^|]+)\|(?P<precision>[^|]+)\|(?P<recall>[^|]+)$")
PAPER_SOURCE_SPECS: tuple[tuple[str, str, dict[str, float]], ...] = (
    (
        "D2RQ",
        "D2RQ",
        {
            "cmt_renamed": 0.31,
            "conference_renamed": 0.26,
            "sigkdd_renamed": 0.38,
            "cmt_structured": 0.14,
            "conference_structured": 0.21,
            "sigkdd_structured": 0.28,
            "sigkdd_mixed": 0.21,
            "conference_nofks": 0.18,
            "cmt_denormalized": 0.20,
            "mondial_rel": 0.06,
            "npd_atomic_tests": 0.08,
        },
    ),
    (
        "MIRR.",
        "MIRR.",
        {
            "cmt_renamed": 0.28,
            "conference_renamed": 0.27,
            "sigkdd_renamed": 0.30,
            "cmt_structured": 0.17,
            "conference_structured": 0.23,
            "sigkdd_structured": 0.11,
            "sigkdd_mixed": 0.11,
            "conference_nofks": 0.17,
            "cmt_denormalized": 0.22,
            "npd_atomic_tests": 0.00,
        },
    ),
    (
        "ontop",
        "ontop",
        {
            "cmt_renamed": 0.28,
            "conference_renamed": 0.26,
            "sigkdd_renamed": 0.38,
            "cmt_structured": 0.14,
            "conference_structured": 0.13,
            "sigkdd_structured": 0.21,
            "sigkdd_mixed": 0.21,
            "cmt_denormalized": 0.20,
            "npd_atomic_tests": 0.10,
        },
    ),
    (
        "COMA",
        "COMA",
        {
            "cmt_renamed": 0.48,
            "conference_renamed": 0.36,
            "sigkdd_renamed": 0.66,
            "cmt_structured": 0.38,
            "conference_structured": 0.31,
            "sigkdd_structured": 0.41,
            "sigkdd_mixed": 0.28,
            "conference_nofks": 0.21,
            "npd_atomic_tests": 0.02,
        },
    ),
    (
        "IncM.",
        "IncM.",
        {
            "cmt_renamed": 0.45,
            "conference_renamed": 0.53,
            "sigkdd_renamed": 0.76,
            "cmt_structured": 0.44,
            "conference_structured": 0.41,
            "sigkdd_structured": 0.38,
            "sigkdd_mixed": 0.38,
            "conference_nofks": 0.41,
            "cmt_denormalized": 0.40,
            "mondial_rel": 0.08,
            "npd_atomic_tests": 0.12,
        },
    ),
    (
        "B.OX",
        "B.OX",
        {
            "cmt_renamed": 0.76,
            "conference_renamed": 0.51,
            "sigkdd_renamed": 0.86,
            "cmt_structured": 0.41,
            "conference_structured": 0.41,
            "sigkdd_structured": 0.52,
            "sigkdd_mixed": 0.48,
            "conference_nofks": 0.33,
            "cmt_denormalized": 0.44,
            "mondial_rel": 0.13,
            "npd_atomic_tests": 0.14,
        },
    ),
    (
        "LLM4VKG -paper",
        "LLM4VKG -paper",
        {
            "cmt_renamed": 0.86,
            "conference_renamed": 0.92,
            "sigkdd_renamed": 0.93,
            "cmt_structured": 0.55,
            "conference_structured": 0.59,
            "sigkdd_structured": 0.71,
            "sigkdd_mixed": 0.72,
            "conference_nofks": 0.51,
            "cmt_denormalized": 0.52,
            "mondial_rel": 0.18,
            "npd_atomic_tests": 0.19,
        },
    ),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a grouped summary comparison website from RODI/Ontop overall tabular results."
    )
    parser.add_argument(
        "run_path",
        nargs="?",
        type=Path,
        help="Optional batch directory under outputs/<system>/<batch_timestamp> used only to prioritize source ordering.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Directory where the site should be written. Defaults to outputs/summary/summary_table_site",
    )
    parser.add_argument(
        "--discover-root",
        type=Path,
        help="Root outputs directory to scan for selectable result sources. Defaults to the top-level outputs directory.",
    )
    return parser.parse_args()


def _read_overall_metrics(tabular_file: Path) -> dict[str, Any] | None:
    if not tabular_file.exists():
        return None
    for raw_line in tabular_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = TABULAR_LINE_RE.match(line)
        if not match:
            continue
        if match.group("label") != "All (AVG)":
            continue
        f1_raw = match.group("f1")
        precision_raw = match.group("precision")
        recall_raw = match.group("recall")
        try:
            f1 = float(f1_raw)
        except ValueError:
            f1 = None
        try:
            precision = float(precision_raw)
        except ValueError:
            precision = None
        try:
            recall = float(recall_raw)
        except ValueError:
            recall = None
        if f1 is not None and math.isnan(f1):
            f1 = None
        if precision is not None and math.isnan(precision):
            precision = None
        if recall is not None and math.isnan(recall):
            recall = None
        return {
            "label": "All (AVG)",
            "f1": f1,
            "f1_raw": f1_raw,
            "precision": precision,
            "precision_raw": precision_raw,
            "recall": recall,
            "recall_raw": recall_raw,
        }
    return None


def _domain_key(group: str) -> str:
    if group.startswith("Conference domain"):
        return "conference"
    if group == "Geodata":
        return "geodata"
    if group == "Oil & gas domain":
        return "oil-gas"
    return "other"


def _variant_key(dataset_name: Optional[str]) -> str:
    if not dataset_name:
        return "other"
    if dataset_name.endswith("_renamed"):
        return "renamed"
    if dataset_name.endswith("_structured"):
        return "structured"
    if dataset_name.endswith("_mixed"):
        return "mixed"
    if dataset_name.endswith("_nofks"):
        return "nofks"
    if dataset_name.endswith("_denormalized"):
        return "denormalized"
    if dataset_name.endswith("_rel"):
        return "rel"
    if "atomic" in dataset_name:
        return "atomic"
    return "other"


def _paper_metrics(score: float) -> dict[str, Any]:
    return {
        "label": "All (AVG)",
        "f1": score,
        "f1_raw": f"{score:.2f}",
        "precision": None,
        "precision_raw": "-",
        "recall": None,
        "recall_raw": "-",
    }


def _build_payload(run_path: Optional[Path], discover_root: Path) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    row_index_by_dataset: dict[str, int] = {}
    for row_index, row in enumerate(PAPER_ROWS):
        row_id = f"{row.group}::{row.scenario}"
        rows.append(
            {
                "id": row_id,
                "group": row.group,
                "scenario": row.scenario,
                "dataset_name": row.dataset_name,
                "domain": _domain_key(row.group),
                "variant": _variant_key(row.dataset_name),
                "row_index": row_index,
            }
        )
        if row.dataset_name:
            row_index_by_dataset[row.dataset_name] = row_index

    sources: list[dict[str, object]] = []
    source_scores: dict[str, dict[str, dict[str, Any]]] = {}
    for source_index, (source_id, display_name, scores) in enumerate(PAPER_SOURCE_SPECS):
        dataset_names = [dataset_name for dataset_name in scores if dataset_name in row_index_by_dataset]
        sources.append(
            {
                "id": f"paper::{source_id}",
                "system": "Paper systems",
                "timestamp": "paper",
                "method": "paper",
                "default_suffix": "",
                "enabled_by_default": True,
                "dataset_names": dataset_names,
                "source_index": source_index,
                "display_name": display_name,
                "short_label": display_name,
                "builtin": True,
            }
        )
        source_scores[f"paper::{source_id}"] = {
            dataset_name: _paper_metrics(score)
            for dataset_name, score in scores.items()
            if dataset_name in row_index_by_dataset
        }

    offset = len(PAPER_SOURCE_SPECS)
    for source_index, source in enumerate(_discover_sources(run_path, discover_root), start=offset):
        source_id = source.id
        suffix = f"R{source_index + 1}"
        sources.append(
            {
                "id": source_id,
                "system": source.system_name,
                "timestamp": source.timestamp,
                "method": source.method,
                "default_suffix": suffix,
                "enabled_by_default": False,
                "dataset_names": [dataset_dir.name for dataset_dir in source.dataset_dirs],
                "source_index": source_index,
            }
        )
        value_map: dict[str, dict[str, Any]] = {}
        for dataset_dir in source.dataset_dirs:
            if dataset_dir.name not in row_index_by_dataset:
                continue
            metrics = _read_overall_metrics(dataset_dir / "evaluation" / source.method / f"eval_{source.method}__tabular.txt")
            if metrics is not None:
                value_map[dataset_dir.name] = metrics
        source_scores[source_id] = value_map

    return {
        "run_path": str(run_path) if run_path else "",
        "discover_root": str(discover_root),
        "rows": rows,
        "sources": sources,
        "scores": source_scores,
        "groups": sorted({row["group"] for row in rows}),
        "domains": sorted({row["domain"] for row in rows}),
        "variants": sorted({row["variant"] for row in rows}),
        "scenarios": sorted({row["scenario"] for row in rows}),
    }


def _html(payload_json: str) -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Summary Table Site</title>
  <style>
    :root {
      --paper: #f5efe2;
      --ink: #1f1a14;
      --muted: #6c6358;
      --line: #c8baa4;
      --accent: #8a3b12;
      --panel: #fffaf0;
      --best: #dff2d6;
      --nan: #f3d0c5;
      --missing: #f3ecdf;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Georgia, "Times New Roman", serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(201,110,49,0.12), transparent 28%),
        linear-gradient(180deg, #f7f1e7 0%, #f2eadc 100%);
    }
    .page {
      max-width: 1540px;
      margin: 0 auto;
      padding: 28px 22px 40px;
    }
    .hero {
      background: linear-gradient(135deg, rgba(255,250,240,0.92), rgba(245,239,226,0.98));
      border: 1px solid rgba(138,59,18,0.14);
      border-radius: 24px;
      padding: 24px 26px 18px;
      box-shadow: 0 18px 60px rgba(73, 48, 28, 0.08);
      margin-bottom: 22px;
    }
    .eyebrow {
      text-transform: uppercase;
      letter-spacing: 0.12em;
      font-size: 12px;
      color: var(--accent);
      margin-bottom: 10px;
    }
    h1 {
      margin: 0 0 10px;
      font-size: clamp(30px, 4vw, 52px);
      line-height: 0.98;
      font-weight: 700;
    }
    .subtitle {
      margin: 0;
      max-width: 1100px;
      color: var(--muted);
      font-size: 18px;
      line-height: 1.45;
    }
    .panel-title {
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.1em;
      color: var(--muted);
      margin: 0 0 10px;
    }
    .source-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(330px, 1fr));
      gap: 10px;
      margin: 0 0 18px;
    }
    .source-card, .group-panel, .control {
      background: rgba(255, 250, 240, 0.92);
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 12px 14px;
      box-shadow: 0 10px 24px rgba(73, 48, 28, 0.04);
    }
    .source-top {
      display: flex;
      align-items: start;
      justify-content: space-between;
      gap: 12px;
    }
    .source-check {
      display: flex;
      gap: 10px;
      align-items: start;
      flex: 1;
      min-width: 0;
    }
    .source-check input[type="checkbox"] { margin-top: 2px; }
    .source-main { min-width: 0; }
    .source-system {
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--accent);
      margin-bottom: 4px;
    }
    .source-stamp {
      font-size: 14px;
      color: var(--ink);
      word-break: break-all;
    }
    .source-name {
      font-size: 18px;
      color: var(--ink);
      font-weight: 700;
      margin-bottom: 4px;
    }
    .source-method {
      margin-top: 6px;
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
    }
    .source-suffix {
      width: 88px;
      flex: 0 0 auto;
    }
    .source-suffix label {
      display: block;
      font-size: 10px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
      margin-bottom: 4px;
    }
    .source-suffix input {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: #fff8ee;
      padding: 6px 8px;
      font: inherit;
      color: var(--ink);
    }
    .source-meta {
      margin-top: 10px;
      font-size: 11px;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }
      .group-panels {
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
      margin: 0 0 14px;
    }
    .group-panel-title {
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.1em;
      color: var(--muted);
      margin-bottom: 10px;
    }
    .group-buttons {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
    .group-button {
      border: 1px solid var(--line);
      background: #fff8ee;
      color: var(--ink);
      border-radius: 999px;
      padding: 8px 12px;
      font: inherit;
      font-size: 13px;
      cursor: pointer;
      transition: background 120ms ease, border-color 120ms ease, transform 120ms ease, opacity 120ms ease;
    }
    .group-button:hover {
      transform: translateY(-1px);
      border-color: rgba(138,59,18,0.32);
    }
    .group-button.active {
      background: rgba(255, 238, 214, 0.98);
      border-color: rgba(138,59,18,0.4);
    }
    .group-button.inactive {
      opacity: 0.58;
      background: rgba(244, 235, 222, 0.72);
    }
    .controls {
      display: grid;
      grid-template-columns: minmax(220px, 1fr) repeat(4, minmax(150px, 0.45fr));
      gap: 12px;
      margin: 18px 0 20px;
    }
    .control label {
      display: block;
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
      margin-bottom: 6px;
    }
    .control input, .control select {
      width: 100%;
      border: none;
      outline: none;
      background: transparent;
      color: var(--ink);
      font-size: 16px;
      font-family: inherit;
    }
    .table-shell {
      background: rgba(255, 250, 240, 0.94);
      border: 1px solid var(--line);
      border-radius: 24px;
      overflow: hidden;
      box-shadow: 0 18px 60px rgba(73, 48, 28, 0.08);
    }
    .table-wrap {
      overflow: auto;
      max-height: 74vh;
    }
    table {
      border-collapse: separate;
      border-spacing: 0;
      width: max-content;
      min-width: 100%;
    }
    thead th {
      position: sticky;
      top: 0;
      z-index: 3;
      background: #efe5d4;
      color: var(--ink);
      font-size: 13px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      white-space: nowrap;
    }
    th, td {
      border-bottom: 1px solid rgba(200, 186, 164, 0.6);
      border-right: 1px solid rgba(200, 186, 164, 0.45);
      padding: 10px 12px;
      text-align: center;
      white-space: nowrap;
      font-variant-numeric: tabular-nums;
    }
    th:first-child, td:first-child {
      position: sticky;
      left: 0;
      z-index: 2;
      text-align: left;
      background: #faf4e8;
      min-width: 200px;
      max-width: 200px;
      white-space: normal;
    }
    thead th:first-child {
      z-index: 4;
      background: #eadbc4;
    }
    tbody tr.group-row td {
      background: #f1e4d1;
      font-weight: 700;
      text-align: center;
      font-size: 15px;
    }
    td.value {
      min-width: 80px;
      background: #fffaf0;
    }
    td.value.best {
      background: var(--best);
      font-weight: 700;
    }
    td.value.nan {
      background: var(--nan);
      color: #7f1d1d;
      font-weight: 700;
    }
    td.value.missing {
      background: var(--missing);
      color: #a18e79;
    }
    .th-top {
      display: block;
      font-size: 14px;
      color: var(--ink);
    }
    .th-bottom {
      display: block;
      font-size: 10px;
      color: var(--muted);
      letter-spacing: 0.08em;
      margin-top: 4px;
    }
    .th-bottom strong {
      color: var(--ink);
      font-weight: 700;
    }
    .footer-note {
      margin-top: 16px;
      color: var(--muted);
      font-size: 13px;
    }
    .summary-strip {
      margin-top: 16px;
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 10px;
    }
    .summary-card {
      background: rgba(255, 250, 240, 0.94);
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 12px 14px;
    }
    .summary-card-title {
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
      margin-bottom: 8px;
    }
    .summary-card-value {
      font-size: 28px;
      font-weight: 700;
    }
    @media (max-width: 980px) {
      .controls { grid-template-columns: 1fr 1fr; }
      .group-panels { grid-template-columns: 1fr; }
      th:first-child, td:first-child {
        min-width: 160px;
        max-width: 160px;
      }
    }
    @media (max-width: 640px) {
      .page { padding: 18px 12px 24px; }
      .controls { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="page">
    <section class="hero">
      <div class="eyebrow">MPBootLLM · Summary Table</div>
      <h1>Grouped overall scores across runs and methods.</h1>
      <p class="subtitle">
        This view is a webpage version of the paper-style summary table. Rows are grouped by benchmark section,
        and columns represent selected result sources across systems, timestamps, and methods.
      </p>
    </section>

    <section>
      <div class="panel-title">Result selection</div>
      <div class="group-panels">
        <div class="group-panel">
          <div class="group-panel-title">System groups</div>
          <div class="group-buttons" id="system-groups"></div>
        </div>
        <div class="group-panel">
          <div class="group-panel-title">Method groups</div>
          <div class="group-buttons" id="method-groups"></div>
        </div>
        <div class="group-panel">
          <div class="group-panel-title">Group filters</div>
          <div class="group-buttons" id="row-groups"></div>
        </div>
      </div>
      <section class="source-grid" id="source-selection"></section>
    </section>

    <section class="group-panels">
      <div class="group-panel">
        <div class="group-panel-title">Domain filters</div>
        <div class="group-buttons" id="domain-groups"></div>
      </div>
      <div class="group-panel">
        <div class="group-panel-title">Scenario filters</div>
        <div class="group-buttons" id="scenario-groups"></div>
      </div>
      <div class="group-panel">
        <div class="group-panel-title">Variant filters</div>
        <div class="group-buttons" id="variant-groups"></div>
      </div>
    </section>

    <section class="controls">
      <div class="control">
        <label for="search">Search rows</label>
        <input id="search" type="text" placeholder="Try CMT, Atomic, Geodata..." />
      </div>
      <div class="control">
        <label for="visibility">Visibility</label>
        <select id="visibility">
          <option value="filled" selected>Only rows with at least one value</option>
          <option value="all">Show all rows</option>
          <option value="complete">Only rows with no blanks</option>
        </select>
      </div>
      <div class="control">
        <label for="column-order">Column order</label>
        <select id="column-order">
          <option value="manual">Selection order</option>
          <option value="alpha">Alphabetical</option>
          <option value="system">By system</option>
          <option value="method">By method</option>
          <option value="timestamp">Newest first</option>
        </select>
      </div>
      <div class="control">
        <label for="group-order">Group order</label>
        <select id="group-order">
          <option value="paper" selected>Paper order</option>
          <option value="alpha">Alphabetical</option>
          <option value="coverage">Most filled first</option>
        </select>
      </div>
      <div class="control">
        <label for="best-mode">Best highlight</label>
        <select id="best-mode">
          <option value="row" selected>Best per row</option>
          <option value="off">Off</option>
        </select>
      </div>
    </section>

    <section class="table-shell">
      <div class="table-wrap">
        <table id="matrix"></table>
      </div>
    </section>

    <section class="summary-strip">
      <article class="summary-card">
        <div class="summary-card-title">Visible rows</div>
        <div class="summary-card-value" id="summary-rows">0</div>
      </article>
      <article class="summary-card">
        <div class="summary-card-title">Active sources</div>
        <div class="summary-card-value" id="summary-sources">0</div>
      </article>
      <article class="summary-card">
        <div class="summary-card-title">Average visible score</div>
        <div class="summary-card-value" id="summary-avg">-</div>
      </article>
    </section>

    <p class="footer-note" id="footer-note"></p>
  </div>

  <script>
    const DATA = __DATA_PLACEHOLDER__;

    const Format = {
      metric(raw) {
        const num = Number(raw);
        return Number.isFinite(num) ? num.toFixed(2) : String(raw);
      },
      naturalParts(text) {
        return text.split(/(\\d+)/).map(part => (/^\\d+$/.test(part) ? Number(part) : part.toLowerCase()));
      },
      compareNatural(a, b) {
        const aa = this.naturalParts(a);
        const bb = this.naturalParts(b);
        const len = Math.max(aa.length, bb.length);
        for (let i = 0; i < len; i += 1) {
          if (aa[i] === undefined) return -1;
          if (bb[i] === undefined) return 1;
          if (aa[i] === bb[i]) continue;
          return aa[i] < bb[i] ? -1 : 1;
        }
        return 0;
      },
    };

    const App = {
      init(data) {
        this.data = data;
        this.cacheDom();
        this.indexData();
        this.initState();
        this.buildDynamicUi();
        this.bindControls();
        this.render();
      },

      cacheDom() {
        this.dom = {
          sourceSelection: document.getElementById('source-selection'),
          systemGroups: document.getElementById('system-groups'),
          methodGroups: document.getElementById('method-groups'),
          rowGroups: document.getElementById('row-groups'),
          domainGroups: document.getElementById('domain-groups'),
          scenarioGroups: document.getElementById('scenario-groups'),
          variantGroups: document.getElementById('variant-groups'),
          search: document.getElementById('search'),
          visibility: document.getElementById('visibility'),
          columnOrder: document.getElementById('column-order'),
          groupOrder: document.getElementById('group-order'),
          bestMode: document.getElementById('best-mode'),
          matrix: document.getElementById('matrix'),
          footer: document.getElementById('footer-note'),
          summaryRows: document.getElementById('summary-rows'),
          summarySources: document.getElementById('summary-sources'),
          summaryAvg: document.getElementById('summary-avg'),
        };
      },

      indexData() {
        this.sourceById = new Map(this.data.sources.map(source => [source.id, source]));
        this.rowsById = new Map(this.data.rows.map(row => [row.id, row]));
        this.sortedSystems = [...new Set(this.data.sources.map(source => source.system))].sort((a, b) => Format.compareNatural(a, b));
        this.sortedMethods = [...new Set(this.data.sources.map(source => source.method))].sort((a, b) => Format.compareNatural(a, b));
      },

      initState() {
        const defaultSourceIds = this.data.sources.filter(source => source.enabled_by_default !== false).map(source => source.id);
        this.state = {
          activeSources: new Set(defaultSourceIds),
          activeSourceOrder: [...defaultSourceIds],
          sourceSuffix: new Map(this.data.sources.map(source => [source.id, source.default_suffix])),
          activeGroups: new Set(this.data.groups),
          activeDomains: new Set(this.data.domains),
          activeVariants: new Set(this.data.variants),
          activeScenarios: new Set(this.data.scenarios),
        };
        this.view = {
          sourceCards: new Map(),
          systemButtons: new Map(),
          methodButtons: new Map(),
          groupButtons: new Map(),
          domainButtons: new Map(),
          scenarioButtons: new Map(),
          variantButtons: new Map(),
        };
      },

      displayLabel(source) {
        if (source.display_name) {
          const suffix = this.state.sourceSuffix.get(source.id) || source.default_suffix;
          return suffix ? `${source.display_name} ${suffix}` : source.display_name;
        }
        return `${source.system} · ${source.method} · ${this.state.sourceSuffix.get(source.id) || source.default_suffix}`;
      },

      shortLabel(source) {
        if (source.short_label) {
          const suffix = this.state.sourceSuffix.get(source.id) || source.default_suffix;
          return suffix ? `${source.short_label} ${suffix}` : source.short_label;
        }
        return `${this.state.sourceSuffix.get(source.id) || source.default_suffix} · ${source.method}`;
      },

      activateSource(sourceId) {
        if (!this.state.activeSources.has(sourceId)) {
          this.state.activeSources.add(sourceId);
        }
        this.state.activeSourceOrder = this.state.activeSourceOrder.filter(id => id !== sourceId);
        this.state.activeSourceOrder.push(sourceId);
      },

      deactivateSource(sourceId) {
        this.state.activeSources.delete(sourceId);
        this.state.activeSourceOrder = this.state.activeSourceOrder.filter(id => id !== sourceId);
      },

      toggleSetValue(setRef, value) {
        if (setRef.has(value)) setRef.delete(value);
        else setRef.add(value);
      },

      toggleGroup(setRef, values) {
        const anyActive = values.some(value => setRef.has(value));
        values.forEach(value => {
          if (anyActive) setRef.delete(value);
          else setRef.add(value);
        });
      },

      toggleActiveSourceGroup(sourceIds) {
        const anyActive = sourceIds.some(sourceId => this.state.activeSources.has(sourceId));
        sourceIds.forEach(sourceId => {
          if (anyActive) this.deactivateSource(sourceId);
          else this.activateSource(sourceId);
        });
      },

      sortedSources() {
        const active = this.data.sources.filter(source => this.state.activeSources.has(source.id));
        const order = this.dom.columnOrder.value;
        if (order === 'manual') {
          return this.state.activeSourceOrder.map(id => this.sourceById.get(id)).filter(Boolean);
        }
        if (order === 'alpha') {
          return [...active].sort((a, b) => Format.compareNatural(this.displayLabel(a), this.displayLabel(b)));
        }
        if (order === 'system') {
          return [...active].sort((a, b) => Format.compareNatural(a.system, b.system) || Format.compareNatural(this.displayLabel(a), this.displayLabel(b)));
        }
        if (order === 'method') {
          return [...active].sort((a, b) => Format.compareNatural(a.method, b.method) || Format.compareNatural(this.displayLabel(a), this.displayLabel(b)));
        }
        return [...active].sort((a, b) => Format.compareNatural(b.timestamp, a.timestamp) || Format.compareNatural(this.displayLabel(a), this.displayLabel(b)));
      },

      rowValue(row, source) {
        if (!row.dataset_name) return null;
        return this.data.scores[source.id]?.[row.dataset_name] || null;
      },

      rowHasNumeric(row, sources) {
        return sources.some(source => {
          const value = this.rowValue(row, source);
          return value && typeof value.f1 === 'number';
        });
      },

      rowCoverage(row, sources) {
        return sources.filter(source => {
          const value = this.rowValue(row, source);
          return value && typeof value.f1 === 'number';
        }).length;
      },

      visibleRows(sources) {
        const q = this.dom.search.value.trim().toLowerCase();
        const visibility = this.dom.visibility.value;
        return this.data.rows.filter(row => {
          if (!this.state.activeGroups.has(row.group)) return false;
          if (!this.state.activeDomains.has(row.domain)) return false;
          if (!this.state.activeVariants.has(row.variant)) return false;
          if (!this.state.activeScenarios.has(row.scenario)) return false;
          if (q && !(row.group.toLowerCase().includes(q) || row.scenario.toLowerCase().includes(q))) return false;
          const coverage = this.rowCoverage(row, sources);
          if (visibility === 'filled' && coverage === 0) return false;
          if (visibility === 'complete' && coverage !== sources.length) return false;
          return true;
        });
      },

      orderedGroups(rows, sources) {
        const groups = [...new Set(rows.map(row => row.group))];
        const mode = this.dom.groupOrder.value;
        if (mode === 'alpha') {
          return groups.sort((a, b) => Format.compareNatural(a, b));
        }
        if (mode === 'coverage') {
          return groups.sort((a, b) => {
            const ca = rows.filter(row => row.group === a).reduce((sum, row) => sum + this.rowCoverage(row, sources), 0);
            const cb = rows.filter(row => row.group === b).reduce((sum, row) => sum + this.rowCoverage(row, sources), 0);
            return cb - ca || Format.compareNatural(a, b);
          });
        }
        return this.data.groups.filter(group => groups.includes(group));
      },

      bestIndices(values) {
        const nums = values
          .map((value, index) => ({ index, value }))
          .filter(item => typeof item.value?.f1 === 'number');
        if (!nums.length || this.dom.bestMode.value === 'off') return new Set();
        const best = Math.max(...nums.map(item => item.value.f1));
        return new Set(nums.filter(item => Math.abs(item.value.f1 - best) < 1e-12).map(item => item.index));
      },

      buildDynamicUi() {
        this.data.sources.forEach(source => {
          const card = document.createElement('article');
          card.className = 'source-card';
          card.innerHTML = `
            <div class="source-top">
              <label class="source-check">
                <input type="checkbox" ${this.state.activeSources.has(source.id) ? 'checked' : ''} />
                <div class="source-main">
                  <div class="source-name">${source.display_name || this.displayLabel(source)}</div>
                  <div class="source-system">${source.system}</div>
                  <div class="source-stamp">${source.timestamp}</div>
                  <div class="source-method">${source.method}</div>
                </div>
              </label>
              <div class="source-suffix">
                <label>Suffix</label>
                <input type="text" value="${source.default_suffix}" />
              </div>
            </div>
            <div class="source-meta">${source.dataset_names.length} datasets</div>
          `;
          const checkbox = card.querySelector('input[type="checkbox"]');
          const suffixInput = card.querySelector('.source-suffix input');
          checkbox.addEventListener('input', () => {
            if (checkbox.checked) this.activateSource(source.id);
            else this.deactivateSource(source.id);
            this.render();
          });
          suffixInput.addEventListener('input', () => {
            this.state.sourceSuffix.set(source.id, suffixInput.value.trim() || source.default_suffix);
            this.render();
          });
          this.view.sourceCards.set(source.id, { card, checkbox, suffixInput });
          this.dom.sourceSelection.appendChild(card);
        });

        this.sortedSystems.forEach(system => {
          const button = document.createElement('button');
          button.type = 'button';
          button.className = 'group-button';
          button.textContent = system;
          button.addEventListener('click', () => {
            this.toggleActiveSourceGroup(this.data.sources.filter(source => source.system === system).map(source => source.id));
            this.render();
          });
          this.view.systemButtons.set(system, button);
          this.dom.systemGroups.appendChild(button);
        });

        this.sortedMethods.forEach(method => {
          const button = document.createElement('button');
          button.type = 'button';
          button.className = 'group-button';
          button.textContent = method;
          button.addEventListener('click', () => {
            this.toggleActiveSourceGroup(this.data.sources.filter(source => source.method === method).map(source => source.id));
            this.render();
          });
          this.view.methodButtons.set(method, button);
          this.dom.methodGroups.appendChild(button);
        });

        this.data.groups.forEach(group => {
          const button = document.createElement('button');
          button.type = 'button';
          button.className = 'group-button';
          button.textContent = group;
          button.addEventListener('click', () => {
            this.toggleSetValue(this.state.activeGroups, group);
            this.render();
          });
          this.view.groupButtons.set(group, button);
          this.dom.rowGroups.appendChild(button);
        });

        this.data.domains.forEach(domain => {
          const button = document.createElement('button');
          button.type = 'button';
          button.className = 'group-button';
          button.textContent = domain;
          button.addEventListener('click', () => {
            this.toggleSetValue(this.state.activeDomains, domain);
            this.render();
          });
          this.view.domainButtons.set(domain, button);
          this.dom.domainGroups.appendChild(button);
        });

        this.data.scenarios.forEach(scenario => {
          const button = document.createElement('button');
          button.type = 'button';
          button.className = 'group-button';
          button.textContent = scenario;
          button.addEventListener('click', () => {
            this.toggleSetValue(this.state.activeScenarios, scenario);
            this.render();
          });
          this.view.scenarioButtons.set(scenario, button);
          this.dom.scenarioGroups.appendChild(button);
        });

        this.data.variants.forEach(variant => {
          const button = document.createElement('button');
          button.type = 'button';
          button.className = 'group-button';
          button.textContent = variant;
          button.addEventListener('click', () => {
            this.toggleSetValue(this.state.activeVariants, variant);
            this.render();
          });
          this.view.variantButtons.set(variant, button);
          this.dom.variantGroups.appendChild(button);
        });
      },

      bindControls() {
        [this.dom.search, this.dom.visibility, this.dom.columnOrder, this.dom.groupOrder, this.dom.bestMode]
          .forEach(el => el.addEventListener('input', () => this.render()));
      },

      renderState() {
        this.view.sourceCards.forEach(({ card, checkbox, suffixInput }, sourceId) => {
          const source = this.sourceById.get(sourceId);
          const active = this.state.activeSources.has(sourceId);
          checkbox.checked = active;
          if (suffixInput !== document.activeElement) {
            suffixInput.value = this.state.sourceSuffix.get(sourceId) || source?.default_suffix || '';
          }
          card.style.opacity = active ? '1' : '0.62';
        });
        const reflect = (buttonMap, activeSet) => {
          buttonMap.forEach((button, value) => {
            const active = activeSet.has(value);
            button.classList.toggle('active', active);
            button.classList.toggle('inactive', !active);
          });
        };
        reflect(this.view.groupButtons, this.state.activeGroups);
        reflect(this.view.domainButtons, this.state.activeDomains);
        reflect(this.view.scenarioButtons, this.state.activeScenarios);
        reflect(this.view.variantButtons, this.state.activeVariants);
        this.view.systemButtons.forEach((button, system) => {
          const ids = this.data.sources.filter(source => source.system === system).map(source => source.id);
          const allActive = ids.every(id => this.state.activeSources.has(id));
          const anyActive = ids.some(id => this.state.activeSources.has(id));
          button.classList.toggle('active', allActive);
          button.classList.toggle('inactive', !anyActive);
        });
        this.view.methodButtons.forEach((button, method) => {
          const ids = this.data.sources.filter(source => source.method === method).map(source => source.id);
          const allActive = ids.every(id => this.state.activeSources.has(id));
          const anyActive = ids.some(id => this.state.activeSources.has(id));
          button.classList.toggle('active', allActive);
          button.classList.toggle('inactive', !anyActive);
        });
      },

      renderTable(sources, rows) {
        const groups = this.orderedGroups(rows, sources);
        const table = document.createElement('table');
        const thead = document.createElement('thead');
        const hr = document.createElement('tr');
        const thScenario = document.createElement('th');
        thScenario.textContent = 'Scenario';
        hr.appendChild(thScenario);
        sources.forEach(source => {
          const th = document.createElement('th');
          th.title = this.displayLabel(source);
          th.innerHTML = `<span class="th-top">${this.shortLabel(source)}</span><span class="th-bottom">${source.system}</span>`;
          hr.appendChild(th);
        });
        thead.appendChild(hr);
        table.appendChild(thead);

        const tbody = document.createElement('tbody');
        groups.forEach(group => {
          const inGroup = rows.filter(row => row.group === group);
          if (!inGroup.length) return;
          const groupRow = document.createElement('tr');
          groupRow.className = 'group-row';
          const td = document.createElement('td');
          td.colSpan = sources.length + 1;
          td.textContent = group;
          groupRow.appendChild(td);
          tbody.appendChild(groupRow);

          inGroup.forEach(row => {
            const tr = document.createElement('tr');
            const tdLabel = document.createElement('td');
            tdLabel.textContent = row.scenario;
            tr.appendChild(tdLabel);

            const values = sources.map(source => this.rowValue(row, source));
            const best = this.bestIndices(values);
            values.forEach((value, index) => {
              const tdValue = document.createElement('td');
              tdValue.className = 'value';
              if (!value) {
                tdValue.textContent = '-';
                tdValue.classList.add('missing');
                tdValue.title = `${row.group} · ${row.scenario}\n${this.displayLabel(sources[index])}\nNo result`;
              } else if (typeof value.f1 === 'number') {
                tdValue.textContent = value.f1.toFixed(2);
                if (best.has(index)) tdValue.classList.add('best');
                tdValue.title = `${row.group} · ${row.scenario}\n${this.displayLabel(sources[index])}\nF1 ${Format.metric(value.f1)} · P ${Format.metric(value.precision_raw)} · R ${Format.metric(value.recall_raw)}`;
              } else {
                tdValue.textContent = value.f1_raw;
                tdValue.classList.add('nan');
                tdValue.title = `${row.group} · ${row.scenario}\n${this.displayLabel(sources[index])}\nF1 ${Format.metric(value.f1_raw)} · P ${Format.metric(value.precision_raw)} · R ${Format.metric(value.recall_raw)}`;
              }
              tr.appendChild(tdValue);
            });
            tbody.appendChild(tr);
          });
        });
        table.appendChild(tbody);
        this.dom.matrix.replaceChildren(table);
      },

      renderSummary(sources, rows) {
        const numeric = [];
        rows.forEach(row => {
          sources.forEach(source => {
            const value = this.rowValue(row, source);
            if (value && typeof value.f1 === 'number') numeric.push(value.f1);
          });
        });
        this.dom.summaryRows.textContent = String(rows.length);
        this.dom.summarySources.textContent = String(sources.length);
        this.dom.summaryAvg.textContent = numeric.length ? (numeric.reduce((a, b) => a + b, 0) / numeric.length).toFixed(2) : '-';
      },

      render() {
        const sources = this.sortedSources();
        const rows = this.visibleRows(sources);
        this.renderState();
        this.renderTable(sources, rows);
        this.renderSummary(sources, rows);
        const scopeLabel = this.data.run_path ? `anchor ${this.data.run_path}` : `discover root ${this.data.discover_root}`;
        this.dom.footer.textContent = `${rows.length} rows shown · ${sources.length}/${this.data.sources.length} sources active · ${scopeLabel}`;
      },
    };

    try {
      App.init(DATA);
    } catch (err) {
      document.body.innerHTML = `<pre style="padding:24px;color:#8a3b12">${String(err)}</pre>`;
    }
  </script>
</body>
</html>
""".replace("__DATA_PLACEHOLDER__", payload_json)


def main() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[2]
    run_path = args.run_path.resolve() if args.run_path else None
    if run_path is not None and (not run_path.exists() or not run_path.is_dir()):
        raise SystemExit(f"Run path not found or not a directory: {run_path}")

    discover_root = (args.discover_root or (run_path.parents[1] if run_path else (repo_root / "outputs"))).resolve()
    default_output_dir = discover_root / "summary" / "summary_table_site"
    output_dir = (args.output_dir or default_output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    payload = _build_payload(run_path, discover_root)
    payload_json = json.dumps(payload, indent=2)
    (output_dir / "data.json").write_text(payload_json, encoding="utf-8")
    (output_dir / "index.html").write_text(_html(payload_json), encoding="utf-8")

    print(f"Wrote {output_dir / 'data.json'}")
    print(f"Wrote {output_dir / 'index.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
