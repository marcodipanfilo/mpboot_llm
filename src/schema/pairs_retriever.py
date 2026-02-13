import json

##############################
# Pairs Retriever
##############################
def find_links_for_table(
    links_json_path: str,
    table_name: str
):
    """
    Reads the relations JSON file and prints all links
    where the given table is the SOURCE (no symmetric duplicates).
    """
    with open(links_json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    relations = data.get("relations", [])
    table_name = table_name.strip()

    found = False

    for rel in relations:
        src = rel.get("source_entity")
        tgt = rel.get("target_entity")
        fc  = rel.get("from_column")
        tc  = rel.get("to_column")
        vb  = rel.get("via_bridge")

        if src != table_name:
            continue

        found = True

        if vb is None:
            print(f"{src} → {tgt}   ({fc}  →  {tc})")
        else:
            print(
                f"{src} → {tgt}   via {vb.get('bridge_table')} "
                f"({vb.get('bridge_fk_from_source')}  →  {vb.get('bridge_fk_to_target')})"
            )

    if not found:
        print(f"No links found for table '{table_name}'.")
