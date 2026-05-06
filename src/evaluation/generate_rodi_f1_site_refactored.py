from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    from src.evaluation.generate_rodi_f1_site import (
        DATASET_LABELS,
        DEFAULT_DISABLED_DATASETS,
        METHOD_TABULAR_FILES,
        _classify_label,
        _dataset_family,
        _dataset_variant,
        _html as _base_html,
        _natural_key,
        _read_query_manifest,
        _read_tabular,
        _row_order_key,
    )
except ModuleNotFoundError:  # pragma: no cover - fallback for direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from src.evaluation.generate_rodi_f1_site import (
        DATASET_LABELS,
        DEFAULT_DISABLED_DATASETS,
        METHOD_TABULAR_FILES,
        _classify_label,
        _dataset_family,
        _dataset_variant,
        _html as _base_html,
        _natural_key,
        _read_query_manifest,
        _read_tabular,
        _row_order_key,
    )


@dataclass(frozen=True)
class BatchRef:
    system_dir: Path
    batch_dir: Path


@dataclass(frozen=True)
class SourceRef:
    system_name: str
    timestamp: str
    method: str
    rel_parts: tuple[str, ...]
    dataset_dirs: tuple[Path, ...]

    @property
    def id(self) -> str:
        return f"{self.system_name}::{self.timestamp}::{self.method}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Refactored alternative generator for the RODI/ontop F1 comparison site."
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
        help="Directory where the site should be written. Defaults to outputs/summary/rodi_f1_site_refactored",
    )
    parser.add_argument(
        "--discover-root",
        type=Path,
        help="Root outputs directory to scan for selectable result sources. Defaults to the top-level outputs directory.",
    )
    return parser.parse_args()


def _is_dataset_archive_dir(path: Path) -> bool:
    return path.is_dir() and (path / "run_metadata.json").exists() and (path / "mappings_r2rml.ttl").exists()


def _batch_dataset_dirs(batch_dir: Path) -> list[Path]:
    return sorted(path for path in batch_dir.iterdir() if _is_dataset_archive_dir(path))


def _ordered_batches(run_path: Optional[Path], discover_root: Path) -> list[BatchRef]:
    prioritized: list[BatchRef] = []
    others: list[BatchRef] = []

    for system_dir in sorted(path for path in discover_root.iterdir() if path.is_dir()):
        batch_dirs = sorted((path for path in system_dir.iterdir() if path.is_dir()), key=lambda p: p.name, reverse=True)
        for batch_dir in batch_dirs:
            ref = BatchRef(system_dir=system_dir, batch_dir=batch_dir)
            if run_path is not None and batch_dir.resolve() == run_path:
                prioritized.append(ref)
            else:
                others.append(ref)

    return prioritized + others


def _discover_sources(run_path: Optional[Path], discover_root: Path) -> list[SourceRef]:
    sources: list[SourceRef] = []
    for batch in _ordered_batches(run_path, discover_root):
        dataset_dirs = _batch_dataset_dirs(batch.batch_dir)
        if not dataset_dirs:
            continue

        for method, rel_parts in METHOD_TABULAR_FILES.items():
            available = tuple(dataset_dir for dataset_dir in dataset_dirs if (dataset_dir / Path(*rel_parts)).exists())
            if not available:
                continue

            sources.append(
                SourceRef(
                    system_name=batch.system_dir.name,
                    timestamp=batch.batch_dir.name,
                    method=method,
                    rel_parts=tuple(rel_parts),
                    dataset_dirs=available,
                )
            )
    return sources


class PayloadBuilder:
    def __init__(self, run_path: Optional[Path], discover_root: Path) -> None:
        self.run_path = run_path
        self.discover_root = discover_root
        self.datasets: list[dict[str, object]] = []
        self.sources: list[dict[str, object]] = []
        self.first_seen_order: dict[str, int] = {}
        self.row_groups: dict[str, set[str]] = {}
        self._seen_counter = 0
        self._dataset_index = 0

    def build(self) -> dict[str, object]:
        for source_index, source in enumerate(_discover_sources(self.run_path, self.discover_root)):
            self._append_source(source, source_index)
            for dataset_dir in source.dataset_dirs:
                self._append_dataset(source, source_index, dataset_dir)

        return {
            "run_path": str(self.run_path) if self.run_path else "",
            "discover_root": str(self.discover_root),
            "row_defs": self._build_row_defs(),
            "query_groups": self._build_query_groups(),
            "sources": self.sources,
            "datasets": self.datasets,
        }

    def _append_source(self, source: SourceRef, source_index: int) -> None:
        self.sources.append(
            {
                "id": source.id,
                "system": source.system_name,
                "timestamp": source.timestamp,
                "method": source.method,
                "default_suffix": f"R{source_index + 1}",
                "enabled_by_default": False,
                "dataset_names": [dataset_dir.name for dataset_dir in source.dataset_dirs],
            }
        )

    def _append_dataset(self, source: SourceRef, source_index: int, dataset_dir: Path) -> None:
        row_map: dict[str, dict[str, object]] = {}
        overall_f1 = None
        query_lookup = _read_query_manifest(dataset_dir)
        tabular_file = dataset_dir / Path(*source.rel_parts)

        for row in _read_tabular(tabular_file):
            label = row["label"]
            assert isinstance(label, str)
            self._apply_query_metadata(row, query_lookup.get(label))
            row_map[label] = row
            self._remember_label(label)
            if label == "All (AVG)":
                overall_f1 = row["f1"]

        self.datasets.append(
            {
                "id": f"{source.id}::{dataset_dir.name}",
                "source_id": source.id,
                "base_name": dataset_dir.name,
                "short_base": DATASET_LABELS.get(dataset_dir.name, dataset_dir.name),
                "name": dataset_dir.name,
                "family": _dataset_family(dataset_dir.name),
                "variant": _dataset_variant(dataset_dir.name),
                "manual_index": self._dataset_index,
                "source_index": source_index,
                "enabled_by_default": False,
                "has_tabular": True,
                "overall_f1": overall_f1,
                "rows": row_map,
            }
        )
        self._dataset_index += 1

    def _apply_query_metadata(self, row: dict[str, object], query_info: dict[str, object] | None) -> None:
        if not query_info:
            return
        categories = query_info.get("categories")
        if isinstance(categories, list):
            label = row["label"]
            assert isinstance(label, str)
            self.row_groups.setdefault(label, set()).update(str(cat) for cat in categories)
        row.update(
            {
                "query_id": query_info.get("id"),
                "categories": query_info.get("categories"),
                "sql_query": query_info.get("sql_query"),
                "sparql_query": query_info.get("sparql_query"),
            }
        )

    def _remember_label(self, label: str) -> None:
        if label not in self.first_seen_order:
            self.first_seen_order[label] = self._seen_counter
            self._seen_counter += 1

    def _build_row_defs(self) -> list[dict[str, object]]:
        row_defs: list[dict[str, object]] = []
        for label in sorted(self.first_seen_order, key=lambda item: _row_order_key(item, self.first_seen_order)):
            kind = _classify_label(label)
            groups = set(self.row_groups.get(label, set()))
            if kind == "aggregate":
                groups.add(label.removesuffix(" (AVG)"))
            row_defs.append(
                {
                    "label": label,
                    "kind": kind,
                    "groups": sorted(groups, key=_natural_key),
                }
            )
        return row_defs

    def _build_query_groups(self) -> list[str]:
        ordered_groups: list[str] = []
        seen: set[str] = set()
        for row in self._build_row_defs():
            for group in row["groups"]:
                if group not in seen:
                    seen.add(group)
                    ordered_groups.append(group)
        return ordered_groups


