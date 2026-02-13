import json

##############################
# Data Retriever
##############################
def print_entity_continuity(
    table_name: str,
    input_path: str = "src/utils_io/DB_json_out/Semantic_Memory/SE_Entity_Continuity_WithData.json"
):
    """
    Reads the SE_Entity_Continuity_WithData.json and prints only the continuity
    information for one entity/table.
    """

    # Load file
    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    entities = data.get("entities", {})

    # Retrieve the requested table's continuity
    result = entities.get(table_name)

    if not result:
        print(f"No continuity data found for entity '{table_name}'.")
        return

    # Pretty-print the result
    print(json.dumps(result, indent=2))
