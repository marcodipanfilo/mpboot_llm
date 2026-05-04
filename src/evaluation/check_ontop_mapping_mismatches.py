from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional


ROOT_DIR = Path(__file__).resolve().parents[2]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

try:
    from parsers import ontology_explorer as oe  # noqa: E402
    from evaluation.common import EvaluationRunConfig  # noqa: E402
except ModuleNotFoundError as exc:
    if exc.name == "owlready2":
        raise SystemExit(
            "Missing Python dependency 'owlready2'. Run scripts/bootstrap.sh first so the repo venv is ready."
        )
    raise


@dataclass
class Mismatch:
    predicate_token: str
    predicate_iri: str
    line_no: int
    block_start: int
    block_end: int
    terminator: str
    reason_lines: List[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check R2RML mappings for ontology property kind mismatches that make Ontop fail."
    )
    parser.add_argument(
        "run_path",
        type=Path,
        help="Dataset archive directory or batch directory under outputs/<model>/<batch_timestamp>",
    )
    parser.add_argument("--dataset", action="append", default=[], help="Only process a specific dataset name")
    parser.add_argument(
        "--remove",
        action="store_true",
        help="Remove predicateObjectMap blocks whose object/data-vs-literal/object usage contradicts the ontology",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Write the sanitized TTL to a different file instead of updating in place. Only valid for a single dataset.",
    )
    return parser.parse_args()


def _prefixes_from_text(text: str) -> Dict[str, str]:
    prefixes: Dict[str, str] = {}
    for match in re.finditer(r"@prefix\s+([A-Za-z_][\w-]*|):\s*<([^>]+)>\s*\.", text):
        prefixes[match.group(1)] = match.group(2)
    return prefixes


def _expand_predicate(token: str, prefixes: Dict[str, str]) -> Optional[str]:
    token = token.strip()
    if token.startswith("<") and token.endswith(">"):
        return token[1:-1]
    if ":" in token:
        prefix, local = token.split(":", 1)
        if prefix in prefixes:
            return prefixes[prefix] + local
    return None


def _property_kinds(ontology_file: Path) -> tuple[set[str], set[str]]:
    os.environ["MPBOOT_ONTOLOGY_PATH"] = str(ontology_file.resolve())
    oe._OWL_ONTO = None
    oe._BASE_IRI = None
    oe._LOADED_FROM = None
    oe._ALL_ONTOLOGY_NAMESPACES.clear()
    onto = oe._load_ontology()
    meta = oe._prop_meta(onto)
    return (
        {item["property_iri"] for item in meta["object_properties"]},
        {item["property_iri"] for item in meta["data_properties"]},
    )


def _scan_pom_blocks(text: str, object_property_iris: set[str], data_property_iris: set[str]) -> List[Mismatch]:
    prefixes = _prefixes_from_text(text)
    lines = text.splitlines()
    mismatches: List[Mismatch] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if "rr:predicateObjectMap [" not in line:
            i += 1
            continue

        start = i
        depth = 0
        predicate_token: Optional[str] = None
        predicate_iri: Optional[str] = None
        while i < len(lines):
            current = lines[i]
            depth += current.count("[")
            depth -= current.count("]")
            if predicate_token is None:
                match = re.search(r"rr:predicate\s+([^ ;]+)\s*;", current)
                if match:
                    predicate_token = match.group(1)
                    predicate_iri = _expand_predicate(predicate_token, prefixes)
            if depth == 0:
                break
            i += 1

        end = i
        block_text = "\n".join(lines[start : end + 1])
        block_is_literal = any(
            marker in block_text
            for marker in (
                "rr:column",
                "rr:datatype",
                "rr:language",
                "rr:languageMap",
                "rr:termType rr:Literal",
            )
        )
        block_is_object_like = any(
            marker in block_text
            for marker in (
                "rr:parentTriplesMap",
                "rr:joinCondition",
                "rr:termType rr:IRI",
                "rr:template",
            )
        )
        terminator = "."
        for j in range(end, start - 1, -1):
            stripped = lines[j].strip()
            if stripped.endswith("."):
                terminator = "."
                break
            if stripped.endswith(";"):
                terminator = ";"
                break

        if predicate_token and predicate_iri:
            if predicate_iri in object_property_iris and block_is_literal:
                mismatches.append(
                    Mismatch(
                        predicate_token=predicate_token,
                        predicate_iri=predicate_iri,
                        line_no=start + 1,
                        block_start=start,
                        block_end=end,
                        terminator=terminator,
                        reason_lines=[
                            f"{predicate_token} is declared as an object property",
                            "in the ontology, but this generated block mapped it as a literal data property.",
                        ],
                    )
                )
            elif predicate_iri in data_property_iris and block_is_object_like:
                mismatches.append(
                    Mismatch(
                        predicate_token=predicate_token,
                        predicate_iri=predicate_iri,
                        line_no=start + 1,
                        block_start=start,
                        block_end=end,
                        terminator=terminator,
                        reason_lines=[
                            f"{predicate_token} is declared as a data property",
                            "in the ontology, but this generated block mapped it as an object property.",
                        ],
                    )
                )
        i += 1
    return mismatches


