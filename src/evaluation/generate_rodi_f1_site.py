from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path


TABULAR_LINE_RE = re.compile(r"^(?P<label>[^|]+)\|(?P<f1>[^|]+)\|(?P<precision>[^|]+)\|(?P<recall>[^|]+)$")

DATASET_LABELS = {
    "cmt_denormalized": "CMT-D",
    "cmt_renamed": "CMT-R",
    "cmt_structured": "CMT-S",
    "conference_nofks": "CONF-NF",
    "conference_renamed": "CONF-R",
    "conference_structured": "CONF-S",
    "mondial_rel": "MOND",
    "npd_atomic_tests": "NPD-A",
    "sigkdd_mixed": "SIG-M",
    "sigkdd_renamed": "SIG-R",
    "sigkdd_structured": "SIG-S",
}

DEFAULT_DISABLED_DATASETS = {"mondial_rel", "npd_atomic_tests"}
TYPE_ORDER = {
    "renamed": 0,
    "structured": 1,
    "mixed": 2,
    "nofks": 3,
    "denormalized": 4,
    "rel": 5,
    "atomic": 6,
    "other": 99,
}

FAMILY_ORDER = {
    "cmt": 0,
    "conference": 1,
    "sigkdd": 2,
    "mondial": 3,
    "npd": 4,
    "other": 99,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a static website that displays RODI F1 tabular results across datasets."
    )
    parser.add_argument(
        "run_path",
        type=Path,
        help="Batch directory under outputs/<model>/<batch_timestamp>",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Directory where the site should be written. Defaults to <run_path>/summary/rodi_f1_site",
    )
    return parser.parse_args()


def _natural_key(text: str) -> list[object]:
    parts = re.split(r"(\d+)", text)
    key: list[object] = []
    for part in parts:
        if part.isdigit():
            key.append(int(part))
        else:
            key.append(part.lower())
    return key


def _read_tabular(tabular_file: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line in tabular_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        match = TABULAR_LINE_RE.match(line)
        if not match:
            continue
        label = match.group("label")
        f1_raw = match.group("f1")
        try:
            f1_value = float(f1_raw)
        except ValueError:
            f1_value = None
        if f1_value is not None and math.isnan(f1_value):
            f1_value = None
        rows.append(
            {
                "label": label,
                "f1_raw": f1_raw,
                "f1": f1_value,
                "precision_raw": match.group("precision"),
                "recall_raw": match.group("recall"),
            }
        )
    return rows


def _read_query_manifest(dataset_dir: Path) -> dict[str, dict[str, object]]:
    manifest_file = dataset_dir / "evaluation" / "queries" / "queries__manifest.json"
    if not manifest_file.exists():
        return {}
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}

    lookup: dict[str, dict[str, object]] = {}
    for item in manifest:
        if not isinstance(item, dict):
            continue
        sql_query = item.get("sql_query")
        sparql_query = item.get("sparql_query")
        entry = {
            "id": item.get("id"),
            "label": item.get("label"),
            "categories": item.get("categories"),
            "sql_query": _compact_query(sql_query) if isinstance(sql_query, str) else sql_query,
            "sparql_query": _compact_query(sparql_query) if isinstance(sparql_query, str) else sparql_query,
        }
        label = item.get("label")
        if isinstance(label, str):
            lookup[label] = entry
        query_id = item.get("id")
        if isinstance(query_id, str) and query_id not in lookup:
            lookup[query_id] = entry
    return lookup


def _compact_query(text: str) -> str:
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines)


def _classify_label(label: str) -> str:
    if label == "All (AVG)":
        return "overall"
    if label.endswith("(AVG)"):
        return "aggregate"
    return "query"


def _row_order_key(label: str, first_seen_order: dict[str, int]) -> tuple[int, object]:
    kind = _classify_label(label)
    if kind == "overall":
        bucket = 0
    elif kind == "query":
        bucket = 1
    else:
        bucket = 2
    return (bucket, first_seen_order[label], _natural_key(label))


def _dataset_variant(name: str) -> str:
    if name.endswith("_renamed"):
        return "renamed"
    if name.endswith("_structured"):
        return "structured"
    if name.endswith("_mixed"):
        return "mixed"
    if name.endswith("_nofks"):
        return "nofks"
    if name.endswith("_denormalized"):
        return "denormalized"
    if name.endswith("_rel"):
        return "rel"
    if "atomic" in name:
        return "atomic"
    return "other"


def _dataset_family(name: str) -> str:
    if name.startswith("cmt_"):
        return "cmt"
    if name.startswith("conference_"):
        return "conference"
    if name.startswith("sigkdd_"):
        return "sigkdd"
    if name.startswith("mondial"):
        return "mondial"
    if name.startswith("npd_"):
        return "npd"
    return "other"


