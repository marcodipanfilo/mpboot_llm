import os
from typing import TypedDict, List, Dict, Any

from langgraph.graph import StateGraph, END

from agents.understanding import understanding
from agents.enrichment import enrichment
from agents.mapper import mapper


class PipelineState(TypedDict, total=False):
    db_folder: str
    ontology_path: str

    tables: List[str]
    idx: int
    current_table: str

    prep_done: bool
    results: Dict[str, Any]
    errors: List[Dict[str, str]]


def _list_tables_from_db_json_out(db_folder: str) -> List[str]:
    import json

    tables_path = os.path.join(db_folder, "tables.json")
    if not os.path.isfile(tables_path):
        raise FileNotFoundError(f"tables.json not found at: {tables_path}")

    with open(tables_path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    names: List[str] = []
    for t in raw:
        if isinstance(t, dict):
            name = str(t.get("table", "")).strip()
        else:
            name = str(t).strip()
        if name:
            names.append(name)

    return sorted(set(names))


def _run_script(script_path: str, db_folder: str) -> None:
    import sys
    import subprocess

    if not os.path.isfile(script_path):
        raise FileNotFoundError(f"Preparation script not found: {script_path}")

    env = os.environ.copy()
    env["MPBOOT_DB_JSON_OUT"] = db_folder

    subprocess.check_call([sys.executable, script_path], env=env)


# -------------------------
# Node 0: preparation once
# -------------------------
def node_prepare(state: PipelineState) -> PipelineState:
    """
    Run global preparation steps ONCE by calling your existing scripts.
    This fills all JSON outputs needed by retrievers/agents for ALL tables.
    """
    db_folder = state["db_folder"]

    # Ensure Semantic_Memory exists
    sem_mem_dir = os.path.join(db_folder, "Semantic_Memory")
    os.makedirs(sem_mem_dir, exist_ok=True)

    # Required base inputs
    required_inputs = [
        os.path.join(db_folder, "tables.json"),
        os.path.join(db_folder, "columns.json"),
        os.path.join(db_folder, "primary_keys.json"),
        os.path.join(db_folder, "foreign_keys.json"),
    ]
    for p in required_inputs:
        if not os.path.exists(p):
            raise FileNotFoundError(f"Missing required artifact/path: {p}")

    # IMPORTANT: order matters
    # - Unit links must exist before building semantic units
    prep_scripts = [
        "scripts/run_rdb_discovery.py",
        "scripts/run_se_data_continuity.py",
        "scripts/run_unit_links_and_continuity.py",
        "scripts/build_semantic_units.py",
    ]

    for s in prep_scripts:
        _run_script(s, db_folder)

    # Verify outputs exist
    required_outputs = [
        os.path.join(db_folder, "Semantic_Memory", "Patterns_discovery.json"),
        os.path.join(db_folder, "SE_Data_Continuity_output.json"),
        os.path.join(db_folder, "Semantic_Memory", "Unit_Links_Pairs.json"),
        os.path.join(db_folder, "Semantic_Memory", "Semantic_Units.json"),
        os.path.join(db_folder, "Semantic_Memory", "SE_Entity_Continuity_WithData.json"),
    ]
    for p in required_outputs:
        if not os.path.isfile(p) or os.path.getsize(p) == 0:
            raise FileNotFoundError(f"Missing/empty required output: {p}")

    state["prep_done"] = True

    tables = _list_tables_from_db_json_out(db_folder)
    state["tables"] = tables
    state["idx"] = 0
    state["results"] = {}
    state["errors"] = []
    return state


# -------------------------
# Node 1: pick next table
# -------------------------
def node_next_table(state: PipelineState) -> PipelineState:
    idx = int(state.get("idx", 0))
    tables = state.get("tables", [])
    if idx >= len(tables):
        return state

    state["current_table"] = tables[idx]
    return state


# -------------------------
# Node 2: run SU
# -------------------------
def node_understanding(state: PipelineState) -> PipelineState:
    t = state["current_table"]
    try:
        out = understanding(t)
        state["results"].setdefault(t, {})["understanding"] = out
    except Exception as e:
        state["errors"].append({"table": t, "stage": "understanding", "error": str(e)})
    return state


# -------------------------
# Node 3: run SE
# -------------------------
def node_enrichment(state: PipelineState) -> PipelineState:
    t = state["current_table"]
    try:
        out = enrichment(t)
        state["results"].setdefault(t, {})["enrichment"] = out
    except Exception as e:
        state["errors"].append({"table": t, "stage": "enrichment", "error": str(e)})
    return state


# -------------------------
# Node 4: run Mapper
# -------------------------
def node_mapper(state: PipelineState) -> PipelineState:
    t = state["current_table"]
    try:
        out = mapper(t)
        state["results"].setdefault(t, {})["mapper"] = {
            "saved_to": out.get("saved_to"),
            "report_saved_to": out.get("report_saved_to"),
            "no_match": out.get("no_match"),
        }
    except Exception as e:
        state["errors"].append({"table": t, "stage": "mapper", "error": str(e)})
    return state


# -------------------------
# Node 5: increment
# -------------------------
def node_increment(state: PipelineState) -> PipelineState:
    state["idx"] = int(state.get("idx", 0)) + 1
    return state


def route_continue_or_end(state: PipelineState):
    idx = int(state.get("idx", 0))
    tables = state.get("tables", [])
    return "next_table" if idx < len(tables) else END


def build_graph():
    g = StateGraph(PipelineState)

    g.add_node("prepare", node_prepare)
    g.add_node("next_table", node_next_table)
    g.add_node("understanding", node_understanding)
    g.add_node("enrichment", node_enrichment)
    g.add_node("mapper", node_mapper)
    g.add_node("inc", node_increment)

    g.set_entry_point("prepare")

    g.add_edge("prepare", "next_table")
    g.add_edge("next_table", "understanding")
    g.add_edge("understanding", "enrichment")
    g.add_edge("enrichment", "mapper")
    g.add_edge("mapper", "inc")

    g.add_conditional_edges("inc", route_continue_or_end, {"next_table": "next_table", END: END})
    return g.compile()


if __name__ == "__main__":
    db_folder = os.environ.get("MPBOOT_DB_JSON_OUT", "src/utils_io/DB_json_out")
    ontology_path = os.environ.get("MPBOOT_ONTOLOGY_PATH", "")

    graph = build_graph()

    # IMPORTANT: default recursion_limit=25 is too small for full DB runs
    final_state = graph.invoke(
        {
            "db_folder": db_folder,
            "ontology_path": ontology_path,
        },
        config={"recursion_limit": 5000},
    )

    print("Tables processed:", len(final_state.get("tables", [])))
    print("Errors:", len(final_state.get("errors", [])))
    if final_state.get("errors"):
        for e in final_state["errors"][:50]:
            print(e)