def _apply_removals(text: str, mismatches: List[Mismatch]) -> str:
    lines = text.splitlines()
    for mm in sorted(mismatches, key=lambda item: item.block_start, reverse=True):
        indent = re.match(r"\s*", lines[mm.block_start]).group(0)
        commented_block = [
            f"{indent}# {line[len(indent):] if line.startswith(indent) else line}"
            for line in lines[mm.block_start : mm.block_end + 1]
        ]
        replacement = [
            *[f"{indent}# Auto-removed for Ontop: {line}" if idx == 0 else f"{indent}# {line}" for idx, line in enumerate(mm.reason_lines)],
            *commented_block,
            f"{indent}{mm.terminator}",
        ]
        lines[mm.block_start : mm.block_end + 1] = replacement
    return "\n".join(lines) + "\n"


def _is_dataset_run_dir(path: Path) -> bool:
    return (path / "run_metadata.json").exists() and (path / "mappings_r2rml.ttl").exists()


def _select_dataset_dirs(run_path: Path, selected_names: List[str]) -> List[Path]:
    run_path = run_path.resolve()
    if _is_dataset_run_dir(run_path):
        dataset_dirs = [run_path]
    else:
        dataset_dirs = sorted(path for path in run_path.iterdir() if path.is_dir() and _is_dataset_run_dir(path))

    if not dataset_dirs:
        raise FileNotFoundError(
            f"No dataset run directories found under {run_path}. Expected run_metadata.json and mappings_r2rml.ttl."
        )

    if not selected_names:
        return dataset_dirs

    selected = set(selected_names)
    filtered = [path for path in dataset_dirs if path.name in selected]
    missing = sorted(selected - {path.name for path in filtered})
    if missing:
        raise FileNotFoundError(f"Dataset(s) not found under {run_path}: {', '.join(missing)}")
    return filtered


def _process_dataset(dataset_dir: Path, *, remove: bool, output_file: Optional[Path]) -> int:
    mapping_file = (dataset_dir / "mappings_r2rml.ttl").resolve()
    ontology_file = (dataset_dir / "inputs" / "ontology.owl").resolve()

    if not mapping_file.exists():
        raise FileNotFoundError(f"Mapping file not found: {mapping_file}")
    if not ontology_file.exists():
        raise FileNotFoundError(f"Ontology file not found: {ontology_file}")

    print(f"\nDataset: {dataset_dir.name}")
    text = mapping_file.read_text(encoding="utf-8")
    try:
        object_property_iris, data_property_iris = _property_kinds(ontology_file)
    except Exception as exc:
        print(f"  Failed to load ontology for mismatch checking: {type(exc).__name__}: {exc}")
        return 1

    mismatches = _scan_pom_blocks(text, object_property_iris, data_property_iris)

    if not mismatches:
        print("  No ontology property-kind mismatches found.")
        return 0

    print("  Found Ontop-breaking property-kind mismatches:")
    for mm in mismatches:
        print(f"    line {mm.line_no}: {mm.predicate_token}  ({mm.predicate_iri})")

    if not remove:
        print("  Re-run with --remove to strip these literal predicateObjectMap blocks.")
        return 1

    sanitized = _apply_removals(text, mismatches)
    if output_file:
        destination = output_file.resolve()
    else:
        cfg = EvaluationRunConfig(
            dataset_dir=dataset_dir,
            dataset_name=dataset_dir.name,
            mapping_file=mapping_file,
            ontology_file=ontology_file,
            dump_file=(dataset_dir / "inputs" / "dump.sql").resolve(),
            output_dir=(dataset_dir / "evaluation").resolve(),
            rodi_root=Path("."),
            ontop_dir=Path("."),
            db_host="",
            db_port=0,
            db_name="",
            db_user="",
            db_password="",
            db_cmd=Path("."),
        )
        destination = (cfg.shared_output_dir / "mappings__r2rml_ontop_kind_sanitized.ttl").resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(sanitized, encoding="utf-8")
    print(f"  Wrote sanitized mapping to: {destination}")
    return 0


def main() -> int:
    args = parse_args()
    dataset_dirs = _select_dataset_dirs(args.run_path, args.dataset)

    if args.output and len(dataset_dirs) != 1:
        raise ValueError("--output can only be used when processing exactly one dataset.")

    overall_ok = True
    for dataset_dir in dataset_dirs:
        try:
            exit_code = _process_dataset(
                dataset_dir,
                remove=args.remove,
                output_file=args.output if len(dataset_dirs) == 1 else None,
            )
        except Exception as exc:
            print(f"\nDataset: {dataset_dir.name}")
            print(f"  Failed unexpectedly: {type(exc).__name__}: {exc}")
            exit_code = 1
        overall_ok = overall_ok and (exit_code == 0)

    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
