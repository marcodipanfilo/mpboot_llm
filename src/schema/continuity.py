def SE_Data_Continuity(
    db_folder=None,
    dump_path=None,
    max_rows: int = 5,
    max_cols_per_table: int = 10,
):
    """
    SE_Data_Continuity (JSON frame version, with SR bridges)

    1) Reads schema from DB_json_out (tables.json, columns.json,
       primary_keys.json, foreign_keys.json, unique_constraints.json).
    2) Reads data from a PostgreSQL-style .sql dump using COPY ... FROM stdin; blocks.
    3) Detects:
         - SE tables = tables that have at least one non-FK column
         - SE source tables = SE tables that also have at least one FK
           (used for "direct" continuity frames)
         - SR bridge tables = tables with >=2 FK columns and 0 non-FK columns
           (pure relationship tables, used as many-to-many bridges)
    4) Builds continuity frames:

       Direct SE continuity (FK inside SE source table):

         {
           "frame_type": "continuity",
           "pattern_category": "SE",
           "pattern_type": "entity",
           "source_entity": "papers",
           "target_entity": "documents",
           "identity": {
             "from_column": "papers.id",
             "to_column": "documents.id"
           },
           "example_row": {
             "papers":    { ... },
             "documents": { ... }
           }
         }

       Bridge continuity via SR table:

         {
           "frame_type": "continuity",
           "pattern_category": "SE",
           "pattern_type": "bridge_via_SR",
           "source_entity": "persons",
           "target_entity": "papers",
           "via_bridge": {
             "bridge_table": "co_write_paper",
             "bridge_fk_from_source": "co_write_paper.co_author",
             "bridge_fk_to_target": "co_write_paper.paper"
           },
           "identity": {
             "from_column": "persons.id",
             "to_column": "papers.id"
           },
           "example_row": {
             "persons":       { ... },   # may be empty if no data
             "co_write_paper":{ ... },   # may be empty if no data
             "papers":        { ... }    # may be empty if no data
           }
         }

       When there is no data (0 rows) in the bridge or endpoints, we still
       create a *structural* bridge frame with empty example rows but with
       correct identity / via_bridge info, so that all semantics and links
       (e.g., papers ↔ subject_areas via paper_subject_area) are discovered.

    Parameters
    ----------
    db_folder : str or None
        Folder containing DB_json_out files.
        If None, it tries common Colab/local paths.

    dump_path : str or None
        Path to the SQL dump file (PostgreSQL, with COPY blocks).
        If None, only structural continuity (no example rows) is possible.

    max_rows : int
        For each continuity relation, how many row examples to include.

    max_cols_per_table : int
        Maximum number of columns to include in the example_row snippet
        per table.

    Returns
    -------
    dict
      {
        "status": "ok" | "error",
        "base_folder": ...,
        "used_dump": dump_path or None,
        "se_tables": [...],    # SE (entity-like) tables
        "frames": [ ... continuity JSON frames ... ]
      }
    """
    import os
    import json
    from typing import Dict, Any, List

    # ------------------------------------------------------------------
    # Helper: load JSON file from base folder
    # ------------------------------------------------------------------
    def load_json(base: str, name: str):
        path = os.path.join(base, name)
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # 1) Locate DB_json_out folder
    # ------------------------------------------------------------------
    candidates: List[str] = []
    if db_folder:
        candidates.append(db_folder)
    candidates.extend([
        "/Colab/DB_json_out/",
        "/content/drive/MyDrive/Colab/DB_json_out/",
        "/Colab/DB_json.out/",
        "/content/drive/MyDrive/Colab/DB_json.out/",
        "./DB_json_out/",
        "./DB_json.out/",
        "./",
    ])

    base = None
    for c in candidates:
        if os.path.isdir(c):
            base = c
            break

    if base is None:
        return {
            "status": "error",
            "error": "db_folder_not_found",
            "candidates": candidates,
        }

    # ------------------------------------------------------------------
    # 2) Load schema metadata JSONs
    # ------------------------------------------------------------------
    tables_raw = load_json(base, "tables.json") or []
    columns_raw = load_json(base, "columns.json") or []
    pks_raw = load_json(base, "primary_keys.json") or []
    fks_raw = load_json(base, "foreign_keys.json") or []
    uniques_raw = load_json(base, "unique_constraints.json") or []

    # ------------------------------------------------------------------
    # 3) Build minimal in-memory schema
    # ------------------------------------------------------------------
    db: Dict[str, Dict[str, Any]] = {}   # key: table_lower, value: {name, columns{col_lower: meta}}
    pk_map: Dict[str, set] = {}          # table_name -> set(pk_columns)

    # 3.1 tables.json
    for t in tables_raw:
        if isinstance(t, dict):
            tname = t.get("table")
        else:
            tname = t
        if not tname:
            continue
        key = str(tname).strip()
        db[key.lower()] = {"name": key, "columns": {}}

    # 3.2 columns.json
    for ent in columns_raw:
        tname = str(ent.get("table", "")).strip()
        cname = str(ent.get("column", "")).strip()
        if not tname or not cname:
            continue

        key_t = tname.lower()
        if key_t not in db:
            db[key_t] = {"name": tname, "columns": {}}
        cols = db[key_t]["columns"]
        key_c = cname.lower()

        if key_c not in cols:
            cols[key_c] = {
                "name": cname,
                "data_type": ent.get("data_type"),
                "nullable": bool(ent["nullable"]) if "nullable" in ent else None,
                "is_pk": False,
                "is_fk": False,
                "fk": [],
                "is_unique": False,
                "samples": ent.get("samples", []) or [],
            }

    # 3.3 primary_keys.json
    for pk in pks_raw:
        tname = str(pk.get("table", "")).strip()
        pk_col = str(pk.get("pk_column", pk.get("column", ""))).strip()
        if not tname or not pk_col:
            continue
        key_t = tname.lower()
        key_c = pk_col.lower()
        if key_t not in db:
            db[key_t] = {"name": tname, "columns": {}}
        cols = db[key_t]["columns"]
        if key_c not in cols:
            cols[key_c] = {
                "name": pk_col,
                "data_type": None,
                "nullable": None,
                "is_pk": True,
                "is_fk": False,
                "fk": [],
                "is_unique": False,
                "samples": [],
            }
        else:
            cols[key_c]["is_pk"] = True

        pk_map.setdefault(tname, set()).add(pk_col)

    # 3.4 foreign_keys.json
    for fk in fks_raw:
        tname = str(fk.get("table", "")).strip()
        cols_str = str(fk.get("columns", "")).strip()
        ref_table = str(fk.get("ref_table", "")).strip()
        ref_cols_str = str(fk.get("ref_columns", "")).strip()
        fk_name = fk.get("fk_name") or fk.get("constraint") or None

        if not tname or not cols_str:
            continue

        key_t = tname.lower()
        if key_t not in db:
            db[key_t] = {"name": tname, "columns": {}}
        cols = db[key_t]["columns"]

        col_names = [c.strip() for c in cols_str.split(",") if c.strip()]
        ref_col_names = [c.strip() for c in ref_cols_str.split(",") if c.strip()]

        for cname in col_names:
            key_c = cname.lower()
            if key_c not in cols:
                cols[key_c] = {
                    "name": cname,
                    "data_type": None,
                    "nullable": None,
                    "is_pk": False,
                    "is_fk": True,
                    "fk": [],
                    "is_unique": False,
                    "samples": [],
                }
            cols[key_c]["is_fk"] = True
            cols[key_c]["fk"].append({
                "fk_name": fk_name,
                "ref_table": ref_table,
                "ref_columns": ref_col_names,
            })

    # 3.5 unique_constraints.json (not essential for continuity)
    for u in uniques_raw:
        if not isinstance(u, dict):
            continue
        tname = str(u.get("table", "")).strip()
        if not tname:
            continue
        cols_u = u.get("columns") or u.get("column") or []
        if isinstance(cols_u, str):
            cols_u = [c.strip() for c in cols_u.split(",") if c.strip()]
        cols_u = [str(c) for c in cols_u]

        key_t = tname.lower()
        if key_t not in db:
            db[key_t] = {"name": tname, "columns": {}}
        cols = db[key_t]["columns"]
        for cname in cols_u:
            key_c = cname.lower()
            if key_c not in cols:
                cols[key_c] = {
                    "name": cname,
                    "data_type": None,
                    "nullable": None,
                    "is_pk": False,
                    "is_fk": False,
                    "fk": [],
                    "is_unique": True,
                    "samples": [],
                }
            else:
                cols[key_c]["is_unique"] = True

    # ------------------------------------------------------------------
    # 4) Classify SE tables, SE-source tables, SR bridge tables
    # ------------------------------------------------------------------
    se_tables: List[str] = []          # entity-like (has at least one non-FK)
    se_source_tables: List[str] = []   # SE + has FK (used for direct continuity)
    sr_bridge_tables: List[str] = []   # pure relationship tables (≥2 FKs, 0 non-FKs)

    table_meta: Dict[str, Dict[str, Any]] = {}  # by table name as in schema (case-sensitive)

    for key_t, tbl in db.items():
        tname = tbl["name"]
        cols_dict = tbl["columns"]
        if not cols_dict:
            continue

        has_fk = any(c.get("is_fk") for c in cols_dict.values())
        has_non_fk = any(not c.get("is_fk") for c in cols_dict.values())

        if has_non_fk:
            se_tables.append(tname)

        # build fk_info for this table
        fk_info = {}
        fk_count = 0
        for c in cols_dict.values():
            cname = c["name"]
            if c.get("is_fk"):
                fk_count += 1
                fk_info.setdefault(cname, [])
                for fk in c.get("fk") or []:
                    ref_table = fk.get("ref_table")
                    ref_cols = fk.get("ref_columns") or []
                    if not ref_table:
                        continue
                    if ref_cols:
                        ref_col0 = ref_cols[0]
                    else:
                        ref_col0 = "id"
                    fk_info[cname].append({
                        "ref_table": ref_table,
                        "ref_col": ref_col0,
                    })

        is_se_source = bool(has_fk and has_non_fk)
        is_sr_bridge = bool(not has_non_fk and fk_count >= 2)

        table_meta[tname] = {
            "fk_info": fk_info,
            "is_se_source": is_se_source,
            "is_sr_bridge": is_sr_bridge,
            "columns_order": None,   # to be filled after loading data
            "index": None,           # to be filled after loading data
        }

        if is_se_source:
            se_source_tables.append(tname)
        if is_sr_bridge:
            sr_bridge_tables.append(tname)

    se_tables_sorted = sorted(se_tables)

    # If no dump, we can only return structural info (no example rows).
    if not dump_path:
        return {
            "status": "ok",
            "base_folder": base,
            "used_dump": None,
            "se_tables": se_tables_sorted,
            "frames": [],
        }

    # ------------------------------------------------------------------
    # 5) Helper: read ALL rows of a table from SQL dump (COPY ...)
    # ------------------------------------------------------------------
    def load_table_rows_from_dump(
        dump_file: str,
        table_name: str,
    ) -> Dict[str, Any]:
        """
        Reads ALL rows for one table from a PostgreSQL dump.
        Returns:
          {
            "status": "ok" | "error",
            "columns": [col1, col2, ...],
            "rows": [ {col1: v1, col2: v2, ...}, ... ]
          }
        """
        if not dump_file or not os.path.isfile(dump_file):
            return {"status": "error", "error": "dump_not_found"}

        header_prefix = f"COPY {table_name} "
        in_copy_block = False
        columns: List[str] = []
        rows: List[Dict[str, Any]] = []

        try:
            with open(dump_file, encoding="utf-8") as f:
                for raw_line in f:
                    line = raw_line.rstrip("\n")

                    # Find COPY header
                    if not in_copy_block:
                        if line.startswith(header_prefix) and "FROM stdin;" in line:
                            # Parse column list
                            try:
                                start = line.index("(") + 1
                                end = line.index(")", start)
                            except ValueError:
                                return {
                                    "status": "error",
                                    "error": "copy_header_parse_error",
                                    "line": line,
                                }
                            cols_str = line[start:end]
                            columns = [c.strip() for c in cols_str.split(",") if c.strip()]
                            in_copy_block = True
                        continue

                    # Inside COPY data block
                    if line == "\\.":
                        # End of this COPY block
                        break

                    if not line:
                        continue

                    parts = line.split("\t")
                    if len(parts) != len(columns):
                        # malformed row, skip
                        continue

                    row = {}
                    for c_name, val in zip(columns, parts):
                        if val == "\\N":
                            val = None
                        row[c_name] = val
                    rows.append(row)
        except Exception as e:
            return {
                "status": "error",
                "error": "io_error",
                "detail": f"{type(e).__name__}: {e}",
            }

        return {
            "status": "ok",
            "columns": columns,
            "rows": rows,
        }

    # ------------------------------------------------------------------
    # 6) Load ALL rows for all tables from dump, build indices
    # ------------------------------------------------------------------
    table_rows: Dict[str, Dict[str, Any]] = {}  # name → {"columns": [...], "rows": [...]}

    for key_t, tbl in db.items():
        tname = tbl["name"]
        res_rows = load_table_rows_from_dump(dump_path, tname)
        if res_rows.get("status") == "ok" and res_rows.get("rows"):
            table_rows[tname] = res_rows

    # Build per-table indices: for each column, value -> first row with that value
    for tname, data in table_rows.items():
        cols_order = data["columns"]
        rows = data["rows"]

        idx: Dict[str, Dict[Any, Dict[str, Any]]] = {}
        for c in cols_order:
            idx[c] = {}

        for row in rows:
            for c in cols_order:
                v = row.get(c)
                if v is None:
                    continue
                if v not in idx[c]:
                    idx[c][v] = row

        if tname in table_meta:
            table_meta[tname]["columns_order"] = cols_order
            table_meta[tname]["index"] = idx
        else:
            table_meta[tname] = {
                "fk_info": {},
                "is_se_source": False,
                "is_sr_bridge": False,
                "columns_order": cols_order,
                "index": idx,
            }

    # ------------------------------------------------------------------
    # 7) Helper: build example_row dict (limited cols, NULLs kept)
    # ------------------------------------------------------------------
    def example_row_dict(tname: str, row: Dict[str, Any]) -> Dict[str, Any]:
        meta = table_meta.get(tname) or {}
        cols_order = meta.get("columns_order") or list(row.keys())
        result: Dict[str, Any] = {}
        count = 0
        for c in cols_order:
            v = row.get(c)
            result[c] = v
            count += 1
            if count >= max_cols_per_table:
                break
        return result

    # ------------------------------------------------------------------
    # 8) Build direct continuity frames (SE source: FK → target)
    # ------------------------------------------------------------------
    frames: List[Dict[str, Any]] = []

    for source_table in se_source_tables:
        meta = table_meta.get(source_table) or {}
        fk_info = meta.get("fk_info") or {}
        data_src = table_rows.get(source_table)
        if not data_src:
            continue

        rows_src = data_src["rows"]
        if not rows_src:
            continue

        # For each FK column and target, create frames for up to max_rows examples
        for fk_col, targets in fk_info.items():
            for target in targets:
                target_table = target["ref_table"]
                target_col = target["ref_col"]

                tmeta2 = table_meta.get(target_table)
                tdata2 = table_rows.get(target_table)
                if not tmeta2 or not tdata2:
                    continue

                idx2 = tmeta2.get("index") or {}
                col_index = idx2.get(target_col) or {}
                if not col_index:
                    continue

                # choose pk names for identity (if any)
                src_pks = sorted(pk_map.get(source_table, []))
                tgt_pks = sorted(pk_map.get(target_table, []))
                src_pk = src_pks[0] if src_pks else fk_col
                tgt_pk = tgt_pks[0] if tgt_pks else target_col

                examples_added = 0
                for row_src in rows_src:
                    if examples_added >= max_rows:
                        break
                    fk_val = row_src.get(fk_col)
                    if fk_val is None:
                        continue

                    row_tgt = col_index.get(fk_val)
                    if not row_tgt:
                        continue

                    frame = {
                        "frame_type": "continuity",
                        "pattern_category": "SE",
                        "pattern_type": "entity",
                        "source_entity": source_table,
                        "target_entity": target_table,
                        "identity": {
                            "from_column": f"{source_table}.{src_pk}",
                            "to_column": f"{target_table}.{tgt_pk}",
                        },
                        "example_row": {
                            source_table: example_row_dict(source_table, row_src),
                            target_table: example_row_dict(target_table, row_tgt),
                        },
                    }
                    frames.append(frame)
                    examples_added += 1

    # ------------------------------------------------------------------
    # 9) Build bridge continuity frames via SR tables (many-to-many)
    # ------------------------------------------------------------------
    for bridge_table in sr_bridge_tables:
        bmeta = table_meta.get(bridge_table) or {}
        b_fk_info = bmeta.get("fk_info") or {}

        # Flatten bridge endpoints: each FK in bridge → (bridge_col, ref_table, ref_col)
        endpoints = []
        for b_col, refs in b_fk_info.items():
            for r in refs:
                ref_table = r.get("ref_table")
                ref_col = r.get("ref_col")
                if not ref_table:
                    continue
                endpoints.append({
                    "bridge_col": b_col,
                    "table": ref_table,
                    "ref_col": ref_col,
                })

        if len(endpoints) < 2:
            continue

        # Load bridge rows (if any)
        b_data = table_rows.get(bridge_table)
        b_rows = b_data["rows"] if b_data and b_data.get("status") == "ok" else []

        # For each pair of referenced tables, create continuity frames
        for i in range(len(endpoints)):
            for j in range(i + 1, len(endpoints)):
                ep1 = endpoints[i]
                ep2 = endpoints[j]
                tab1 = ep1["table"]
                tab2 = ep2["table"]
                if tab1 == tab2:
                    continue

                # Only connect entity-like tables
                if tab1 not in se_tables or tab2 not in se_tables:
                    continue

                # PK names for identity
                pk1_list = sorted(pk_map.get(tab1, []))
                pk2_list = sorted(pk_map.get(tab2, []))
                pk1 = pk1_list[0] if pk1_list else ep1["ref_col"]
                pk2 = pk2_list[0] if pk2_list else ep2["ref_col"]

                # Indices for endpoints (if data exists)
                meta1 = table_meta.get(tab1) or {}
                meta2 = table_meta.get(tab2) or {}
                idx1_all = meta1.get("index") or {}
                idx2_all = meta2.get("index") or {}
                idx1 = idx1_all.get(ep1["ref_col"]) or {}
                idx2 = idx2_all.get(ep2["ref_col"]) or {}

                # If we have data for bridge AND endpoints -> example-based frames
                if b_rows and idx1 and idx2:
                    examples_dir1 = 0
                    examples_dir2 = 0

                    for row_b in b_rows:
                        if examples_dir1 >= max_rows and examples_dir2 >= max_rows:
                            break

                        v1 = row_b.get(ep1["bridge_col"])
                        v2 = row_b.get(ep2["bridge_col"])
                        if v1 is None or v2 is None:
                            continue

                        row1 = idx1.get(v1)
                        row2 = idx2.get(v2)
                        if not row1 or not row2:
                            continue

                        # Direction 1: tab1 -> tab2
                        if examples_dir1 < max_rows:
                            frame1 = {
                                "frame_type": "continuity",
                                "pattern_category": "SE",
                                "pattern_type": "bridge_via_SR",
                                "source_entity": tab1,
                                "target_entity": tab2,
                                "via_bridge": {
                                    "bridge_table": bridge_table,
                                    "bridge_fk_from_source": f"{bridge_table}.{ep1['bridge_col']}",
                                    "bridge_fk_to_target": f"{bridge_table}.{ep2['bridge_col']}",
                                },
                                "identity": {
                                    "from_column": f"{tab1}.{pk1}",
                                    "to_column": f"{tab2}.{pk2}",
                                },
                                "example_row": {
                                    tab1: example_row_dict(tab1, row1),
                                    bridge_table: example_row_dict(bridge_table, row_b),
                                    tab2: example_row_dict(tab2, row2),
                                },
                            }
                            frames.append(frame1)
                            examples_dir1 += 1

                        # Direction 2: tab2 -> tab1
                        if examples_dir2 < max_rows:
                            frame2 = {
                                "frame_type": "continuity",
                                "pattern_category": "SE",
                                "pattern_type": "bridge_via_SR",
                                "source_entity": tab2,
                                "target_entity": tab1,
                                "via_bridge": {
                                    "bridge_table": bridge_table,
                                    "bridge_fk_from_source": f"{bridge_table}.{ep2['bridge_col']}",
                                    "bridge_fk_to_target": f"{bridge_table}.{ep1['bridge_col']}",
                                },
                                "identity": {
                                    "from_column": f"{tab2}.{pk2}",
                                    "to_column": f"{tab1}.{pk1}",
                                },
                                "example_row": {
                                    tab2: example_row_dict(tab2, row2),
                                    bridge_table: example_row_dict(bridge_table, row_b),
                                    tab1: example_row_dict(tab1, row1),
                                },
                            }
                            frames.append(frame2)
                            examples_dir2 += 1
                else:
                    # No data (0 rows in bridge or endpoints) → structural-only frame
                    # Direction 1: tab1 -> tab2
                    frame1 = {
                        "frame_type": "continuity",
                        "pattern_category": "SE",
                        "pattern_type": "bridge_via_SR",
                        "source_entity": tab1,
                        "target_entity": tab2,
                        "via_bridge": {
                            "bridge_table": bridge_table,
                            "bridge_fk_from_source": f"{bridge_table}.{ep1['bridge_col']}",
                            "bridge_fk_to_target": f"{bridge_table}.{ep2['bridge_col']}",
                        },
                        "identity": {
                            "from_column": f"{tab1}.{pk1}",
                            "to_column": f"{tab2}.{pk2}",
                        },
                        "example_row": {
                            tab1: {},
                            bridge_table: {},
                            tab2: {},
                        },
                    }
                    frames.append(frame1)

                    # Direction 2: tab2 -> tab1
                    frame2 = {
                        "frame_type": "continuity",
                        "pattern_category": "SE",
                        "pattern_type": "bridge_via_SR",
                        "source_entity": tab2,
                        "target_entity": tab1,
                        "via_bridge": {
                            "bridge_table": bridge_table,
                            "bridge_fk_from_source": f"{bridge_table}.{ep2['bridge_col']}",
                            "bridge_fk_to_target": f"{bridge_table}.{ep1['bridge_col']}",
                        },
                        "identity": {
                            "from_column": f"{tab2}.{pk2}",
                            "to_column": f"{tab1}.{pk1}",
                        },
                        "example_row": {
                            tab2: {},
                            bridge_table: {},
                            tab1: {},
                        },
                    }
                    frames.append(frame2)

    # ------------------------------------------------------------------
    # 10) Return result
    # ------------------------------------------------------------------
    return {
        "status": "ok",
        "base_folder": base,
        "used_dump": dump_path,
        "se_tables": se_tables_sorted,
        "frames": frames,
    }
