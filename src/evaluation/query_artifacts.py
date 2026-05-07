from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List

from evaluation.common import EvaluationRunConfig


def _display_qid(qid: str) -> str:
    match = re.match(r"(Q\d+)", qid, re.IGNORECASE)
    return match.group(1) if match else qid


def _normalize_query_label(qid: str, label: str | None) -> str:
    display_qid = _display_qid(qid)
    if label is None:
        return display_qid

    normalized = label.strip()
    if not normalized:
        return display_qid

    nested_pattern = re.compile(
        rf"^{re.escape(display_qid)}\s*\(\s*{re.escape(display_qid)}\s*\((.+)\)\s*\)$",
        re.IGNORECASE,
    )
    while True:
        match = nested_pattern.match(normalized)
        if not match:
            break
        normalized = f"{display_qid} ({match.group(1).strip()})"

    if (
        normalized == display_qid
        or normalized.startswith(f"{display_qid} (")
        or normalized.startswith(f"{display_qid}(")
    ):
        return normalized

    if re.match(r"^Q\d+\s*\(.+\)$", normalized, re.IGNORECASE):
        return normalized

    return f"{display_qid} ({normalized})"


def extract_sparql_from_qpair(file_path: Path) -> str:
    content = file_path.read_text(encoding="utf-8")
    match = re.search(r"sparql\s*=\s*(.*?)(\ncategories=|\n\s*$)", content, re.DOTALL)
    if not match:
        match = re.search(r"sparql.*=.*?(pre.*)=?", content, re.DOTALL)
    if not match:
        raise ValueError(f"Could not extract SPARQL from {file_path}")
    sparql = match.group(1).strip()
    sparql = sparql.replace("\\n", "\n").replace("\\", "").strip()
    if "}" in sparql and not sparql.endswith("}"):
        sparql = sparql[: sparql.rindex("}") + 1]
    return sparql


def extract_sql_from_qpair(file_path: Path) -> str:
    content = file_path.read_text(encoding="utf-8")
    match = re.search(r"sql\s*=\s*(.*?)(?=\n\s*sparql\s*=|\Z)", content, re.DOTALL)
    if not match:
        raise ValueError(f"Could not extract SQL from {file_path}")
    sql = match.group(1).strip()
    sql = sql.replace("\\n", " ").replace("\\", "").strip()
    return sql


def extract_qpair_metadata(file_path: Path) -> Dict[str, Any]:
    content = file_path.read_text(encoding="utf-8")
    qid = file_path.stem
    label = None

    for pattern in [
        r"(?m)^\s*title\s*=\s*(.+?)\s*$",
        r"(?m)^\s*name\s*=\s*(.+?)\s*$",
        r"(?m)^\s*description\s*=\s*(.+?)\s*$",
    ]:
        match = re.search(pattern, content)
        if match:
            raw = match.group(1).strip()
            if raw:
                label = raw
                break

    if label is None:
        for pattern in [
            r'(?m)^\s*#\s*"?(Q\d+\s*\(.+?\))"?\s*$',
            r'(?m)^\s*//\s*"?(Q\d+\s*\(.+?\))"?\s*$',
        ]:
            match = re.search(pattern, content)
            if match:
                label = match.group(1).strip()
                break

    label = _normalize_query_label(qid, label)

    categories: List[str] = []
    match = re.search(r"(?m)^\s*categories\s*=\s*(.+?)\s*$", content)
    if match:
        categories = [value.strip() for value in match.group(1).strip().split(",") if value.strip()]

    disabled = None
    match = re.search(r"(?m)^\s*disabled\s*=\s*(.*?)\s*$", content)
    if match:
        value = match.group(1).strip()
        disabled = value if value else True

    return {
        "qid": qid,
        "label": label,
        "categories": categories,
        "disabled": disabled,
    }


def archive_query_artifacts(cfg: EvaluationRunConfig, qpair_files: List[Path]) -> Path:
    cfg.queries_output_dir.mkdir(parents=True, exist_ok=True)
    cfg.query_qpair_dir.mkdir(parents=True, exist_ok=True)
    cfg.query_sql_dir.mkdir(parents=True, exist_ok=True)
    cfg.query_sparql_dir.mkdir(parents=True, exist_ok=True)

    for target_dir in (cfg.query_qpair_dir, cfg.query_sql_dir, cfg.query_sparql_dir):
        for existing in target_dir.iterdir():
            if existing.is_file():
                existing.unlink()

    manifest: List[Dict[str, Any]] = []
    for qpair_file in qpair_files:
        metadata = extract_qpair_metadata(qpair_file)
        sql_query = extract_sql_from_qpair(qpair_file)
        sparql_query = extract_sparql_from_qpair(qpair_file)

        raw_name = qpair_file.name
        qid = metadata["qid"]
        sql_file = cfg.query_sql_dir / f"{qid}.sql"
        sparql_file = cfg.query_sparql_dir / f"{qid}.rq"

        shutil.copy2(qpair_file, cfg.query_qpair_dir / raw_name)
        sql_file.write_text(sql_query + "\n", encoding="utf-8")
        sparql_file.write_text(sparql_query + "\n", encoding="utf-8")

        manifest.append(
            {
                "id": qid,
                "label": metadata["label"],
                "categories": metadata["categories"],
                "disabled": metadata["disabled"],
                "qpair_file": raw_name,
                "sql_file": sql_file.name,
                "sparql_file": sparql_file.name,
                "sql_query": sql_query,
                "sparql_query": sparql_query,
            }
        )

    cfg.query_manifest_file.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return cfg.queries_output_dir