def _build_payload(run_path: Path) -> dict[str, object]:
    dataset_dirs = sorted(
        path
        for path in run_path.iterdir()
        if path.is_dir() and (path / "run_metadata.json").exists() and (path / "mappings_r2rml.ttl").exists()
    )
    datasets: list[dict[str, object]] = []
    first_seen_order: dict[str, int] = {}
    seen_counter = 0
    row_groups: dict[str, set[str]] = {}

    for dataset_index, dataset_dir in enumerate(dataset_dirs):
        tabular_file = dataset_dir / "evaluation" / "rodi" / "eval_rodi__tabular.txt"
        query_lookup = _read_query_manifest(dataset_dir)
        if not tabular_file.exists():
            datasets.append(
                {
                    "name": dataset_dir.name,
                    "short_name": DATASET_LABELS.get(dataset_dir.name, dataset_dir.name),
                    "family": _dataset_family(dataset_dir.name),
                    "variant": _dataset_variant(dataset_dir.name),
                    "manual_index": dataset_index,
                    "enabled_by_default": dataset_dir.name not in DEFAULT_DISABLED_DATASETS,
                    "has_tabular": False,
                    "overall_f1": None,
                    "rows": {},
                }
            )
            continue

        row_entries = _read_tabular(tabular_file)
        row_map: dict[str, dict[str, object]] = {}
        overall_f1 = None
        for row in row_entries:
            label = row["label"]
            assert isinstance(label, str)
            query_info = query_lookup.get(label)
            if query_info:
                cats = query_info.get("categories")
                if isinstance(cats, list):
                    row_groups.setdefault(label, set()).update(str(cat) for cat in cats)
                row.update(
                    {
                        "query_id": query_info.get("id"),
                        "categories": query_info.get("categories"),
                        "sql_query": query_info.get("sql_query"),
                        "sparql_query": query_info.get("sparql_query"),
                    }
                )
            row_map[label] = row
            if label not in first_seen_order:
                first_seen_order[label] = seen_counter
                seen_counter += 1
            if label == "All (AVG)":
                overall_f1 = row["f1"]

        datasets.append(
            {
                "name": dataset_dir.name,
                "short_name": DATASET_LABELS.get(dataset_dir.name, dataset_dir.name),
                "family": _dataset_family(dataset_dir.name),
                "variant": _dataset_variant(dataset_dir.name),
                "manual_index": dataset_index,
                "enabled_by_default": dataset_dir.name not in DEFAULT_DISABLED_DATASETS,
                "has_tabular": True,
                "overall_f1": overall_f1,
                "rows": row_map,
            }
        )

    all_labels = sorted(first_seen_order.keys(), key=lambda label: _row_order_key(label, first_seen_order))
    query_groups: list[str] = []
    seen_groups: set[str] = set()
    row_defs = []
    for label in all_labels:
        kind = _classify_label(label)
        groups = set(row_groups.get(label, set()))
        if kind == "aggregate":
            base = label.removesuffix(" (AVG)")
            groups.add(base)
        normalized_groups = sorted(groups, key=_natural_key)
        for group in normalized_groups:
            if group not in seen_groups:
                seen_groups.add(group)
                query_groups.append(group)
        row_defs.append({"label": label, "kind": kind, "groups": normalized_groups})

    return {
        "run_path": str(run_path),
        "row_defs": row_defs,
        "query_groups": query_groups,
        "datasets": datasets,
    }


