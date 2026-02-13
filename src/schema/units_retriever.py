from collections import deque
import json

##############################
# Units Retriever
##############################

def get_semantic_unit(
    table_name: str,
    input_path: str = "src/utils_io/DB_json_out/Semantic_Memory/Semantic_Units.json"
):
    """
    Given a table/entity name, return the semantic unit (connected component)
    it belongs to, based on Semantic_Units.json.

    Output: list of entity names (including the given table_name).
    """

    # Load semantic units file
    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    entities = data.get("entities", {})

    if table_name not in entities:
        # The node might still appear only as a neighbor, so we add it if needed
        present_somewhere = False
        for src, neighbors in entities.items():
            if table_name in neighbors:
                present_somewhere = True
                break
        if not present_somewhere:
            print(f"Entity '{table_name}' not found in Semantic_Units graph.")
            return []

        # If it appears only as neighbor, ensure it exists as a key with empty list
        entities.setdefault(table_name, [])

    # Build undirected adjacency
    graph = {e: set(neighs) for e, neighs in entities.items()}
    for src, neighs in entities.items():
        for n in neighs:
            graph.setdefault(n, set())
            graph[n].add(src)

    # BFS to get connected component
    visited = set()
    queue = deque([table_name])
    visited.add(table_name)

    while queue:
        current = queue.popleft()
        for neighbor in graph.get(current, []):
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append(neighbor)

    unit = sorted(visited)
    print(", ".join(unit))

    return unit
