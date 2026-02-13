import json


##############################
# Pattern Retriever
##############################
def get_table_pattern(
    table_name: str,
    discovery_json_path: str = "src/utils_io/DB_json_out/Semantic_Memory/Patterns_discovery.json"
) -> str:
    """
    Returns a compact pattern string for a table, enriched with SH info.

    Base pattern is one of: "SE", "SEw", "SR", "SRR", "None".
    If the table is an SH child in discovery_json_path, the string becomes:
      "<pattern> + SH(child_of=Parent1,Parent2)"
    or, if no base pattern is found:
      "SH(child_of=Parent1,Parent2)"
    """

    with open(discovery_json_path, "r", encoding="utf-8") as f:
        discovery_result = json.load(f)

    t = table_name.strip()

    if t in discovery_result.get("SE_tables", []):
        base = "SE"
    elif t in discovery_result.get("SEw_tables", []):
        base = "SEw"
    elif t in discovery_result.get("SR_tables", []):
        base = "SR"
    elif t in discovery_result.get("SRR_tables", []):
        base = "SRR"
    else:
        base = "None"

    parents = []
    for rel in discovery_result.get("SH_relations", []):
        if rel.get("child_table") == t:
            pt = rel.get("parent_table")
            if pt and pt not in parents:
                parents.append(pt)

    if parents:
        parents_str = ",".join(sorted(parents))
        sh_part = f"SH(child_of={parents_str})"
        if base != "None":
            return f"{base} + {sh_part}"
        else:
            return sh_part
    else:
        return base