def _html(payload_json: str) -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>RODI F1 Matrix</title>
  <style>
    :root {
      --paper: #f5efe2;
      --ink: #1f1a14;
      --muted: #6c6358;
      --line: #c8baa4;
      --accent: #8a3b12;
      --panel: #fffaf0;
      --heat-0: #9d3d12;
      --heat-1: #c96e31;
      --heat-2: #dca06c;
      --heat-3: #e9c9a6;
      --heat-4: #f3e4d4;
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
      max-width: 1460px;
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
      max-width: 1000px;
      color: var(--muted);
      font-size: 18px;
      line-height: 1.45;
    }

    .controls {
      display: grid;
      grid-template-columns: minmax(240px, 1.2fr) repeat(3, minmax(140px, 0.45fr));
      gap: 12px;
      margin: 18px 0 20px;
    }

    .control {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 12px 14px;
      box-shadow: 0 10px 28px rgba(73, 48, 28, 0.06);
    }

    .control label {
      display: block;
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
      margin-bottom: 6px;
    }

    .control input,
    .control select {
      width: 100%;
      border: none;
      outline: none;
      background: transparent;
      color: var(--ink);
      font-size: 16px;
      font-family: inherit;
    }

    .legend-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 8px;
      margin: 0 0 18px;
    }

    .panel-title {
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.1em;
      color: var(--muted);
      margin: 0 0 10px;
    }

    .group-panels {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
      margin: 0 0 14px;
    }

    .group-panel {
      background: rgba(255, 250, 240, 0.9);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 12px;
      box-shadow: 0 10px 24px rgba(73, 48, 28, 0.04);
    }

    .group-panel-title {
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.1em;
      color: var(--muted);
      margin-bottom: 10px;
    }

    .query-panel {
      background: rgba(255, 250, 240, 0.9);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 12px;
      box-shadow: 0 10px 24px rgba(73, 48, 28, 0.04);
      margin: 0 0 18px;
    }

    .group-buttons {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }

    .group-actions {
      display: flex;
      gap: 8px;
      margin-bottom: 10px;
      flex-wrap: wrap;
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

    .legend-chip {
      background: rgba(255, 250, 240, 0.9);
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 10px 12px;
      box-shadow: 0 10px 24px rgba(73, 48, 28, 0.04);
      cursor: pointer;
      transition: transform 120ms ease, border-color 120ms ease, background 120ms ease, opacity 120ms ease;
      user-select: none;
    }

    .legend-chip:hover {
      transform: translateY(-1px);
      border-color: rgba(138,59,18,0.32);
    }

    .legend-chip.active {
      background: rgba(255, 245, 230, 0.98);
      border-color: rgba(138,59,18,0.4);
      box-shadow: 0 14px 26px rgba(73, 48, 28, 0.06);
    }

    .legend-chip.inactive {
      opacity: 0.58;
      background: rgba(244, 235, 222, 0.72);
    }

    .legend-chip .short {
      font-size: 13px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--accent);
      margin-bottom: 4px;
    }

    .legend-chip .full {
      font-size: 13px;
      color: var(--ink);
      word-break: break-word;
    }

    .legend-chip .meta {
      margin-top: 6px;
      font-size: 11px;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.08em;
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
      max-height: 72vh;
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
    }

    .th-inner {
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }

    .sort-button {
      border: none;
      background: transparent;
      color: var(--muted);
      cursor: pointer;
      padding: 0;
      font: inherit;
      font-size: 12px;
      line-height: 1;
    }

    .sort-button:hover {
      color: var(--accent);
    }

    .sort-button.active {
      color: var(--accent);
    }

    thead th.dataset-col {
      cursor: grab;
    }

    thead th.dataset-col:active {
      cursor: grabbing;
    }

    thead th.dataset-col.dragging {
      opacity: 0.55;
    }

    thead th.dataset-col.drag-over {
      box-shadow: inset 0 -3px 0 var(--accent);
      background: #e8d8c0;
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
      min-width: 230px;
      max-width: 230px;
      white-space: normal;
    }

    th:nth-child(2), td:nth-child(2) {
      position: sticky;
      left: 230px;
      z-index: 2;
      background: #f6ecdd;
      min-width: 72px;
      max-width: 72px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      font-size: 11px;
      color: var(--muted);
    }

    thead th:first-child {
      z-index: 4;
      background: #eadbc4;
    }

    thead th:nth-child(2) {
      z-index: 4;
      background: #e6d6bd;
    }

    tbody tr.section td {
      background: #f1e4d1;
      font-style: italic;
      color: var(--muted);
    }

    td.heat {
      font-weight: 700;
      text-shadow: 0 1px 0 rgba(0,0,0,0.12);
      min-width: 64px;
    }

    td.blank {
      background: #f7f0e4;
      color: #a18e79;
      min-width: 64px;
    }

    td.nan {
      background: repeating-linear-gradient(
        -45deg,
        #a7461a,
        #a7461a 10px,
        #c96e31 10px,
        #c96e31 20px
      );
      color: #fff7ea;
      font-weight: 700;
      min-width: 64px;
    }

    .legend {
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 14px 16px 0;
      color: var(--muted);
      font-size: 13px;
    }

    .gradient {
      width: 220px;
      height: 14px;
      border-radius: 999px;
      background: linear-gradient(90deg, var(--heat-0), var(--heat-1), var(--heat-2), var(--heat-3), var(--heat-4));
      border: 1px solid rgba(0,0,0,0.08);
    }

    .footer-note {
      margin-top: 16px;
      color: var(--muted);
      font-size: 13px;
    }

    .tooltip {
      position: fixed;
      z-index: 50;
      width: min(640px, calc(100vw - 24px));
      background: rgba(31, 26, 20, 0.96);
      color: #fff8ee;
      border: 1px solid rgba(255,255,255,0.08);
      border-radius: 16px;
      padding: 14px 16px;
      box-shadow: 0 24px 60px rgba(0,0,0,0.25);
      pointer-events: none;
      opacity: 0;
      transform: translateY(6px);
      transition: opacity 120ms ease, transform 120ms ease;
    }

    .tooltip.visible {
      opacity: 1;
      transform: translateY(0);
    }

    .tooltip .title {
      font-size: 14px;
      font-weight: 700;
      margin-bottom: 6px;
      color: #ffe1b8;
    }

    .tooltip .metrics {
      font-size: 13px;
      margin-bottom: 10px;
      color: #f7ead7;
    }

    .tooltip .section {
      margin-top: 8px;
    }

    .tooltip .section-label {
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: #dcb38a;
      margin-bottom: 4px;
    }

    .tooltip pre {
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
      font-size: 12px;
      line-height: 1.45;
      color: #fff8ee;
    }

    @media (max-width: 980px) {
      .controls {
        grid-template-columns: 1fr 1fr;
      }
      .group-panels {
        grid-template-columns: 1fr;
      }
      th:first-child, td:first-child {
        min-width: 180px;
        max-width: 180px;
      }
      th:nth-child(2), td:nth-child(2) {
        left: 180px;
      }
    }

    @media (max-width: 640px) {
      .page { padding: 18px 12px 24px; }
      .controls { grid-template-columns: 1fr; }
      .card .score { font-size: 26px; }
    }
  </style>
