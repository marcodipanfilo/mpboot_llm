import os
import json
from dotenv import load_dotenv

load_dotenv()

from schema.continuity import SE_Data_Continuity

def main():
    db_folder = os.environ.get("MPBOOT_DB_JSON_OUT")
    dump_path = os.environ.get("MPBOOT_DUMP_PATH")  # optional

    if not db_folder:
        raise ValueError("MPBOOT_DB_JSON_OUT is not set")

    result = SE_Data_Continuity(
        db_folder=db_folder,
        dump_path=dump_path,
        max_rows=5,
        max_cols_per_table=10,
    )

    # IMPORTANT: set this to the exact filename you used in Colab
    out_file = os.path.join(db_folder, "SE_Data_Continuity_output.json")

    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"Wrote: {out_file}")

if __name__ == "__main__":
    main()
