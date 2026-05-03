from __future__ import annotations

import argparse
import re
from pathlib import Path


NAME_LINE_RE = re.compile(r"(?m)^(?P<prefix>\s*name\s*=\s*)(?P<value>.+?)(?P<suffix>\s*)$")
QID_PREFIX_RE = re.compile(r"^(Q\d+(?:_pg_compatible)?)(\b.*)$")


def normalize_qpair_name(file_path: Path) -> bool:
    content = file_path.read_text(encoding="utf-8")
    match = NAME_LINE_RE.search(content)
    if not match:
        return False

    qid = file_path.stem
    current_value = match.group("value").strip()
    qid_match = QID_PREFIX_RE.match(current_value)
    if not qid_match:
        return False

    current_qid, remainder = qid_match.groups()
    if current_qid == qid:
        return False

    replacement = f"{match.group('prefix')}{qid}{remainder}{match.group('suffix')}"
    patched = content[: match.start()] + replacement + content[match.end() :]
    file_path.write_text(patched, encoding="utf-8")
    return True


def normalize_dataset_queries(dataset_root: Path) -> int:
    updated = 0
    for qpair_file in sorted(dataset_root.glob("*/queries/*.qpair")):
        if normalize_qpair_name(qpair_file):
            updated += 1
    return updated


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Normalize downloaded RODI qpair names so the name= prefix matches the filename."
    )
    parser.add_argument("datasets_dir", type=Path, help="Path to the local datasets/rodi directory")
    args = parser.parse_args()

    updated = normalize_dataset_queries(args.datasets_dir)
    print(f"Normalized qpair names: {updated}")


if __name__ == "__main__":
    main()