</head>
<body>
  <div class="page">
    <section class="hero">
      <div class="eyebrow">MPBootLLM · RODI · F1 Matrix</div>
      <h1>All datasets, one tabular surface.</h1>
      <p class="subtitle">
        This view takes the first score from each <code>eval_rodi__tabular.txt</code> line, i.e. the F1 value,
        and lays every dataset side by side. Query rows and aggregate rows are kept together so you can compare
        where each mapping run is strong, weak, or simply missing.
      </p>
    </section>

    <section>
      <div class="panel-title">Dataset selection</div>
      <section class="legend-grid" id="dataset-legend"></section>
    </section>

    <section class="group-panels">
      <div class="group-panel">
        <div class="group-panel-title">Dataset groups</div>
        <div class="group-buttons" id="family-groups"></div>
      </div>
      <div class="group-panel">
        <div class="group-panel-title">Dataset Type Groups</div>
        <div class="group-buttons" id="type-groups"></div>
      </div>
    </section>

    <section class="query-panel">
      <div class="group-panel-title">Query groups</div>
      <div class="group-actions">
        <button type="button" class="group-button" id="query-groups-all">Keep all</button>
        <button type="button" class="group-button" id="query-groups-none">Delete all</button>
      </div>
      <div class="group-buttons" id="query-groups"></div>
    </section>

    <section class="controls">
      <div class="control">
        <label for="search">Search rows</label>
        <input id="search" type="text" placeholder="Try Q20, All (AVG), path-1, capital..." />
      </div>
      <div class="control">
        <label for="row-kind">Row type</label>
        <select id="row-kind">
          <option value="all">All rows</option>
          <option value="overall">Overall only</option>
          <option value="query">Queries only</option>
          <option value="aggregate">Aggregates only</option>
        </select>
      </div>
      <div class="control">
        <label for="hide-empty">Visibility</label>
        <select id="hide-empty">
          <option value="all">Show all rows</option>
          <option value="filled" selected>Only rows with at least one value</option>
          <option value="complete">Only rows with no blanks</option>
        </select>
      </div>
      <div class="control">
        <label for="dataset-sort">Dataset order</label>
        <select id="dataset-sort">
          <option value="manual">Manual</option>
          <option value="alpha">Alphabetical</option>
          <option value="type">By type</option>
        </select>
      </div>
    </section>

    <section class="table-shell">
      <div class="legend">
        <div class="gradient"></div>
        <span>F1 heat from 0.00 to 1.00, with lower values darker. Hatched cells are explicit <code>NaN</code>. Blank cells mean no row in that dataset.</span>
      </div>
      <div class="table-wrap">
        <table id="matrix"></table>
      </div>
    </section>

    <p class="footer-note" id="footer-note"></p>
  </div>

  <div class="tooltip" id="tooltip"></div>

  <script>
    const DATA = __DATA_PLACEHOLDER__;
    const HEAT_COLORS = [
      [157, 61, 18],
      [201, 110, 49],
      [220, 160, 108],
      [233, 201, 166],
      [243, 228, 212]
    ];

    function lerp(a, b, t) {
      return a + (b - a) * t;
    }

    function colorForValue(value) {
      if (value == null || Number.isNaN(value)) return '';
      const t = Math.max(0, Math.min(1, value));
      const scaled = t * (HEAT_COLORS.length - 1);
      const index = Math.floor(scaled);
      const local = scaled - index;
      const from = HEAT_COLORS[index];
      const to = HEAT_COLORS[Math.min(index + 1, HEAT_COLORS.length - 1)];
      const rgb = from.map((v, i) => Math.round(lerp(v, to[i], local)));
      return `rgb(${rgb.join(',')})`;
    }

    function textColorForValue(value) {
      if (value == null || Number.isNaN(value)) return '';
      return value <= 0.55 ? '#fff8ee' : '#2d2118';
    }

    function formatMetric(raw) {
      const num = Number(raw);
      if (Number.isFinite(num)) {
        return num.toFixed(2);
      }
      return String(raw);
    }

    function naturalParts(text) {
      return text.split(/(\\d+)/).map(part => (/^\\d+$/.test(part) ? Number(part) : part.toLowerCase()));
    }

    function compareNatural(a, b) {
      const aa = naturalParts(a);
      const bb = naturalParts(b);
      const len = Math.max(aa.length, bb.length);
      for (let i = 0; i < len; i += 1) {
        if (aa[i] === undefined) return -1;
        if (bb[i] === undefined) return 1;
        if (aa[i] === bb[i]) continue;
        return aa[i] < bb[i] ? -1 : 1;
      }
      return 0;
    }

    function compareDataset(a, b, mode) {
      if (mode === 'alpha') {
        return compareNatural(a.name, b.name);
      }
      if (mode === 'type') {
        const rank = {
          renamed: 0,
          structured: 1,
          mixed: 2,
          nofks: 3,
          denormalized: 4,
          rel: 5,
          atomic: 6,
          other: 99
        };
        const ar = rank[a.variant] ?? 999;
        const br = rank[b.variant] ?? 999;
        if (ar !== br) return ar - br;
        return compareNatural(a.name, b.name);
      }
      return a.manual_index - b.manual_index;
    }

    function main() {
      const data = DATA;
      const legend = document.getElementById('dataset-legend');
      const familyGroups = document.getElementById('family-groups');
      const typeGroups = document.getElementById('type-groups');
      const queryGroups = document.getElementById('query-groups');
      const queryGroupsAll = document.getElementById('query-groups-all');
      const queryGroupsNone = document.getElementById('query-groups-none');
      const matrix = document.getElementById('matrix');
      const search = document.getElementById('search');
      const rowKind = document.getElementById('row-kind');
      const hideEmpty = document.getElementById('hide-empty');
      const datasetSort = document.getElementById('dataset-sort');
      const footer = document.getElementById('footer-note');
      const tooltip = document.getElementById('tooltip');
      const datasetByName = new Map(data.datasets.map(dataset => [dataset.name, dataset]));
      const activeDatasets = new Set(
        data.datasets.filter(dataset => dataset.enabled_by_default !== false).map(dataset => dataset.name)
      );
      const activeQueryGroups = new Set(data.query_groups);
      let manualDatasetOrder = [...data.datasets]
        .sort((a, b) => a.manual_index - b.manual_index)
        .map(dataset => dataset.name);
      let draggedDatasetName = null;
      let rowSort = { key: null, dir: 'asc' };
      let currentRowOrder = data.row_defs.map(row => row.label);
      const familyRank = { cmt: 0, conference: 1, sigkdd: 2, mondial: 3, npd: 4, other: 99 };
      const typeRank = { renamed: 0, structured: 1, mixed: 2, nofks: 3, denormalized: 4, rel: 5, atomic: 6, other: 99 };
      const familyButtons = new Map();
      const typeButtons = new Map();
      const queryGroupButtons = new Map();

      function sortedAllDatasets() {
        if (datasetSort.value === 'manual') {
          return manualDatasetOrder.map(name => datasetByName.get(name)).filter(Boolean);
        }
        return [...data.datasets].sort((a, b) => compareDataset(a, b, datasetSort.value));
      }

      function visibleDatasets() {
        return sortedAllDatasets().filter(dataset => activeDatasets.has(dataset.name));
      }

      function datasetsForFamily(family) {
        return data.datasets.filter(dataset => dataset.family === family);
      }

      function datasetsForType(variant) {
        return data.datasets.filter(dataset => dataset.variant === variant);
      }

      function toggleDatasetGroup(datasetsInGroup) {
        const allActive = datasetsInGroup.every(dataset => activeDatasets.has(dataset.name));
        datasetsInGroup.forEach(dataset => {
          if (allActive) {
            activeDatasets.delete(dataset.name);
          } else {
            activeDatasets.add(dataset.name);
          }
        });
      }

      function toggleQueryGroup(group) {
        if (activeQueryGroups.has(group)) {
          activeQueryGroups.delete(group);
        } else {
          activeQueryGroups.add(group);
        }
      }

      function rowMatchesActiveQueryGroups(row) {
        if (!row) return true;
        if (row.kind === 'overall') return true;
        if (row.kind !== 'aggregate') return true;
        if (!Array.isArray(row.groups) || row.groups.length === 0) return true;
        return row.groups.some(group => activeQueryGroups.has(group));
      }

      function cellMatchesActiveQueryGroups(value) {
        if (!value || !Array.isArray(value.categories) || value.categories.length === 0) {
          return true;
        }
        return value.categories.some(group => activeQueryGroups.has(group));
      }

      function displayedCoverage(row) {
        return row.values.filter(value => {
          if (!value || !cellMatchesActiveQueryGroups(value)) {
            return false;
          }
          return typeof value.f1 === 'number';
        }).length;
      }

      function buildRows(datasets) {
        return data.row_defs.map((row, index) => {
          const values = datasets.map(dataset => dataset.rows[row.label] || null);
          const numeric = values.map(v => (v && typeof v.f1 === 'number' ? v.f1 : null)).filter(v => v != null);
          return {
            ...row,
            sourceIndex: index,
            values,
            avg: numeric.length ? numeric.reduce((sum, v) => sum + v, 0) / numeric.length : null,
            coverage: numeric.length,
          };
        });
      }

      data.datasets.forEach(dataset => {
        const chip = document.createElement('article');
        chip.className = 'legend-chip';
        chip.innerHTML = `
          <div class="short">${dataset.short_name}</div>
          <div class="full">${dataset.name}</div>
          <div class="meta">${dataset.variant}</div>
        `;
        chip.title = activeDatasets.has(dataset.name) ? 'Click to hide dataset' : 'Click to show dataset';
        chip.addEventListener('click', () => {
          if (activeDatasets.has(dataset.name)) {
            activeDatasets.delete(dataset.name);
          } else {
            activeDatasets.add(dataset.name);
          }
          render();
        });
        dataset._chip = chip;
        legend.appendChild(chip);
      });

      [...new Set(data.datasets.map(dataset => dataset.family))]
        .sort((a, b) => (familyRank[a] ?? 999) - (familyRank[b] ?? 999) || compareNatural(a, b))
        .forEach(family => {
          const button = document.createElement('button');
          button.type = 'button';
          button.className = 'group-button';
          button.textContent = family.toUpperCase();
          button.addEventListener('click', () => {
            toggleDatasetGroup(datasetsForFamily(family));
            render();
          });
          familyButtons.set(family, button);
          familyGroups.appendChild(button);
        });

      [...new Set(data.datasets.map(dataset => dataset.variant))]
        .sort((a, b) => (typeRank[a] ?? 999) - (typeRank[b] ?? 999) || compareNatural(a, b))
        .forEach(variant => {
          const button = document.createElement('button');
          button.type = 'button';
          button.className = 'group-button';
          button.textContent = variant;
          button.addEventListener('click', () => {
            toggleDatasetGroup(datasetsForType(variant));
            render();
          });
          typeButtons.set(variant, button);
          typeGroups.appendChild(button);
        });

      data.query_groups.forEach(group => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'group-button';
        button.textContent = group;
        button.addEventListener('click', () => {
          toggleQueryGroup(group);
          render();
        });
        queryGroupButtons.set(group, button);
        queryGroups.appendChild(button);
      });

      queryGroupsAll.addEventListener('click', () => {
        data.query_groups.forEach(group => activeQueryGroups.add(group));
        render();
      });

      queryGroupsNone.addEventListener('click', () => {
        activeQueryGroups.clear();
        render();
      });

      function reorderManualDataset(draggedName, targetName) {
        if (!draggedName || !targetName || draggedName === targetName) {
          return;
        }
        const order = manualDatasetOrder.filter(name => name !== draggedName);
        const targetIndex = order.indexOf(targetName);
        if (targetIndex === -1) {
          order.push(draggedName);
        } else {
          order.splice(targetIndex, 0, draggedName);
        }
        manualDatasetOrder = order;
        datasetSort.value = 'manual';
      }

      function toggleRowSort(key) {
        if (rowSort.key === key) {
          rowSort = { key, dir: rowSort.dir === 'asc' ? 'desc' : 'asc' };
        } else {
          rowSort = { key, dir: 'asc' };
        }
      }

      function sortIconFor(key) {
        if (rowSort.key !== key) return '↕';
        return rowSort.dir === 'asc' ? '↑' : '↓';
      }

      function stableRankMap(rows) {
        const rank = new Map();
        currentRowOrder.forEach((label, index) => rank.set(label, index));
        rows.forEach((row, index) => {
          if (!rank.has(row.label)) {
            rank.set(row.label, currentRowOrder.length + index);
          }
        });
        return rank;
      }

      function sortRows(result, datasets) {
        const rank = stableRankMap(result);
        if (!rowSort.key) {
          return result.sort((a, b) => (rank.get(a.label) ?? a.sourceIndex) - (rank.get(b.label) ?? b.sourceIndex));
        }
        if (rowSort.key === 'label') {
          return result.sort((a, b) => {
            const cmp = compareNatural(a.label, b.label) || ((rank.get(a.label) ?? a.sourceIndex) - (rank.get(b.label) ?? b.sourceIndex));
            return rowSort.dir === 'asc' ? cmp : -cmp;
          });
        }
        if (rowSort.key === 'type') {
          return result.sort((a, b) => {
            const cmp = compareNatural(a.kind, b.kind) || ((rank.get(a.label) ?? a.sourceIndex) - (rank.get(b.label) ?? b.sourceIndex));
            return rowSort.dir === 'asc' ? cmp : -cmp;
          });
        }
        const datasetName = rowSort.key.replace('dataset:', '');
        const datasetIndex = datasets.findIndex(d => d.name === datasetName);
        return result.sort((a, b) => {
          const avRaw = datasetIndex >= 0 ? a.values[datasetIndex] : null;
          const bvRaw = datasetIndex >= 0 ? b.values[datasetIndex] : null;
          const av = cellMatchesActiveQueryGroups(avRaw) ? avRaw : null;
          const bv = cellMatchesActiveQueryGroups(bvRaw) ? bvRaw : null;
          const aHasValue = av && typeof av.f1 === 'number';
          const bHasValue = bv && typeof bv.f1 === 'number';
          const aIsNaN = !!(av && !aHasValue && String(av.f1_raw).toLowerCase() === 'nan');
          const bIsNaN = !!(bv && !bHasValue && String(bv.f1_raw).toLowerCase() === 'nan');
          const aRank = aHasValue ? 0 : aIsNaN ? 1 : 2;
          const bRank = bHasValue ? 0 : bIsNaN ? 1 : 2;
          if (aRank !== bRank) return aRank - bRank;
          if (!aHasValue && !bHasValue) return (rank.get(a.label) ?? a.sourceIndex) - (rank.get(b.label) ?? b.sourceIndex);
          const an = av.f1;
          const bn = bv.f1;
          const cmp = (an - bn) || ((rank.get(a.label) ?? a.sourceIndex) - (rank.get(b.label) ?? b.sourceIndex));
          return rowSort.dir === 'asc' ? cmp : -cmp;
        });
      }

      function escapeHtml(text) {
        return String(text)
          .replaceAll('&', '&amp;')
          .replaceAll('<', '&lt;')
          .replaceAll('>', '&gt;');
      }

      function tooltipHtml(rowLabel, datasetName, value, rowAllowed) {
        if (!value) {
          return `<div class="title">${escapeHtml(datasetName)} · ${escapeHtml(rowLabel)}</div><div class="metrics">No matching row in this dataset.</div>`;
        }
        if (!rowAllowed) {
          return `<div class="title">${escapeHtml(datasetName)} · ${escapeHtml(rowLabel)}</div><div class="metrics">Hidden by the current query-group selection.</div>`;
        }
        const metrics = `F1 ${formatMetric(value.f1_raw)} · P ${formatMetric(value.precision_raw)} · R ${formatMetric(value.recall_raw)}`;
        const parts = [
          `<div class="title">${escapeHtml(datasetName)} · ${escapeHtml(rowLabel)}</div>`,
          `<div class="metrics">${escapeHtml(metrics)}</div>`,
        ];
        if (Array.isArray(value.categories) && value.categories.length) {
          parts.push(`<div class="metrics">Groups: ${escapeHtml(value.categories.join(' · '))}</div>`);
        }
        if (value.sql_query) {
          parts.push(`<div class="section"><div class="section-label">SQL</div><pre>${escapeHtml(value.sql_query)}</pre></div>`);
        }
        if (value.sparql_query) {
          parts.push(`<div class="section"><div class="section-label">SPARQL</div><pre>${escapeHtml(value.sparql_query)}</pre></div>`);
        }
        if (!value.sql_query && !value.sparql_query) {
          parts.push(`<div class="section"><div class="section-label">Query</div><pre>No single SQL/SPARQL query attached to this aggregate row.</pre></div>`);
        }
        return parts.join('');
      }

      function placeTooltip(event) {
        const margin = 16;
        const rect = tooltip.getBoundingClientRect();
        let left = event.clientX + 18;
        let top = event.clientY + 18;
        if (left + rect.width + margin > window.innerWidth) {
          left = window.innerWidth - rect.width - margin;
        }
        if (top + rect.height + margin > window.innerHeight) {
          top = window.innerHeight - rect.height - margin;
        }
        tooltip.style.left = `${Math.max(margin, left)}px`;
        tooltip.style.top = `${Math.max(margin, top)}px`;
      }

      function showTooltip(event, rowLabel, datasetName, value, rowAllowed) {
        tooltip.innerHTML = tooltipHtml(rowLabel, datasetName, value, rowAllowed);
        tooltip.classList.add('visible');
        placeTooltip(event);
      }

      function hideTooltip() {
        tooltip.classList.remove('visible');
      }

      function filteredRows(rows, datasets) {
        const q = search.value.trim().toLowerCase();
        const kind = rowKind.value;
        const emptyMode = hideEmpty.value;
        let result = rows.filter(row => {
          const coverage = displayedCoverage(row);
          if (!rowMatchesActiveQueryGroups(row)) return false;
          if (kind !== 'all' && row.kind !== kind) return false;
          if (q && !row.label.toLowerCase().includes(q)) return false;
          if (emptyMode === 'filled' && coverage === 0) return false;
          if (emptyMode === 'complete' && coverage !== datasets.filter(d => d.has_tabular).length) return false;
          return true;
        });
        return sortRows(result, datasets);
      }

      function updateCurrentRowOrder(visibleRows) {
        const visibleLabels = visibleRows.map(row => row.label);
        const visibleSet = new Set(visibleLabels);
        const remainder = currentRowOrder.filter(label => !visibleSet.has(label));
        currentRowOrder = [...visibleLabels, ...remainder];
      }

      function render() {
        const datasets = visibleDatasets();
        const rows = buildRows(datasets);
        const visibleRows = filteredRows(rows, datasets);
        updateCurrentRowOrder(visibleRows);
        const legendDatasets = sortedAllDatasets();
        data.datasets.forEach(dataset => {
          const active = activeDatasets.has(dataset.name);
          dataset._chip.classList.toggle('active', active);
          dataset._chip.classList.toggle('inactive', !active);
          dataset._chip.title = active ? 'Click to hide dataset' : 'Click to show dataset';
        });
        familyButtons.forEach((button, family) => {
          const familyDatasets = datasetsForFamily(family);
          const allActive = familyDatasets.every(dataset => activeDatasets.has(dataset.name));
          const anyActive = familyDatasets.some(dataset => activeDatasets.has(dataset.name));
          button.classList.toggle('active', allActive);
          button.classList.toggle('inactive', !anyActive);
          button.title = allActive ? `Click to hide all ${family} datasets` : `Click to show all ${family} datasets`;
        });
        typeButtons.forEach((button, variant) => {
          const variantDatasets = datasetsForType(variant);
          const allActive = variantDatasets.every(dataset => activeDatasets.has(dataset.name));
          const anyActive = variantDatasets.some(dataset => activeDatasets.has(dataset.name));
          button.classList.toggle('active', allActive);
          button.classList.toggle('inactive', !anyActive);
          button.title = allActive ? `Click to hide all ${variant} datasets` : `Click to show all ${variant} datasets`;
        });
        queryGroupButtons.forEach((button, group) => {
          const active = activeQueryGroups.has(group);
          button.classList.toggle('active', active);
          button.classList.toggle('inactive', !active);
          button.title = active ? `Click to hide ${group} rows` : `Click to show ${group} rows`;
        });
        legend.replaceChildren(...legendDatasets.map(dataset => dataset._chip));
        const thead = document.createElement('thead');
        const hr = document.createElement('tr');
        ['Query', 'Type', ...datasets.map(d => d.short_name)].forEach((label, idx) => {
          const th = document.createElement('th');
          const wrap = document.createElement('span');
          wrap.className = 'th-inner';
          const text = document.createElement('span');
          text.textContent = label;
          wrap.appendChild(text);
          const sortButton = document.createElement('button');
          sortButton.type = 'button';
          const sortKey = idx === 0 ? 'label' : idx === 1 ? 'type' : `dataset:${datasets[idx - 2].name}`;
          sortButton.className = 'sort-button';
          sortButton.textContent = sortIconFor(sortKey);
          sortButton.classList.toggle('active', rowSort.key === sortKey);
          sortButton.title = `Sort rows by ${label}`;
          sortButton.addEventListener('click', event => {
            event.stopPropagation();
            toggleRowSort(sortKey);
            render();
          });
          wrap.appendChild(sortButton);
          th.appendChild(wrap);
          if (idx >= 2) {
            const dataset = datasets[idx - 2];
            th.title = `${dataset.name} · drag left/right to reorder`;
            th.classList.add('dataset-col');
            th.draggable = true;
            th.addEventListener('dragstart', event => {
              draggedDatasetName = dataset.name;
              th.classList.add('dragging');
              if (event.dataTransfer) {
                event.dataTransfer.effectAllowed = 'move';
                event.dataTransfer.setData('text/plain', dataset.name);
              }
            });
            th.addEventListener('dragend', () => {
              draggedDatasetName = null;
              th.classList.remove('dragging');
              thead.querySelectorAll('.drag-over').forEach(el => el.classList.remove('drag-over'));
            });
            th.addEventListener('dragover', event => {
              if (!draggedDatasetName || draggedDatasetName === dataset.name) return;
              event.preventDefault();
              th.classList.add('drag-over');
              if (event.dataTransfer) {
                event.dataTransfer.dropEffect = 'move';
              }
            });
            th.addEventListener('dragleave', () => {
              th.classList.remove('drag-over');
            });
            th.addEventListener('drop', event => {
              event.preventDefault();
              th.classList.remove('drag-over');
              reorderManualDataset(draggedDatasetName, dataset.name);
              render();
            });
          }
          hr.appendChild(th);
        });
        thead.appendChild(hr);

        const tbody = document.createElement('tbody');
        visibleRows.forEach(row => {
          const tr = document.createElement('tr');

          const tdLabel = document.createElement('td');
          tdLabel.textContent = row.label;
          tr.appendChild(tdLabel);

          const tdType = document.createElement('td');
          tdType.textContent = row.kind;
          tdType.title = row.label;
          tr.appendChild(tdType);

          row.values.forEach((value, index) => {
            const td = document.createElement('td');
            const datasetName = datasets[index].name;
            const rowAllowed = cellMatchesActiveQueryGroups(value);
            if (!value || !rowAllowed) {
              td.textContent = '—';
              td.className = 'blank';
            } else if (typeof value.f1 === 'number') {
              td.textContent = value.f1.toFixed(2);
              td.className = 'heat';
              td.style.background = colorForValue(value.f1);
              td.style.color = textColorForValue(value.f1);
            } else {
              td.textContent = value.f1_raw;
              td.className = 'nan';
            }
            td.addEventListener('mouseenter', event => showTooltip(event, row.label, datasetName, value, rowAllowed));
            td.addEventListener('mousemove', placeTooltip);
            td.addEventListener('mouseleave', hideTooltip);
            tr.appendChild(td);
          });

          tbody.appendChild(tr);
        });

        matrix.replaceChildren(thead, tbody);
        footer.textContent = `${visibleRows.length} rows shown · ${datasets.length}/${data.datasets.length} datasets active · source ${data.run_path}`;
      }

      [search, rowKind, hideEmpty, datasetSort].forEach(el => el.addEventListener('input', render));
      render();
    }

    try {
      main();
    } catch (err) {
      document.body.innerHTML = `<pre style="padding:24px;color:#8a3b12">${String(err)}</pre>`;
    }
  </script>
</body>
</html>
""".replace("__DATA_PLACEHOLDER__", payload_json)


def main() -> int:
    args = parse_args()
    run_path = args.run_path.resolve()
    if not run_path.exists() or not run_path.is_dir():
        raise SystemExit(f"Run path not found or not a directory: {run_path}")

    output_dir = (args.output_dir or (run_path / "summary" / "rodi_f1_site")).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    payload = _build_payload(run_path)
    payload_json = json.dumps(payload, indent=2)
    (output_dir / "data.json").write_text(payload_json, encoding="utf-8")
    (output_dir / "index.html").write_text(_html(payload_json), encoding="utf-8")

    print(f"Wrote {output_dir / 'data.json'}")
    print(f"Wrote {output_dir / 'index.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
