from typing import Any, Dict, List, Tuple
from utils_io.jsonio import load_json

def discover_SE_SR_SRR_SH_json_v2(db_folder: str) -> dict:
    """
    Discover:
      - SE  : strong entity tables
      - SEw : weak entity tables
      - SR  : pure many-to-many bridge
      - SRR : bridge with extra attributes
      - SH  : subclass (child ⊑ parent) ONLY between pure SE tables.
    """

    # -------------------------------------------------------
    # 1) Load metadata
    # -------------------------------------------------------
    tables_raw = load_json(db_folder, "tables.json") or []
    cols_raw   = load_json(db_folder, "columns.json") or []
    pks_raw    = load_json(db_folder, "primary_keys.json") or []
    fks_raw    = load_json(db_folder, "foreign_keys.json") or []

    # -------------------------------------------------------
    # 2) Build schema structures
    # -------------------------------------------------------
    schema: Dict[str, Dict[str, Any]] = {}
    table_names: List[str] = []

    for t in tables_raw:
        if isinstance(t, dict):
            tname = t.get("table")
        else:
            tname = t
        if not tname:
            continue
        tname = str(tname).strip()
        if not tname:
            continue
        table_names.append(tname)
        schema[tname] = {
            "columns": {},
            "pk": set(),
            "fk": {},
        }

    for c in cols_raw:
        tname = str(c.get("table", "")).strip()
        cname = str(c.get("column", "")).strip()
        if not tname or not cname:
            continue
        if tname not in schema:
            schema[tname] = {"columns": {}, "pk": set(), "fk": {}}
        schema[tname]["columns"][cname] = {
            "name": cname,
            "data_type": c.get("data_type"),
            "nullable": c.get("nullable"),
            "is_pk": False,
            "is_fk": False,
        }

    for pk in pks_raw:
        tname = str(pk.get("table", "")).strip()
        cname = str(pk.get("pk_column", pk.get("column", ""))).strip()
        if not tname or not cname:
            continue
        if tname not in schema:
            schema[tname] = {"columns": {}, "pk": set(), "fk": {}}
        schema[tname]["pk"].add(cname)
        colmeta = schema[tname]["columns"].setdefault(
            cname,
            {"name": cname, "data_type": None, "nullable": None, "is_pk": False, "is_fk": False},
        )
        colmeta["is_pk"] = True

    referenced_by: Dict[Tuple[str, str], List[Tuple[str, str]]] = {}

    for fk in fks_raw:
        tname = str(fk.get("table", "")).strip()
        cols_str = str(fk.get("columns", "")).strip()
        ref_table = str(fk.get("ref_table", "")).strip()
        ref_cols_str = str(fk.get("ref_columns", "")).strip()

        if not tname or not cols_str or not ref_table:
            continue

        if tname not in schema:
            schema[tname] = {"columns": {}, "pk": set(), "fk": {}}

        col_names = [c.strip() for c in cols_str.split(",") if c.strip()]
        ref_col_names = [c.strip() for c in ref_cols_str.split(",") if c.strip()]
        if not ref_col_names:
            ref_col_names = ["id"]

        for cname in col_names:
            colmeta = schema[tname]["columns"].setdefault(
                cname,
                {"name": cname, "data_type": None, "nullable": None, "is_pk": False, "is_fk": False},
            )
            colmeta["is_fk"] = True
            schema[tname]["fk"].setdefault(cname, []).append(
                {"ref_table": ref_table, "ref_columns": ref_col_names}
            )

            for rc in ref_col_names:
                key = (ref_table, rc)
                referenced_by.setdefault(key, []).append((tname, cname))

    # -------------------------------------------------------
    # 3) Per-table stats
    # -------------------------------------------------------
    per_table_stats: Dict[str, Dict[str, Any]] = {}

    for tname in table_names:
        tinfo = schema.get(tname, {})
        cols  = tinfo.get("columns", {})
        pk    = set(tinfo.get("pk", set()))

        col_names = list(cols.keys())
        pk_cols   = [c for c in col_names if c in pk]
        fk_cols   = [c for c in col_names if cols[c].get("is_fk")]
        attr_cols = [c for c in col_names if (c not in pk and not cols[c].get("is_fk"))]

        pk_referenced = False
        for c in pk_cols:
            if referenced_by.get((tname, c)):
                pk_referenced = True
                break

        all_pk_are_fk  = bool(pk_cols) and all(c in fk_cols for c in pk_cols)
        some_pk_fk     = any(c in fk_cols for c in pk_cols)
        some_pk_non_fk = any(c in pk_cols and c not in fk_cols for c in pk_cols)

        is_bridge_candidate = (
            len(pk_cols) >= 2
            and len(pk_cols) == len(col_names)
            and not pk_referenced
            and all_pk_are_fk
        )

        is_weak_candidate = (
            len(pk_cols) >= 2
            and some_pk_fk
            and some_pk_non_fk
            and len(attr_cols) == 0
            and not pk_referenced
        )

        has_non_fk_pk = some_pk_non_fk

        per_table_stats[tname] = {
            "all_cols": col_names,
            "pk_cols": pk_cols,
            "fk_cols": fk_cols,
            "attr_cols": attr_cols,
            "is_bridge_candidate": is_bridge_candidate,
            "is_weak_candidate": is_weak_candidate,
            "pk_has_incoming": pk_referenced,
            "all_pk_are_fk": all_pk_are_fk,
            "has_non_fk_pk": has_non_fk_pk,
        }

    # -------------------------------------------------------
    # 4) SE / SEw detection
    # -------------------------------------------------------
    is_SE: Dict[str, bool] = {}
    is_SEw: Dict[str, bool] = {}

    for tname in table_names:
        st = per_table_stats[tname]
        has_attr        = len(st["attr_cols"]) > 0
        pk_has_incoming = st["pk_has_incoming"]
        has_non_fk_pk   = st["has_non_fk_pk"]
        is_weak         = st["is_weak_candidate"]
        is_bridge       = st["is_bridge_candidate"]

        if is_weak:
            is_SEw[tname] = True
            is_SE[tname] = False
            continue

        se_flag = (
            (has_attr or pk_has_incoming or has_non_fk_pk)
            and not is_bridge
        )
        is_SE[tname] = se_flag
        if not se_flag:
            is_SEw[tname] = False

    # -------------------------------------------------------
    # 5) SH detection
    # -------------------------------------------------------
    SH_relations: List[Dict[str, Any]] = []
    is_SH_child: Dict[str, bool] = {t: False for t in table_names}

    for tname in table_names:
        if not is_SE.get(tname, False) or is_SEw.get(tname, False):
            continue

        tinfo = schema.get(tname, {})
        pk    = set(tinfo.get("pk", set()))
        fk_map = tinfo.get("fk", {})

        for c in pk:
            if c not in fk_map:
                continue
            for fk in fk_map[c]:
                pt = fk["ref_table"]
                ref_cols = fk.get("ref_columns") or ["id"]

                pinfo = schema.get(pt)
                if not pinfo:
                    continue
                parent_pk = set(pinfo.get("pk", set()))
                if not parent_pk:
                    continue
                if (not is_SE.get(pt, False)) or is_SEw.get(pt, False):
                    continue
                if not set(ref_cols).issubset(parent_pk):
                    continue

                SH_relations.append({
                    "child_table": tname,
                    "child_pk_column": c,
                    "parent_table": pt,
                    "parent_pk_columns": list(parent_pk),
                })
                is_SH_child[tname] = True

    # -------------------------------------------------------
    # 6) SR / SRR detection
    # -------------------------------------------------------
    SR_tables: List[str]  = []
    SRR_tables: List[str] = []

    for tname in table_names:
        st = per_table_stats[tname]

        if is_SE.get(tname, False) or is_SEw.get(tname, False):
            continue

        if st["is_bridge_candidate"]:
            if len(st["attr_cols"]) == 0:
                SR_tables.append(tname)
            else:
                SRR_tables.append(tname)

    SE_tables  = sorted([t for t in table_names if is_SE.get(t, False)])
    SEw_tables = sorted([t for t in table_names if is_SEw.get(t, False)])
    SR_tables  = sorted(SR_tables)
    SRR_tables = sorted(SRR_tables)

    return {
        "SE_tables": SE_tables,
        "SEw_tables": SEw_tables,
        "SR_tables": SR_tables,
        "SRR_tables": SRR_tables,
        "SH_relations": SH_relations,
        "per_table_stats": per_table_stats,
        "is_SE": is_SE,
        "is_SEw": is_SEw,
        "is_SH_child": is_SH_child,
    }
