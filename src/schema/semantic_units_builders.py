import json
import os


def extract_fk_pk_relations(
    input_path: str = "src/utils_io/DB_json_out/SE_Data_Continuity_output.json",
    output_path: str = "src/utils_io/DB_json_out/Semantic_Memory/Unit_Links_Pairs.json",
):
    """
    Read continuity frames and output only FK–PK style links between tables,
    without example_row or pattern_type.

    Output structure:
    {
      "relations": [
        {
          "source_entity": "papers",
          "target_entity": "documents",
          "from_column": "papers.id",
          "to_column": "documents.id",
          "via_bridge": {...} | null
        },
        ...
      ]
    }
    """
    if not os.path.isfile(input_path):
        raise FileNotFoundError(f"Input file not found: {input_path}")

    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, dict) and "frames" in data:
        frames = data["frames"]
    elif isinstance(data, list):
        frames = data
    else:
        raise ValueError("Unexpected JSON structure: expected dict with 'frames' or a list.")

    relations = []
    seen = set()

    for frame in frames:
        src = frame.get("source_entity")
        tgt = frame.get("target_entity")
        if not src or not tgt:
            continue

        identity = frame.get("identity") or {}
        from_col = identity.get("from_column")
        to_col = identity.get("to_column")

        via_bridge = frame.get("via_bridge")

        key = (
            src,
            tgt,
            from_col,
            to_col,
            None if not via_bridge else via_bridge.get("bridge_table"),
        )
        if key in seen:
            continue
        seen.add(key)

        rel_entry = {
            "source_entity": src,
            "target_entity": tgt,
            "from_column": from_col,
            "to_column": to_col,
            "via_bridge": via_bridge if via_bridge else None,
        }
        relations.append(rel_entry)

    result = {"relations": relations}

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print("Saved relations to:", output_path)
    print(json.dumps(result, indent=2))

    return result


def build_entity_continuity_with_data(
    input_path: str = "src/utils_io/DB_json_out/SE_Data_Continuity_output.json",
    output_path: str = "src/utils_io/DB_json_out/Semantic_Memory/SE_Entity_Continuity_WithData.json",
    max_examples_per_link: int = 5,
):
    """
    Build a continuity summary PER ENTITY including example rows.
    """
    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    frames = data.get("frames", [])

    entities = {}
    link_index = {}

    for frame in frames:
        src = frame.get("source_entity")
        tgt = frame.get("target_entity")
        if not src or not tgt:
            continue

        pattern_type = frame.get("pattern_type")
        identity = frame.get("identity") or {}
        from_col = identity.get("from_column")
        to_col = identity.get("to_column")
        example_row = frame.get("example_row", {})

        if pattern_type == "entity":
            kind = "direct"
            bridge_table = None
            bridge_fk_from_source = None
            bridge_fk_to_target = None
        elif pattern_type == "bridge_via_SR":
            kind = "bridge"
            via = frame.get("via_bridge") or {}
            bridge_table = via.get("bridge_table")
            bridge_fk_from_source = via.get("bridge_fk_from_source")
            bridge_fk_to_target = via.get("bridge_fk_to_target")
        else:
            continue

        key = (
            src, kind, tgt, from_col, to_col,
            bridge_table, bridge_fk_from_source, bridge_fk_to_target
        )

        if key not in link_index:
            if src not in entities:
                entities[src] = {"continuity": []}

            link_record = {
                "kind": kind,
                "target": tgt,
                "from_column": from_col,
                "to_column": to_col,
                "examples": [],
            }
            if kind == "bridge":
                link_record.update({
                    "bridge_table": bridge_table,
                    "bridge_fk_from_source": bridge_fk_from_source,
                    "bridge_fk_to_target": bridge_fk_to_target,
                })

            entities[src]["continuity"].append(link_record)
            link_index[key] = link_record
        else:
            link_record = link_index[key]

        if example_row and len(link_record["examples"]) < max_examples_per_link:
            link_record["examples"].append({
                "example_row": example_row
            })

    out_obj = {"entities": entities}

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(out_obj, f, indent=2)

    return out_obj
