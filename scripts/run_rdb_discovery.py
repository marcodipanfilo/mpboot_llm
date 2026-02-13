import os
import json
from schema.patterns import discover_SE_SR_SRR_SH_json_v2

def main():
    db_folder = "src/utils_io/DB_json_out/Semantic_Memory/"
    if not db_folder:
        raise ValueError("Set MPBOOT_DB_JSON_OUT env var to your DB_json_out folder path")

    result = discover_SE_SR_SRR_SH_json_v2(db_folder=db_folder)

    out_path = os.path.join(db_folder, "Patterns_discovery.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"Wrote: {out_path}")

if __name__ == "__main__":
    main()
