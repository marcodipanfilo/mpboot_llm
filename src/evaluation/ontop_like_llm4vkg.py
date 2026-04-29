from __future__ import annotations

from collections import defaultdict
import json
import math
import re
import subprocess
import time
from pathlib import Path
from threading import Thread
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import psycopg2
import requests

from evaluation.common import EvaluationRunConfig


def create_properties_file(file_path: Path, *, cfg: EvaluationRunConfig) -> None:
    jdbc = (
        f"jdbc.url=jdbc:postgresql://{cfg.db_host}:{cfg.db_port}/{cfg.db_name}"
        f"?options=-c%20search_path%3D{quote(cfg.dataset_name)}\n"
        f"jdbc.driver=org.postgresql.Driver\n"
        f"jdbc.password={cfg.db_password}\n"
        f"jdbc.user={cfg.db_user}\n"
    )
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(jdbc, encoding="utf-8")


def convert_ttl_to_obda(input_ttl: Path, output_obda: Path, ontop_dir: Path) -> None:
    ontop_exe = ontop_dir / "ontop"
    if not ontop_exe.exists():
        raise FileNotFoundError(f"Ontop executable not found: {ontop_exe}")

    cmd = [
        str(ontop_exe),
        "mapping",
        "to-obda",
        "-i",
        str(input_ttl.resolve()),
        "-o",
        str(output_obda.resolve()),
    ]
    print("[EVAL] Converting TTL to OBDA...")
    print("[CMD]", " ".join(cmd))
    result = subprocess.run(cmd, text=True, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(
            "Ontop mapping to-obda failed:\n"
            f"STDOUT:\n{result.stdout}\n\nSTDERR:\n{result.stderr}"
        )


def start_ontop_endpoint(
    mappings_file: Path,
    ontology_file: Path,
    property_file: Path,
    cfg: EvaluationRunConfig,
) -> tuple[subprocess.Popen, Path]:
    ontop_exe = cfg.ontop_dir / "ontop"
    if not ontop_exe.exists():
        raise FileNotFoundError(f"Ontop executable not found: {ontop_exe}")

    cmd = [
        str(ontop_exe),
        "endpoint",
        "-m",
        str(mappings_file.resolve()),
        "-t",
        str(ontology_file.resolve()),
        "-p",
        str(property_file.resolve()),
        "--port",
        str(cfg.ontop_port),
        "--cors-allowed-origins=*",
    ]
    print("[EVAL] Starting Ontop endpoint...")
    print("[CMD]", " ".join(cmd))
    log_file = cfg.eval_dir / "ontop_endpoint.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if proc.stdout is not None:
        def _pump_output() -> None:
            with log_file.open("a", encoding="utf-8") as handle:
                for line in proc.stdout:
                    handle.write(line)
                    handle.flush()

        Thread(target=_pump_output, daemon=True).start()
    return proc, log_file


def wait_for_endpoint(
    proc: subprocess.Popen,
    cfg: EvaluationRunConfig,
    log_file: Path,
    timeout_seconds: int = 30,
) -> None:
    endpoint_url = f"http://127.0.0.1:{cfg.ontop_port}/sparql"
    print(f"[EVAL] Waiting for Ontop endpoint on {endpoint_url} ...")
    start = time.time()
    while time.time() - start < timeout_seconds:
        if proc.poll() is not None:
            captured = log_file.read_text(encoding="utf-8") if log_file.exists() else ""
            raise RuntimeError(
                "Ontop endpoint terminated during startup.\n"
                f"Startup log: {log_file}\n{captured}"
            )

        try:
            response = requests.get(f"http://127.0.0.1:{cfg.ontop_port}", timeout=2)
            if response.status_code in {200, 404}:
                print("[EVAL] Ontop endpoint is reachable.")
                return
        except requests.RequestException:
            pass
        time.sleep(1)

    captured = log_file.read_text(encoding="utf-8") if log_file.exists() else ""
    raise TimeoutError(
        f"Ontop endpoint did not become reachable within {timeout_seconds}s.\n"
        f"Startup log: {log_file}\n{captured}"
    )


def stop_ontop_endpoint(proc: Optional[subprocess.Popen]) -> None:
    if proc is None:
        return
    if proc.poll() is None:
        print("[EVAL] Stopping Ontop endpoint...")
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
    print("[EVAL] Ontop endpoint stopped.")


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


def execute_sparql_query(cfg: EvaluationRunConfig, sparql_query: str) -> List[Any]:
    endpoint_url = f"http://127.0.0.1:{cfg.ontop_port}/sparql"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    payload = {"query": sparql_query}
    response = requests.post(endpoint_url, data=payload, headers=headers, timeout=60)

    if response.status_code == 400:
        return [None]
    if response.status_code == 500:
        if "is not supported yet!" in response.text:
            return [None]
        raise RuntimeError(f"Ontop endpoint returned 500:\n{response.text}")

    response.raise_for_status()
    data = response.json()

    vars_ = data["head"]["vars"]
    bindings = data["results"]["bindings"]

    results: List[Any] = []
    for binding in bindings:
        row = []
        for variable in vars_:
            if variable not in binding:
                row.append(None)
                continue
            value = binding[variable]
            if value["type"] == "literal":
                row.append(value["value"])
            elif value["type"] == "uri":
                row.append("##iri##")
            else:
                row.append(None)
        results.append(row[0] if len(row) == 1 else row)
    return results


def execute_sql_query(cfg: EvaluationRunConfig, sql_query: str) -> List[Any]:
    conn = psycopg2.connect(
        dbname=cfg.db_name,
        user=cfg.db_user,
        password=cfg.db_password,
        host=cfg.db_host,
        port=str(cfg.db_port),
    )
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(f"SET search_path TO {cfg.dataset_name}, public;")
                cur.execute(sql_query)
                rows = cur.fetchall()
    finally:
        conn.close()

    results: List[Any] = []
    for row in rows:
        values = [str(value) for value in row]
        results.append(values[0] if len(values) == 1 else values)
    return results


def _normalize_row(row: Any) -> tuple[str, ...]:
    if not isinstance(row, (list, tuple)):
        return (str(row),)
    filtered = [str(value) for value in row if value is not None and value != "##iri##"]
    return tuple(filtered)


def normalize_results(rows: List[Any]) -> List[str]:
    normalized = []
    for row in rows:
        norm = _normalize_row(row)
        if norm:
            normalized.append(str(norm))
    return normalized


def calculate_precision_recall_f1(res: List[Any], ref: List[Any]) -> tuple[float, float, float]:
    norm_res = normalize_results(res)
    norm_ref = normalize_results(ref)

    matched_res_num = sum(1 for row in norm_res if row in norm_ref)
    matched_ref_num = sum(1 for row in norm_ref if row in norm_res)

    precision = matched_res_num / len(norm_res) if norm_res else 0.0
    recall = matched_ref_num / len(norm_ref) if norm_ref else 1.0
    denom = precision + recall
    f1 = (2 * precision * recall / denom) if denom != 0 else float("nan")
    return precision, recall, f1


def _fmt_metric(value: Any) -> str:
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        return f"{value:.4f}"
    return str(value)


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

    if label is None:
        label = qid
    elif not label.startswith(qid):
        label = f"{qid} ({label})"

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


def rodi_f1(precision: float, recall: float) -> float:
    denom = precision + recall
    return (2 * precision * recall / denom) if denom != 0 else float("nan")


def write_rodi_like_tabular_report(json_results: List[Dict[str, Any]], output_file: Path) -> None:
    lines: List[str] = []

    valid_precisions: List[float] = []
    valid_recalls: List[float] = []
    valid_f1s: List[float] = []

    category_precision: Dict[str, List[float]] = defaultdict(list)
    category_recall: Dict[str, List[float]] = defaultdict(list)
    category_f1: Dict[str, List[float]] = defaultdict(list)

    print("\n[EVAL] Writing RODI-like report...\n")

    for item in json_results:
        precision = item["precision"]
        recall = item["recall"]
        f1 = item["f1"]

        line = f'{item["label"]}|{_fmt_metric(f1)}|{_fmt_metric(precision)}|{_fmt_metric(recall)}'
        lines.append(line)
        print(line)

        if not (isinstance(precision, float) and math.isnan(precision)):
            valid_precisions.append(precision)
        if not (isinstance(recall, float) and math.isnan(recall)):
            valid_recalls.append(recall)
        if not (isinstance(f1, float) and math.isnan(f1)):
            valid_f1s.append(f1)

        for category in item.get("categories", []):
            if not (isinstance(precision, float) and math.isnan(precision)):
                category_precision[category].append(precision)
            if not (isinstance(recall, float) and math.isnan(recall)):
                category_recall[category].append(recall)
            if not (isinstance(f1, float) and math.isnan(f1)):
                category_f1[category].append(f1)

    avg_precision = sum(valid_precisions) / len(valid_precisions) if valid_precisions else 0.0
    avg_recall = sum(valid_recalls) / len(valid_recalls) if valid_recalls else 0.0
    avg_f1 = rodi_f1(avg_precision, avg_recall)

    global_line = f"All (AVG)|{_fmt_metric(avg_f1)}|{_fmt_metric(avg_precision)}|{_fmt_metric(avg_recall)}"
    lines.append(global_line)
    print("\n[EVAL] GLOBAL AVERAGE")
    print(global_line)

    print("\n[EVAL] CATEGORY AVERAGES")
    for category in sorted(category_precision.keys()):
        cat_precision = (
            sum(category_precision[category]) / len(category_precision[category])
            if category_precision[category]
            else float("nan")
        )
        cat_recall = (
            sum(category_recall[category]) / len(category_recall[category])
            if category_recall[category]
            else float("nan")
        )
        cat_f1 = rodi_f1(cat_precision, cat_recall)
        cat_line = f"{category} (AVG)|{_fmt_metric(cat_f1)}|{_fmt_metric(cat_precision)}|{_fmt_metric(cat_recall)}"
        lines.append(cat_line)
        print(cat_line)

    output_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n[EVAL] Report written to: {output_file}")


def evaluate_with_ontop_like_llm4vkg(cfg: EvaluationRunConfig) -> None:
    print("\n====================")
    print("ONTOP EVALUATION START")
    print("====================\n")

    if not cfg.mapping_file.exists():
        raise FileNotFoundError(f"Mapping file not found: {cfg.mapping_file}")
    if not cfg.ontology_file.exists():
        raise FileNotFoundError(f"Ontology file not found: {cfg.ontology_file}")
    if not cfg.qpair_dir.exists():
        raise FileNotFoundError(f"Query folder not found: {cfg.qpair_dir}")
    if not cfg.ontop_dir.exists():
        raise FileNotFoundError(f"Ontop directory not found: {cfg.ontop_dir}")

    cfg.output_dir.mkdir(parents=True, exist_ok=True)

    print("[EVAL] Creating JDBC properties file...")
    create_properties_file(cfg.ontop_properties_file, cfg=cfg)
    print(f"[EVAL] JDBC properties written to: {cfg.ontop_properties_file}")

    convert_ttl_to_obda(cfg.mapping_file, cfg.obda_file, cfg.ontop_dir)

    proc: Optional[subprocess.Popen] = None
    try:
        proc, log_file = start_ontop_endpoint(cfg.obda_file, cfg.ontology_file, cfg.ontop_properties_file, cfg)
        wait_for_endpoint(proc, cfg, log_file)

        json_results: List[Dict[str, Any]] = []
        qpair_files = sorted(path for path in cfg.qpair_dir.iterdir() if path.is_file() and path.suffix == ".qpair")
        print(f"[EVAL] Evaluating {len(qpair_files)} qpair files from {cfg.qpair_dir} ...")

        for qpair_file in qpair_files:
            metadata = extract_qpair_metadata(qpair_file)
            if metadata.get("disabled"):
                print(f"[EVAL] Skipping {qpair_file.name}: disabled ({metadata['disabled']})")
                continue

            sql_query = extract_sql_from_qpair(qpair_file)
            sparql_query = extract_sparql_from_qpair(qpair_file)

            try:
                sql_results = execute_sql_query(cfg, sql_query)
            except psycopg2.errors.UndefinedTable:
                print(f"[EVAL] Skipping {qpair_file.name}: undefined table")
                continue
            except psycopg2.errors.UndefinedColumn:
                print(f"[EVAL] Skipping {qpair_file.name}: undefined column")
                continue

            sparql_results = execute_sparql_query(cfg, sparql_query)
            precision, recall, f1 = calculate_precision_recall_f1(sparql_results, sql_results)
            print(f"[EVAL] {metadata['label']}: F1={f1:.4f} P={precision:.4f} R={recall:.4f}")

            json_results.append(
                {
                    "id": f"{cfg.dataset_name}.{qpair_file.name}",
                    "label": metadata["label"],
                    "qpair_file": qpair_file.name,
                    "categories": metadata["categories"],
                    "sparql_query": sparql_query,
                    "sparql_results": sparql_results,
                    "sql_query": sql_query,
                    "sql_results": sql_results,
                    "precision": precision,
                    "recall": recall,
                    "f1": f1,
                }
            )

        valid_precisions = [
            item["precision"]
            for item in json_results
            if not (isinstance(item["precision"], float) and math.isnan(item["precision"]))
        ]
        valid_recalls = [
            item["recall"]
            for item in json_results
            if not (isinstance(item["recall"], float) and math.isnan(item["recall"]))
        ]

        avg_precision = sum(valid_precisions) / len(valid_precisions) if valid_precisions else 0.0
        avg_recall = sum(valid_recalls) / len(valid_recalls) if valid_recalls else 0.0
        avg_f1 = rodi_f1(avg_precision, avg_recall)

        cfg.eval_ontop_metrics_file.write_text(
            json.dumps(json_results, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        cfg.eval_ontop_summary_file.write_text(
            f"Average F1 Score: {avg_f1:.4f}\n"
            f"Average Precision: {avg_precision:.4f}\n"
            f"Average Recall: {avg_recall:.4f}\n",
            encoding="utf-8",
        )
        write_rodi_like_tabular_report(json_results, cfg.eval_ontop_tabular_file)

        print(f"[EVAL] Average F1 Score: {avg_f1:.4f}")
        print(f"[EVAL] Average Precision: {avg_precision:.4f}")
        print(f"[EVAL] Average Recall: {avg_recall:.4f}")
        print(f"[EVAL] Metrics JSON: {cfg.eval_ontop_metrics_file}")
        print(f"[EVAL] F1 summary:   {cfg.eval_ontop_summary_file}")
        print(f"[EVAL] Tabular report:{cfg.eval_ontop_tabular_file}")
    finally:
        stop_ontop_endpoint(proc)
