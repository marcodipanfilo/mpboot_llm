from __future__ import annotations

import difflib
from pathlib import Path


def build_tabular_diff(
    file_a: Path,
    file_b: Path,
    *,
    label_a: str = "Ontop",
    label_b: str = "RODI",
) -> str:
    if not file_a.exists():
        raise FileNotFoundError(f"Missing {label_a} file: {file_a}")
    if not file_b.exists():
        raise FileNotFoundError(f"Missing {label_b} file: {file_b}")

    lines_a = [line.strip() for line in file_a.read_text(encoding="utf-8").splitlines()]
    lines_b = [line.strip() for line in file_b.read_text(encoding="utf-8").splitlines()]

    if lines_a == lines_b:
        return ""

    diff = difflib.unified_diff(
        lines_a,
        lines_b,
        fromfile=str(file_a),
        tofile=str(file_b),
        lineterm="",
    )
    return "\n".join(diff)


def compare_tabular_files(
    file_a: Path,
    file_b: Path,
    *,
    label_a: str = "Ontop",
    label_b: str = "RODI",
) -> None:
    diff_text = build_tabular_diff(file_a, file_b, label_a=label_a, label_b=label_b)
    if not diff_text:
        print("[CHECK] Tabular files are identical.")
        return
    raise RuntimeError("[CHECK] Tabular files differ:\n\n" + diff_text)

