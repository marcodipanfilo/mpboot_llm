import os
from dotenv import load_dotenv

load_dotenv()

from schema.semantic_units_builders import (
    extract_fk_pk_relations,
    build_entity_continuity_with_data,
)

def main():
    base = os.environ.get("MPBOOT_DB_JSON_OUT")
    if not base:
        raise ValueError("MPBOOT_DB_JSON_OUT is not set")

    input_path = os.path.join(base, "SE_Data_Continuity_output.json")

    out_links = os.path.join(base, "Semantic_Memory", "Unit_Links_Pairs.json")
    out_cont  = os.path.join(base, "Semantic_Memory", "SE_Entity_Continuity_WithData.json")

    extract_fk_pk_relations(input_path=input_path, output_path=out_links)
    build_entity_continuity_with_data(input_path=input_path, output_path=out_cont, max_examples_per_link=5)

    print("Done.")

if __name__ == "__main__":
    main()
