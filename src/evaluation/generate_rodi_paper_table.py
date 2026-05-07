from __future__ import annotations

import argparse
import csv
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional


@dataclass(frozen=True)
class PaperRow:
    group: str
    scenario: str
    baselines: tuple[Optional[float], ...]
    dataset_name: Optional[str]


BASELINE_HEADERS = ("B.OX", "IncM.", "MIRR.", "COMA", "D2RQ")
MPBOOT_HEADER = "MPBootLLM (Haiku)"
TABULAR_PATTERN = re.compile(r"^All \(AVG\)\|([^|]+)\|")


PAPER_ROWS: tuple[PaperRow, ...] = (
    PaperRow("Conference domain, adjusted naming", "CMT", (0.76, 0.45, 0.28, 0.48, 0.31), "cmt_renamed"),
    PaperRow("Conference domain, adjusted naming", "Conference", (0.51, 0.53, 0.27, 0.36, 0.26), "conference_renamed"),
    PaperRow("Conference domain, adjusted naming", "SIGKDD", (0.86, 0.76, 0.30, 0.66, 0.38), "sigkdd_renamed"),
    PaperRow("Conference domain, restructured", "CMT", (0.41, 0.44, 0.17, 0.38, 0.14), "cmt_structured"),
    PaperRow("Conference domain, restructured", "Conference", (0.41, 0.41, 0.23, 0.31, 0.21), "conference_structured"),
    PaperRow("Conference domain, restructured", "SIGKDD", (0.52, 0.38, 0.11, 0.41, 0.28), "sigkdd_structured"),
    PaperRow("Conference domain, combined case", "SIGKDD", (0.48, 0.38, 0.11, 0.28, 0.21), "sigkdd_mixed"),
    PaperRow("Conference domain, missing FKs", "Conference", (0.33, 0.41, 0.17, 0.21, 0.18), "conference_nofks"),
    PaperRow("Conference domain, denormalized", "CMT", (0.44, 0.40, 0.22, None, 0.20), "cmt_denormalized"),
    PaperRow("Geodata", "Classic Rel.", (0.13, 0.08, None, None, 0.06), "mondial_rel"),
    PaperRow("Oil & gas domain", "User Queries", (0.00, 0.00, 0.00, None, 0.00), None),
    PaperRow("Oil & gas domain", "Atomic", (0.14, 0.12, 0.00, 0.02, 0.08), "npd_atomic_tests"),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate paper-style markdown/CSV/LaTeX summary tables from RODI tabular evaluation results."
    )
    parser.add_argument(
        "run_path",
        type=Path,
        help="Batch directory under outputs/<model>/<batch_timestamp>",
    )
    parser.add_argument(
        "--model-label",
        default="Haiku",
        help="Label to use in the MPBootLLM column header. Default: Haiku",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Directory where the summary files should be written. Defaults to <run_path>/summary",
    )
    return parser.parse_args()


def _fmt_num(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:.2f}"


def _best_indices(values: Iterable[Optional[float]]) -> set[int]:
    items = list(values)
    nums = [v for v in items if v is not None]
    if not nums:
        return set()
    max_value = max(nums)
    return {i for i, v in enumerate(items) if v is not None and abs(v - max_value) < 1e-12}


def _extract_rodi_f1(dataset_dir: Path) -> Optional[float]:
    tabular = dataset_dir / "evaluation" / "rodi" / "eval_rodi__tabular.txt"
    if not tabular.exists():
        return None
    for line in tabular.read_text(encoding="utf-8").splitlines():
        match = TABULAR_PATTERN.match(line.strip())
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                return None
    return None


def _collect_mpboot_scores(run_path: Path) -> dict[str, Optional[float]]:
    scores: dict[str, Optional[float]] = {}
    for row in PAPER_ROWS:
        if row.dataset_name and row.dataset_name not in scores:
            scores[row.dataset_name] = _extract_rodi_f1(run_path / row.dataset_name)
    return scores