def build_payload(run_path: Optional[Path], discover_root: Path) -> dict[str, object]:
    return PayloadBuilder(run_path, discover_root).build()


def _javascript_refactored() -> str:
    return """
    const DATA = __DATA_PLACEHOLDER__;

    const Heat = {
      colors: [
        [157, 61, 18],
        [201, 110, 49],
        [220, 160, 108],
        [233, 201, 166],
        [243, 228, 212]
      ],
      lerp(a, b, t) {
        return a + (b - a) * t;
      },
      colorForValue(value) {
        if (value == null || Number.isNaN(value)) return '';
        const t = Math.max(0, Math.min(1, value));
        const scaled = t * (this.colors.length - 1);
        const index = Math.floor(scaled);
        const local = scaled - index;
        const from = this.colors[index];
        const to = this.colors[Math.min(index + 1, this.colors.length - 1)];
        const rgb = from.map((v, i) => Math.round(this.lerp(v, to[i], local)));
        return `rgb(${rgb.join(',')})`;
      },
      textColorForValue(value) {
        if (value == null || Number.isNaN(value)) return '';
        return value <= 0.55 ? '#fff8ee' : '#2d2118';
      }
    };

    const Format = {
      formatMetric(raw) {
        const num = Number(raw);
        if (Number.isFinite(num)) return num.toFixed(2);
        return String(raw);
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
      escapeHtml(text) {
        return String(text)
          .replaceAll('&', '&amp;')
          .replaceAll('<', '&lt;')
          .replaceAll('>', '&gt;');
      }
    };

    const Compare = {
      explicitNaN(value) {
        return !!(value && String(value.f1_raw).toLowerCase() === 'nan');
      },
      valueState(value, rowAllowed) {
        if (!value || !rowAllowed) return 'missing';
        if (typeof value.f1 === 'number') return 'number';
        if (this.explicitNaN(value)) return 'nan';
        return 'missing';
      },
      roundDisplayValue(num) {
        return Math.round((num + Number.EPSILON) * 100) / 100;
      },
      values(leftValue, leftAllowed, rightValue, rightAllowed) {
        const leftState = this.valueState(leftValue, leftAllowed);
        const rightState = this.valueState(rightValue, rightAllowed);
        if (leftState === 'missing' || rightState === 'missing') return null;
        if (leftState === 'number' && rightState === 'number') {
          return {
            kind: 'numeric',
            delta: this.roundDisplayValue(leftValue.f1) - this.roundDisplayValue(rightValue.f1),
          };
        }
        if (leftState === 'number' && rightState === 'nan') {
          return { kind: 'left-better-nan', delta: null };
        }
        if (leftState === 'nan' && rightState === 'number') {
          return { kind: 'left-worse-nan', delta: null };
        }
        if (leftState === 'nan' && rightState === 'nan') {
          return { kind: 'both-nan', delta: null };
        }
        return null;
      },
      priority(relation) {
        if (!relation) return 0;
        if (relation.kind === 'left-better-nan' || relation.kind === 'left-worse-nan') return 3;
        if (relation.kind === 'numeric') return 2;
        if (relation.kind === 'both-nan') return 1;
        return 0;
      },
      deltaClass(delta) {
        if (delta > 1e-9) return 'compare-better';
        if (delta < -1e-9) return 'compare-worse';
        return '';
      },
      deltaText(delta) {
        const sign = delta > 0 ? '+' : '';
        return `${sign}${delta.toFixed(2)}`;
      },
      relationClass(relation) {
        if (!relation) return '';
        if (relation.kind === 'left-better-nan') return 'compare-better';
        if (relation.kind === 'left-worse-nan') return 'compare-worse';
        if (relation.kind === 'numeric') return this.deltaClass(relation.delta);
        return '';
      },
      relationText(relation, otherValue) {
        if (!relation) return '';
        if (relation.kind === 'numeric') return this.deltaText(relation.delta);
        if (relation.kind === 'left-better-nan') return 'better than NaN';
        if (relation.kind === 'left-worse-nan') return `worse than ${Format.formatMetric(otherValue?.f1_raw)}`;
        if (relation.kind === 'both-nan') return 'both NaN';
        return '';
      },
      buildContext(app, datasets) {
        if (app.state.compareMode !== 'first-two') return null;
        const sourceA = app.logic.getBaselineSource();
        const sourceB = app.state.activeSourceOrder
          .filter(sourceId => sourceId !== sourceA?.id && app.state.activeSources.has(sourceId))
          .map(sourceId => app.sourceById.get(sourceId))
          .find(Boolean) || null;
        if (!sourceA || !sourceB) {
          return { error: 'Select at least two result sources to compare.' };
        }
        const sourceAByBase = new Map();
        const sourceBByBase = new Map();
        datasets.forEach((dataset, index) => {
          if (dataset.source_id === sourceA.id) sourceAByBase.set(dataset.base_name, { dataset, index });
          if (dataset.source_id === sourceB.id) sourceBByBase.set(dataset.base_name, { dataset, index });
        });
        const pairs = new Map();
        const report = [];
        for (const [baseName, left] of sourceAByBase.entries()) {
          const right = sourceBByBase.get(baseName);
          if (!right) continue;
          pairs.set(left.dataset.id, { baseName, role: 'a', self: left, other: right });
          pairs.set(right.dataset.id, { baseName, role: 'b', self: right, other: left });
          const leftOverall = left.dataset.rows['All (AVG)'];
          const rightOverall = right.dataset.rows['All (AVG)'];
          const relation = this.values(leftOverall, true, rightOverall, true);
          if (relation) {
            report.push({
              baseName,
              leftLabel: app.display.datasetName(left.dataset),
              rightLabel: app.display.datasetName(right.dataset),
              leftValue: leftOverall?.f1_raw ?? null,
              rightValue: rightOverall?.f1_raw ?? null,
              relation,
            });
          }
        }
        report.sort((a, b) => this.priority(b.relation) - this.priority(a.relation)
          || Math.abs(b.relation.delta ?? 0) - Math.abs(a.relation.delta ?? 0)
          || Format.compareNatural(a.baseName, b.baseName));
        return { sourceA, sourceB, pairs, report };
      }
    };

    const App = {
      familyRank: { cmt: 0, conference: 1, sigkdd: 2, mondial: 3, npd: 4, other: 99 },
      typeRank: { renamed: 0, structured: 1, mixed: 2, nofks: 3, denormalized: 4, rel: 5, atomic: 6, other: 99 },

      init(data) {
        this.data = data;
        this.cacheDom();
        this.indexData();
        this.initState();
        this.buildDynamicUi();
        this.bindStaticControls();
        this.render();
      },

      cacheDom() {
        this.dom = {
          sourceSelection: document.getElementById('source-selection'),
          systemGroups: document.getElementById('system-groups'),
          legend: document.getElementById('dataset-legend'),
          familyGroups: document.getElementById('family-groups'),
          typeGroups: document.getElementById('type-groups'),
          queryGroups: document.getElementById('query-groups'),
          queryGroupsAll: document.getElementById('query-groups-all'),
          queryGroupsNone: document.getElementById('query-groups-none'),
          matrix: document.getElementById('matrix'),
          search: document.getElementById('search'),
          rowKind: document.getElementById('row-kind'),
          hideEmpty: document.getElementById('hide-empty'),
          datasetSort: document.getElementById('dataset-sort'),
          compareMode: document.getElementById('compare-mode'),
          footer: document.getElementById('footer-note'),
          compareReport: document.getElementById('compare-report'),
          compareSummary: document.getElementById('compare-summary'),
          compareList: document.getElementById('compare-list'),
          tooltip: document.getElementById('tooltip'),
        };
      },

      indexData() {
        this.sourceById = new Map(this.data.sources.map(source => [source.id, source]));
        this.datasetById = new Map(this.data.datasets.map(dataset => [dataset.id, dataset]));
        this.datasetGroups = {
          systems: [...new Set(this.data.sources.map(source => source.system))].sort((a, b) => Format.compareNatural(a, b)),
          families: [...new Set(this.data.datasets.map(dataset => dataset.family))]
            .sort((a, b) => (this.familyRank[a] ?? 999) - (this.familyRank[b] ?? 999) || Format.compareNatural(a, b)),
          variants: [...new Set(this.data.datasets.map(dataset => dataset.variant))]
            .sort((a, b) => (this.typeRank[a] ?? 999) - (this.typeRank[b] ?? 999) || Format.compareNatural(a, b)),
        };
      },

      initState() {
        this.state = {
          activeSources: new Set(this.data.sources.filter(source => source.enabled_by_default !== false).map(source => source.id)),
          activeSourceOrder: this.data.sources.filter(source => source.enabled_by_default !== false).map(source => source.id),
          baselineSourceId: this.data.sources.filter(source => source.enabled_by_default !== false).map(source => source.id)[0] || null,
          activeDatasets: new Set(this.data.datasets.filter(dataset => dataset.enabled_by_default !== false).map(dataset => dataset.id)),
          activeQueryGroups: new Set(this.data.query_groups),
          sourceSuffix: new Map(this.data.sources.map(source => [source.id, source.default_suffix])),
          manualDatasetOrder: [...this.data.datasets].sort((a, b) => a.manual_index - b.manual_index).map(dataset => dataset.id),
          draggedDatasetId: null,
          rowSort: { key: null, dir: 'asc' },
          currentRowOrder: this.data.row_defs.map(row => row.label),
          compareMode: this.dom.compareMode.value,
        };
        this.view = {
          sourceCards: new Map(),
          systemButtons: new Map(),
          familyButtons: new Map(),
          typeButtons: new Map(),
          queryGroupButtons: new Map(),
        };
      },

      display: {
        datasetName(dataset) {
          return `${dataset.base_name}_${App.state.sourceSuffix.get(dataset.source_id) || App.sourceById.get(dataset.source_id)?.default_suffix || 'R?'}`;
        },
        datasetShort(dataset) {
          return `${dataset.short_base}_${App.state.sourceSuffix.get(dataset.source_id) || App.sourceById.get(dataset.source_id)?.default_suffix || 'R?'}`;
        },
        datasetSourceLabel(dataset) {
          const source = App.sourceById.get(dataset.source_id);
          if (!source) return '';
          return `${source.system} · ${source.timestamp} · ${source.method}`;
        },
        sourceLabel(source) {
          return `${source.system} · ${source.timestamp} · ${source.method} · ${App.state.sourceSuffix.get(source.id) || source.default_suffix}`;
        }
      },

      select: {
        sourcesForSystem(system) {
          return App.data.sources.filter(source => source.system === system);
        },
        sortedDatasets() {
          if (App.dom.datasetSort.value === 'manual') {
            return App.state.manualDatasetOrder.map(id => App.datasetById.get(id)).filter(Boolean);
          }
          return [...App.data.datasets].sort((a, b) => App.compareDataset(a, b, App.dom.datasetSort.value));
        },
        datasetsFromActiveSources() {
          return this.sortedDatasets().filter(dataset => App.state.activeSources.has(dataset.source_id));
        },
        visibleDatasets() {
          return this.sortedDatasets().filter(
            dataset => App.state.activeSources.has(dataset.source_id) && App.state.activeDatasets.has(dataset.id)
          );
        },
        datasetsForFamily(family) {
          return this.datasetsFromActiveSources().filter(dataset => dataset.family === family);
        },
        datasetsForType(variant) {
          return this.datasetsFromActiveSources().filter(dataset => dataset.variant === variant);
        }
      },

      compareDataset(a, b, mode) {
        const displayedA = this.display.datasetName(a);
        const displayedB = this.display.datasetName(b);
        if (mode === 'alpha') {
          return Format.compareNatural(displayedA, displayedB) || (a.source_index - b.source_index);
        }
        if (mode === 'type') {
          const ar = this.typeRank[a.variant] ?? 999;
          const br = this.typeRank[b.variant] ?? 999;
          if (ar !== br) return ar - br;
          return Format.compareNatural(displayedA, displayedB) || (a.source_index - b.source_index);
        }
        return a.manual_index - b.manual_index;
      },

      logic: {
        toggleDatasetGroup(datasetsInGroup) {
          const anyActive = datasetsInGroup.some(dataset => App.state.activeDatasets.has(dataset.id));
          datasetsInGroup.forEach(dataset => {
            if (anyActive) App.state.activeDatasets.delete(dataset.id);
            else App.state.activeDatasets.add(dataset.id);
          });
        },
        activateSource(sourceId) {
          if (!App.state.activeSources.has(sourceId)) {
            App.state.activeSources.add(sourceId);
          }
          App.state.activeSourceOrder = App.state.activeSourceOrder.filter(id => id !== sourceId);
          App.state.activeSourceOrder.push(sourceId);
          if (!App.state.baselineSourceId) {
            App.state.baselineSourceId = sourceId;
          }
        },
        deactivateSource(sourceId) {
          App.state.activeSources.delete(sourceId);
          App.state.activeSourceOrder = App.state.activeSourceOrder.filter(id => id !== sourceId);
          if (App.state.baselineSourceId === sourceId) {
            App.state.baselineSourceId = App.state.activeSourceOrder[0] || null;
          }
        },
        setBaselineSource(sourceId) {
          if (!App.state.activeSources.has(sourceId)) return;
          App.state.baselineSourceId = sourceId;
        },
        getBaselineSource() {
          if (App.state.baselineSourceId && App.state.activeSources.has(App.state.baselineSourceId)) {
            return App.sourceById.get(App.state.baselineSourceId) || null;
          }
          App.state.baselineSourceId = App.state.activeSourceOrder[0] || null;
          return App.state.baselineSourceId ? App.sourceById.get(App.state.baselineSourceId) || null : null;
        },
        toggleSystemGroup(systemSources) {
          const anyActive = systemSources.some(source => App.state.activeSources.has(source.id));
          systemSources.forEach(source => {
            if (anyActive) this.deactivateSource(source.id);
            else this.activateSource(source.id);
          });
        },
        toggleQueryGroup(group) {
          if (App.state.activeQueryGroups.has(group)) App.state.activeQueryGroups.delete(group);
          else App.state.activeQueryGroups.add(group);
        },
        rowMatchesActiveQueryGroups(row) {
          if (!row) return true;
          if (row.kind === 'overall' || row.kind === 'query') return true;
          if (!Array.isArray(row.groups) || row.groups.length === 0) {
            return App.state.activeQueryGroups.size === App.data.query_groups.length;
          }
          return row.groups.some(group => App.state.activeQueryGroups.has(group));
        },
        cellMatchesActiveQueryGroups(value) {
          if (!value || !Array.isArray(value.categories) || value.categories.length === 0) {
            return App.state.activeQueryGroups.size === App.data.query_groups.length;
          }
          return value.categories.some(group => App.state.activeQueryGroups.has(group));
        },
        displayedCoverage(row) {
          return row.values.filter(value => value && this.cellMatchesActiveQueryGroups(value) && typeof value.f1 === 'number').length;
        },
        buildRows(datasets) {
          return App.data.row_defs.map((row, index) => {
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
        },
        reorderManualDataset(draggedId, targetId) {
          if (!draggedId || !targetId || draggedId === targetId) return;
          const order = App.state.manualDatasetOrder.filter(id => id !== draggedId);
          const targetIndex = order.indexOf(targetId);
          if (targetIndex === -1) order.push(draggedId);
          else order.splice(targetIndex, 0, draggedId);
          App.state.manualDatasetOrder = order;
          App.dom.datasetSort.value = 'manual';
        },
        toggleRowSort(key) {
          if (App.state.rowSort.key === key) {
            App.state.rowSort = { key, dir: App.state.rowSort.dir === 'asc' ? 'desc' : 'asc' };
          } else {
            App.state.rowSort = { key, dir: 'asc' };
          }
        },
        sortIconFor(key) {
          if (App.state.rowSort.key !== key) return '↕';
          return App.state.rowSort.dir === 'asc' ? '↑' : '↓';
        },
        stableRankMap(rows) {
          const rank = new Map();
          App.state.currentRowOrder.forEach((label, index) => rank.set(label, index));
          rows.forEach((row, index) => {
            if (!rank.has(row.label)) rank.set(row.label, App.state.currentRowOrder.length + index);
          });
          return rank;
        },
        sortRows(rows, datasets) {
          const rank = this.stableRankMap(rows);
          const rowSort = App.state.rowSort;
          if (!rowSort.key) {
            return rows.sort((a, b) => (rank.get(a.label) ?? a.sourceIndex) - (rank.get(b.label) ?? b.sourceIndex));
          }
          if (rowSort.key === 'label') {
            return rows.sort((a, b) => {
              const cmp = Format.compareNatural(a.label, b.label)
                || ((rank.get(a.label) ?? a.sourceIndex) - (rank.get(b.label) ?? b.sourceIndex));
              return rowSort.dir === 'asc' ? cmp : -cmp;
            });
          }
          if (rowSort.key === 'type') {
            return rows.sort((a, b) => {
              const cmp = Format.compareNatural(a.kind, b.kind)
                || ((rank.get(a.label) ?? a.sourceIndex) - (rank.get(b.label) ?? b.sourceIndex));
              return rowSort.dir === 'asc' ? cmp : -cmp;
            });
          }
          const datasetId = rowSort.key.replace('dataset:', '');
          const datasetIndex = datasets.findIndex(d => d.id === datasetId);
          return rows.sort((a, b) => {
            const avRaw = datasetIndex >= 0 ? a.values[datasetIndex] : null;
            const bvRaw = datasetIndex >= 0 ? b.values[datasetIndex] : null;
            const av = this.cellMatchesActiveQueryGroups(avRaw) ? avRaw : null;
            const bv = this.cellMatchesActiveQueryGroups(bvRaw) ? bvRaw : null;
            const aHasValue = av && typeof av.f1 === 'number';
            const bHasValue = bv && typeof bv.f1 === 'number';
            const aIsNaN = !!(av && !aHasValue && String(av.f1_raw).toLowerCase() === 'nan');
            const bIsNaN = !!(bv && !bHasValue && String(bv.f1_raw).toLowerCase() === 'nan');
            const aRank = aHasValue ? 0 : aIsNaN ? 1 : 2;
            const bRank = bHasValue ? 0 : bIsNaN ? 1 : 2;
            if (aRank !== bRank) return aRank - bRank;
            if (!aHasValue && !bHasValue) {
              return (rank.get(a.label) ?? a.sourceIndex) - (rank.get(b.label) ?? b.sourceIndex);
            }
            const cmp = (av.f1 - bv.f1) || ((rank.get(a.label) ?? a.sourceIndex) - (rank.get(b.label) ?? b.sourceIndex));
            return rowSort.dir === 'asc' ? cmp : -cmp;
          });
        },
        filteredRows(rows, datasets) {
          const q = App.dom.search.value.trim().toLowerCase();
          const kind = App.dom.rowKind.value;
          const emptyMode = App.dom.hideEmpty.value;
          const filtered = rows.filter(row => {
            const coverage = this.displayedCoverage(row);
            if (!this.rowMatchesActiveQueryGroups(row)) return false;
            if (kind !== 'all' && row.kind !== kind) return false;
            if (q && !row.label.toLowerCase().includes(q)) return false;
            if (emptyMode === 'filled' && coverage === 0) return false;
            if (emptyMode === 'complete' && coverage !== datasets.filter(d => d.has_tabular).length) return false;
            return true;
          });
          return this.sortRows(filtered, datasets);
        },
        updateCurrentRowOrder(visibleRows) {
          const visibleLabels = visibleRows.map(row => row.label);
          const visibleSet = new Set(visibleLabels);
          const remainder = App.state.currentRowOrder.filter(label => !visibleSet.has(label));
          App.state.currentRowOrder = [...visibleLabels, ...remainder];
        },
      },

      buildDynamicUi() {
        this.data.datasets.forEach(dataset => {
          const chip = document.createElement('article');
          chip.className = 'legend-chip';
          chip.addEventListener('click', () => {
            if (this.state.activeDatasets.has(dataset.id)) this.state.activeDatasets.delete(dataset.id);
            else this.state.activeDatasets.add(dataset.id);
            this.render();
          });
          dataset._chip = chip;
        });

        this.data.sources.forEach(source => {
          const card = document.createElement('article');
          card.className = 'source-card';
          card.innerHTML = `
            <div class="source-top">
              <label class="source-check">
                <input type="checkbox" ${this.state.activeSources.has(source.id) ? 'checked' : ''} />
                <div class="source-main">
                  <div class="source-system">${source.system}</div>
                  <div class="source-stamp">${source.timestamp}</div>
                  <div class="source-method">${source.method}</div>
                </div>
              </label>
              <div class="source-controls">
                <div class="source-suffix">
                  <label>Suffix</label>
                  <input type="text" value="${source.default_suffix}" />
                </div>
                <div class="source-baseline">
                  <label><input type="checkbox" /> Baseline</label>
                </div>
              </div>
            </div>
            <div class="source-meta">${source.dataset_names.length} datasets</div>
          `;
          const checkbox = card.querySelector('input[type="checkbox"]');
          const suffixInput = card.querySelector('.source-suffix input');
          const baselineInput = card.querySelector('.source-baseline input');
          checkbox.addEventListener('input', () => {
            if (checkbox.checked) this.logic.activateSource(source.id);
            else this.logic.deactivateSource(source.id);
            this.render();
          });
          suffixInput.addEventListener('input', () => {
            this.state.sourceSuffix.set(source.id, suffixInput.value.trim() || source.default_suffix);
            this.render();
          });
          baselineInput.addEventListener('input', () => {
            if (baselineInput.checked) {
              this.logic.setBaselineSource(source.id);
            }
            this.render();
          });
          this.view.sourceCards.set(source.id, { card, checkbox, suffixInput, baselineInput });
          this.dom.sourceSelection.appendChild(card);
        });

        this.datasetGroups.systems.forEach(system => {
          const button = document.createElement('button');
          button.type = 'button';
          button.className = 'group-button';
          button.textContent = system;
          button.addEventListener('click', () => {
            this.logic.toggleSystemGroup(this.select.sourcesForSystem(system));
            this.render();
          });
          this.view.systemButtons.set(system, button);
          this.dom.systemGroups.appendChild(button);
        });

        this.datasetGroups.families.forEach(family => {
          const button = document.createElement('button');
          button.type = 'button';
          button.className = 'group-button';
          button.textContent = family.toUpperCase();
          button.addEventListener('click', () => {
            this.logic.toggleDatasetGroup(this.select.datasetsForFamily(family));
            this.render();
          });
          this.view.familyButtons.set(family, button);
          this.dom.familyGroups.appendChild(button);
        });

        this.datasetGroups.variants.forEach(variant => {
          const button = document.createElement('button');
          button.type = 'button';
          button.className = 'group-button';
          button.textContent = variant;
          button.addEventListener('click', () => {
            this.logic.toggleDatasetGroup(this.select.datasetsForType(variant));
            this.render();
          });
          this.view.typeButtons.set(variant, button);
          this.dom.typeGroups.appendChild(button);
        });

        this.data.query_groups.forEach(group => {
          const button = document.createElement('button');
          button.type = 'button';
          button.className = 'group-button';
          button.textContent = group;
          button.addEventListener('click', () => {
            this.logic.toggleQueryGroup(group);
            this.render();
          });
          this.view.queryGroupButtons.set(group, button);
          this.dom.queryGroups.appendChild(button);
        });
      },

      bindStaticControls() {
        this.dom.queryGroupsAll.addEventListener('click', () => {
          this.data.query_groups.forEach(group => this.state.activeQueryGroups.add(group));
          this.render();
        });
        this.dom.queryGroupsNone.addEventListener('click', () => {
          this.state.activeQueryGroups.clear();
          this.render();
        });
        [this.dom.search, this.dom.rowKind, this.dom.hideEmpty, this.dom.datasetSort, this.dom.compareMode]
          .forEach(el => el.addEventListener('input', () => {
            this.state.compareMode = this.dom.compareMode.value;
            this.render();
          }));
      },

      tooltipHtml(rowLabel, datasetName, value, rowAllowed, compareNote = null) {
        if (!value) {
          return `<div class="title">${Format.escapeHtml(datasetName)} · ${Format.escapeHtml(rowLabel)}</div><div class="metrics">No matching row in this dataset.</div>`;
        }
        if (!rowAllowed) {
          return `<div class="title">${Format.escapeHtml(datasetName)} · ${Format.escapeHtml(rowLabel)}</div><div class="metrics">Hidden by the current query-group selection.</div>`;
        }
        const metrics = `F1 ${Format.formatMetric(value.f1_raw)} · P ${Format.formatMetric(value.precision_raw)} · R ${Format.formatMetric(value.recall_raw)}`;
        const parts = [
          `<div class="title">${Format.escapeHtml(datasetName)} · ${Format.escapeHtml(rowLabel)}</div>`,
          `<div class="metrics">${Format.escapeHtml(metrics)}</div>`,
        ];
        if (compareNote) parts.push(`<div class="metrics">${Format.escapeHtml(compareNote)}</div>`);
        if (Array.isArray(value.categories) && value.categories.length) {
          parts.push(`<div class="metrics">Groups: ${Format.escapeHtml(value.categories.join(' · '))}</div>`);
        }
        if (value.sql_query) {
          parts.push(`<div class="section"><div class="section-label">SQL</div><pre>${Format.escapeHtml(value.sql_query)}</pre></div>`);
        }
        if (value.sparql_query) {
          parts.push(`<div class="section"><div class="section-label">SPARQL</div><pre>${Format.escapeHtml(value.sparql_query)}</pre></div>`);
        }
        if (!value.sql_query && !value.sparql_query) {
          parts.push(`<div class="section"><div class="section-label">Query</div><pre>No single SQL/SPARQL query attached to this aggregate row.</pre></div>`);
        }
        return parts.join('');
      },

      showTooltip(event, rowLabel, datasetName, value, rowAllowed, compareNote = null) {
        this.dom.tooltip.innerHTML = this.tooltipHtml(rowLabel, datasetName, value, rowAllowed, compareNote);
        this.dom.tooltip.classList.add('visible');
        this.placeTooltip(event);
      },

      hideTooltip() {
        this.dom.tooltip.classList.remove('visible');
      },

      placeTooltip(event) {
        const margin = 16;
        const rect = this.dom.tooltip.getBoundingClientRect();
        let left = event.clientX + 18;
        let top = event.clientY + 18;
        if (left + rect.width + margin > window.innerWidth) left = window.innerWidth - rect.width - margin;
        if (top + rect.height + margin > window.innerHeight) top = window.innerHeight - rect.height - margin;
        this.dom.tooltip.style.left = `${Math.max(margin, left)}px`;
        this.dom.tooltip.style.top = `${Math.max(margin, top)}px`;
      },

      renderStatePanels() {
        const legendDatasets = this.select.datasetsFromActiveSources();
        this.view.sourceCards.forEach(({ card, checkbox, suffixInput, baselineInput }, sourceId) => {
          const source = this.sourceById.get(sourceId);
          const active = this.state.activeSources.has(sourceId);
          checkbox.checked = active;
          baselineInput.checked = active && sourceId === this.state.baselineSourceId;
          baselineInput.disabled = !active;
          if (suffixInput !== document.activeElement) {
            suffixInput.value = this.state.sourceSuffix.get(sourceId) || source?.default_suffix || '';
          }
          card.style.opacity = active ? '1' : '0.62';
        });

        this.view.systemButtons.forEach((button, system) => {
          const systemSources = this.select.sourcesForSystem(system);
          const allActive = systemSources.every(source => this.state.activeSources.has(source.id));
          const anyActive = systemSources.some(source => this.state.activeSources.has(source.id));
          button.classList.toggle('active', allActive);
          button.classList.toggle('inactive', !anyActive);
          button.title = allActive ? `Click to hide all ${system} sources` : `Click to show all ${system} sources`;
        });

        this.data.datasets.forEach(dataset => {
          const active = this.state.activeDatasets.has(dataset.id);
          dataset._chip.classList.toggle('active', active);
          dataset._chip.classList.toggle('inactive', !active);
          dataset._chip.title = `${this.display.datasetName(dataset)} · ${this.display.datasetSourceLabel(dataset)} · ${active ? 'Click to hide dataset' : 'Click to show dataset'}`;
          dataset._chip.innerHTML = `
            <div class="short">${this.display.datasetShort(dataset)}</div>
            <div class="full">${this.display.datasetName(dataset)}</div>
            <div class="meta">${dataset.variant} · ${this.sourceById.get(dataset.source_id)?.method ?? ''}</div>
          `;
        });
        this.dom.legend.replaceChildren(...legendDatasets.map(dataset => dataset._chip));

        this.view.familyButtons.forEach((button, family) => {
          const familyDatasets = this.select.datasetsForFamily(family);
          const allActive = familyDatasets.every(dataset => this.state.activeDatasets.has(dataset.id));
          const anyActive = familyDatasets.some(dataset => this.state.activeDatasets.has(dataset.id));
          button.classList.toggle('active', allActive);
          button.classList.toggle('inactive', !anyActive);
          button.title = allActive ? `Click to hide all ${family} datasets` : `Click to show all ${family} datasets`;
        });

        this.view.typeButtons.forEach((button, variant) => {
          const variantDatasets = this.select.datasetsForType(variant);
          const allActive = variantDatasets.every(dataset => this.state.activeDatasets.has(dataset.id));
          const anyActive = variantDatasets.some(dataset => this.state.activeDatasets.has(dataset.id));
          button.classList.toggle('active', allActive);
          button.classList.toggle('inactive', !anyActive);
          button.title = allActive ? `Click to hide all ${variant} datasets` : `Click to show all ${variant} datasets`;
        });

        this.view.queryGroupButtons.forEach((button, group) => {
          const active = this.state.activeQueryGroups.has(group);
          button.classList.toggle('active', active);
          button.classList.toggle('inactive', !active);
          button.title = active ? `Click to hide ${group} rows` : `Click to show ${group} rows`;
        });
      },

      renderMatrix(datasets, visibleRows, compareContext) {
        const thead = document.createElement('thead');
        const hr = document.createElement('tr');
        ['Query', 'Type', ...datasets.map(d => this.display.datasetShort(d))].forEach((label, idx) => {
          const th = document.createElement('th');
          const wrap = document.createElement('span');
          wrap.className = 'th-inner';
          const text = document.createElement('span');
          text.textContent = label;
          wrap.appendChild(text);
          const sortButton = document.createElement('button');
          sortButton.type = 'button';
          const sortKey = idx === 0 ? 'label' : idx === 1 ? 'type' : `dataset:${datasets[idx - 2].id}`;
          sortButton.className = 'sort-button';
          sortButton.textContent = this.logic.sortIconFor(sortKey);
          sortButton.classList.toggle('active', this.state.rowSort.key === sortKey);
          sortButton.title = `Sort rows by ${label}`;
          sortButton.addEventListener('click', event => {
            event.stopPropagation();
            this.logic.toggleRowSort(sortKey);
            this.render();
          });
          wrap.appendChild(sortButton);
          th.appendChild(wrap);

          if (idx >= 2) {
            const dataset = datasets[idx - 2];
            th.title = `${this.display.datasetName(dataset)} · ${this.display.datasetSourceLabel(dataset)} · drag left/right to reorder`;
            th.classList.add('dataset-col');
            th.draggable = true;
            th.addEventListener('dragstart', event => {
              this.state.draggedDatasetId = dataset.id;
              th.classList.add('dragging');
              if (event.dataTransfer) {
                event.dataTransfer.effectAllowed = 'move';
                event.dataTransfer.setData('text/plain', dataset.id);
              }
            });
            th.addEventListener('dragend', () => {
              this.state.draggedDatasetId = null;
              th.classList.remove('dragging');
              thead.querySelectorAll('.drag-over').forEach(el => el.classList.remove('drag-over'));
            });
            th.addEventListener('dragover', event => {
              if (!this.state.draggedDatasetId || this.state.draggedDatasetId === dataset.id) return;
              event.preventDefault();
              th.classList.add('drag-over');
              if (event.dataTransfer) event.dataTransfer.dropEffect = 'move';
            });
            th.addEventListener('dragleave', () => th.classList.remove('drag-over'));
            th.addEventListener('drop', event => {
              event.preventDefault();
              th.classList.remove('drag-over');
              this.logic.reorderManualDataset(this.state.draggedDatasetId, dataset.id);
              this.render();
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
            const dataset = datasets[index];
            const datasetName = this.display.datasetName(dataset);
            const rowAllowed = this.logic.cellMatchesActiveQueryGroups(value);
            let compareNote = null;

            if (!value || !rowAllowed) {
              td.textContent = '—';
              td.className = 'blank';
            } else if (typeof value.f1 === 'number') {
              td.textContent = value.f1.toFixed(2);
              td.className = 'heat';
              td.style.background = Heat.colorForValue(value.f1);
              td.style.color = Heat.textColorForValue(value.f1);
              if (compareContext && !compareContext.error) {
                const pair = compareContext.pairs.get(dataset.id);
                if (pair && pair.role === 'a') {
                  const otherValue = row.values[pair.other.index];
                  const otherAllowed = this.logic.cellMatchesActiveQueryGroups(otherValue);
                  const relation = Compare.values(value, rowAllowed, otherValue, otherAllowed);
                  if (relation) {
                    const deltaClass = Compare.relationClass(relation);
                    if (deltaClass) td.classList.add(deltaClass);
                    compareNote = `vs ${this.display.datasetName(pair.other.dataset)}: ${Compare.relationText(relation, otherValue)}`;
                  }
                }
              }
            } else {
              td.textContent = value.f1_raw;
              td.className = 'nan';
              if (compareContext && !compareContext.error) {
                const pair = compareContext.pairs.get(dataset.id);
                if (pair && pair.role === 'a') {
                  const otherValue = row.values[pair.other.index];
                  const otherAllowed = this.logic.cellMatchesActiveQueryGroups(otherValue);
                  const relation = Compare.values(value, rowAllowed, otherValue, otherAllowed);
                  if (relation) {
                    const deltaClass = Compare.relationClass(relation);
                    if (deltaClass) td.classList.add(deltaClass);
                    compareNote = `vs ${this.display.datasetName(pair.other.dataset)}: ${Compare.relationText(relation, otherValue)}`;
                  }
                }
              }
            }

            td.addEventListener('mouseenter', event => this.showTooltip(event, row.label, datasetName, value, rowAllowed, compareNote));
            td.addEventListener('mousemove', event => this.placeTooltip(event));
            td.addEventListener('mouseleave', () => this.hideTooltip());
            tr.appendChild(td);
          });

          tbody.appendChild(tr);
        });

        this.dom.matrix.replaceChildren(thead, tbody);
      },

      renderCompareReport(compareContext) {
        this.dom.compareList.replaceChildren();
        if (!compareContext) {
          this.dom.compareReport.classList.add('hidden');
          return;
        }
        if (compareContext.error) {
          this.dom.compareReport.classList.remove('hidden');
          this.dom.compareSummary.textContent = compareContext.error;
          return;
        }

        this.dom.compareReport.classList.remove('hidden');
        this.dom.compareSummary.textContent = `Baseline ${this.display.sourceLabel(compareContext.sourceA)} compared against ${this.display.sourceLabel(compareContext.sourceB)}. Highlights are applied to the baseline source, and NaN is treated as the worst result.`;
        if (compareContext.report.length === 0) {
          const empty = document.createElement('div');
          empty.className = 'compare-item';
          empty.textContent = 'No matching dataset pairs with comparable All (AVG) scores in the current selection.';
          this.dom.compareList.appendChild(empty);
          return;
        }

        compareContext.report.slice(0, 24).forEach(item => {
          const row = document.createElement('div');
          row.className = 'compare-item';
          const deltaClass = item.relation.kind === 'left-better-nan'
            ? 'pos'
            : item.relation.kind === 'left-worse-nan'
            ? 'neg'
            : item.relation.kind === 'numeric' && item.relation.delta > 1e-9
            ? 'pos'
            : item.relation.kind === 'numeric' && item.relation.delta < -1e-9
            ? 'neg'
            : 'zero';
          row.innerHTML = `
            <div>${item.baseName}: ${Format.formatMetric(item.leftValue)} vs ${Format.formatMetric(item.rightValue)}</div>
            <div class="compare-delta ${deltaClass}">${Compare.relationText(item.relation, { f1_raw: item.rightValue })}</div>
          `;
          this.dom.compareList.appendChild(row);
        });
      },

      render() {
        const datasets = this.select.visibleDatasets();
        const rows = this.logic.buildRows(datasets);
        const visibleRows = this.logic.filteredRows(rows, datasets);
        const compareContext = Compare.buildContext(this, datasets);
        this.logic.updateCurrentRowOrder(visibleRows);
        this.renderStatePanels();
        this.renderMatrix(datasets, visibleRows, compareContext);
        const scopeLabel = this.data.run_path ? `anchor ${this.data.run_path}` : `discover root ${this.data.discover_root}`;
        this.dom.footer.textContent = `${visibleRows.length} rows shown · ${datasets.length}/${this.data.datasets.length} dataset columns active · ${this.state.activeSources.size}/${this.data.sources.length} sources active · ${scopeLabel}`;
        this.renderCompareReport(compareContext);
      },
    };

    try {
      App.init(DATA);
    } catch (err) {
      document.body.innerHTML = `<pre style="padding:24px;color:#8a3b12">${String(err)}</pre>`;
    }
    """


def _html_refactored(payload_json: str) -> str:
    template = _base_html("__DATA_PLACEHOLDER__")
    script_block = f"<script>\n{_javascript_refactored()}\n  </script>"
    template = re.sub(r"<script>[\s\S]*?</script>", lambda _m: script_block, template, count=1)
    return template.replace("__DATA_PLACEHOLDER__", payload_json)


def main() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[2]
    run_path = args.run_path.resolve() if args.run_path else None
    if run_path is not None and (not run_path.exists() or not run_path.is_dir()):
        raise SystemExit(f"Run path not found or not a directory: {run_path}")

    discover_root = (args.discover_root or (run_path.parents[1] if run_path else (repo_root / "outputs"))).resolve()
    default_output_dir = discover_root / "summary" / "rodi_f1_site_refactored"
    output_dir = (args.output_dir or default_output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    payload = build_payload(run_path, discover_root)
    payload_json = json.dumps(payload, indent=2)
    (output_dir / "data.json").write_text(payload_json, encoding="utf-8")
    (output_dir / "index.html").write_text(_html_refactored(payload_json), encoding="utf-8")

    print(f"Wrote {output_dir / 'data.json'}")
    print(f"Wrote {output_dir / 'index.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
