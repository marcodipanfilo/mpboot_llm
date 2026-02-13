# scripts/build_semantic_units.py
import os, json
from collections import defaultdict

def load_json(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

def main():
    db_folder = os.environ.get("MPBOOT_DB_JSON_OUT", "src/utils_io/DB_json_out")
    sem_mem = os.path.join(db_folder, "Semantic_Memory")
    os.makedirs(sem_mem, exist_ok=True)

    tables_path = os.path.join(db_folder, "tables.json")
    links_path  = os.path.join(sem_mem, "Unit_Links_Pairs.json")
    out_path    = os.path.join(sem_mem, "Semantic_Units.json")

    tables_raw = load_json(tables_path) if os.path.isfile(tables_path) else []
    all_tables = []
    for t in tables_raw:
        all_tables.append(t.get("table") if isinstance(t, dict) else t)
    all_tables = [str(x).strip() for x in all_tables if x]

    relations = []
    if os.path.isfile(links_path):
        data = load_json(links_path)
        relations = data.get("relations", []) or []

    graph = defaultdict(set)

    # 1) add all tables as nodes (even isolated)
    for t in all_tables:
        graph[t]  # touch

    # 2) add edges from relations
    for rel in relations:
        src = (rel.get("source_entity") or "").strip()
        tgt = (rel.get("target_entity") or "").strip()
        if src and tgt:
            graph[src].add(tgt)
            graph[tgt].add(src)

        vb = rel.get("via_bridge") or None
        if isinstance(vb, dict):
            bt = (vb.get("bridge_table") or "").strip()
            if bt:
                # include bridge table in unit graph too
                graph[src].add(bt); graph[bt].add(src)
                graph[tgt].add(bt); graph[bt].add(tgt)

    entities_relations = {k: sorted(list(v)) for k, v in graph.items()}
    result = {"entities": entities_relations}

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print("Saved entity relations JSON to:", out_path)

if __name__ == "__main__":
    main()