def _md_table(rows: tuple[PaperRow, ...], scores: dict[str, Optional[float]], mpboot_header: str, run_path: Path) -> str:
    lines = [
        f"# RODI Results Table With {mpboot_header}",
        "",
        "Source batch:",
        f"- `{run_path}`",
        "",
        "Notes:",
        "- The added column is derived from each dataset's `evaluation/rodi/eval_rodi__tabular.txt`.",
        "- For a line `All (AVG)|F1|Precision|Recall`, the table uses the first numeric value, i.e. the F1 score.",
        "- Best value per row is shown in bold, matching the paper style.",
    ]
    current_group: Optional[str] = None
    headers = ("Scenario",) + BASELINE_HEADERS + (mpboot_header,)
    for row in rows:
        if row.group != current_group:
            lines.extend(
                [
                    "",
                    f"## {row.group}",
                    "",
                    "| " + " | ".join(headers) + " |",
                    "|" + " --- |" * len(headers),
                ]
            )
            current_group = row.group

        values = list(row.baselines) + [scores.get(row.dataset_name) if row.dataset_name else None]
        best = _best_indices(values)
        rendered = []
        for i, value in enumerate(values):
            text = _fmt_num(value)
            if i in best and value is not None:
                text = f"**{text}**"
            rendered.append(text)
        lines.append("| " + row.scenario + " | " + " | ".join(rendered) + " |")
    return "\n".join(lines) + "\n"


def _csv_table(rows: tuple[PaperRow, ...], scores: dict[str, Optional[float]], mpboot_header: str) -> list[list[str]]:
    csv_rows: list[list[str]] = [["group", "scenario", *BASELINE_HEADERS, mpboot_header]]
    for row in rows:
        mp = scores.get(row.dataset_name) if row.dataset_name else None
        csv_rows.append([row.group, row.scenario, *(_fmt_num(v) for v in row.baselines), _fmt_num(mp)])
    return csv_rows


def _latex_num(value: Optional[float], bold: bool) -> str:
    text = _fmt_num(value)
    return f"\\textbf{{{text}}}" if bold and value is not None else text


def _tex_escape(text: str) -> str:
    return text.replace("&", r"\&")


def _latex_table(rows: tuple[PaperRow, ...], scores: dict[str, Optional[float]], mpboot_header: str) -> str:
    lines = [
        r"\begin{table}[t]",
        r"\centering",
        r"\caption{Overall scores in default scenarios (scores based on average of per-test F-measure). Best numbers per scenario in bold print.}",
        r"\label{tab:rodi-mpbootllm-haiku}",
        r"\setlength{\tabcolsep}{5pt}",
        r"\renewcommand{\arraystretch}{1.15}",
        r"\begin{tabular}{lrrrrrr}",
        r"\hline",
        r"\textbf{Scenario} & \textbf{B.OX} & \textbf{IncM.} & \textbf{MIRR.} & \textbf{COMA} & \textbf{D2RQ} & \textbf{" + _tex_escape(mpboot_header) + r"} \\",
        r"\hline",
    ]
    current_group: Optional[str] = None
    for row in rows:
        if row.group != current_group:
            if current_group is not None:
                lines.append(r"\hline")
            lines.append(r"\multicolumn{7}{c}{\textbf{" + _tex_escape(row.group) + r"}} \\")
            current_group = row.group

        values = list(row.baselines) + [scores.get(row.dataset_name) if row.dataset_name else None]
        best = _best_indices(values)
        rendered = [_latex_num(v, i in best) for i, v in enumerate(values)]
        lines.append(_tex_escape(row.scenario) + " & " + " & ".join(rendered) + r" \\")
    lines.extend([r"\hline", r"\end{tabular}", r"\end{table}"])
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    run_path = args.run_path.resolve()
    if not run_path.exists() or not run_path.is_dir():
        raise SystemExit(f"Run path not found or not a directory: {run_path}")

    output_dir = (args.output_dir or (run_path / "summary")).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    mpboot_header = f"MPBootLLM ({args.model_label})"
    scores = _collect_mpboot_scores(run_path)

    md_path = output_dir / "rodi_table5__mpbootllm_haiku.md"
    csv_path = output_dir / "rodi_table5__mpbootllm_haiku.csv"
    tex_path = output_dir / "rodi_table5__mpbootllm_haiku.tex"

    md_path.write_text(_md_table(PAPER_ROWS, scores, mpboot_header, run_path), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerows(_csv_table(PAPER_ROWS, scores, mpboot_header))
    tex_path.write_text(_latex_table(PAPER_ROWS, scores, mpboot_header), encoding="utf-8")

    print(f"Wrote {md_path}")
    print(f"Wrote {csv_path}")
    print(f"Wrote {tex_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
