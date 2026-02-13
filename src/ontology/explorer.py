from typing import List, Dict, Any, Union
from owlready2 import get_ontology

# Defaults (override via env)
ONTOLOGY_PATH = "src/ontology/ontology.owl"
BASE_IRI      = "http://conference#"

_OWL_ONTO = None  # cached Owlready ontology


def _load_ontology():
    """Load the ontology once and cache it."""
    global _OWL_ONTO
    if _OWL_ONTO is None:
        _OWL_ONTO = get_ontology("file://" + ONTOLOGY_PATH).load()
    return _OWL_ONTO


def _local_name(iri: str) -> str:
    """Extract local name after '#' or last '/'."""
    if "#" in iri:
        return iri.split("#")[-1]
    return iri.rsplit("/", 1)[-1]


def ontology_explorer(
    mode: str = "classes",
    class_names: Union[None, str, List[str]] = None
) -> Any:
    """
    Generic ontology explorer.
    """
    onto = _load_ontology()

    if mode == "classes":
        local_classes = []
        for cls in onto.classes():
            iri = getattr(cls, "iri", None)
            if not iri:
                continue
            if str(iri).startswith(BASE_IRI):
                local_classes.append(_local_name(str(iri)))
        local_classes = sorted(set(local_classes))
        return local_classes

    if isinstance(class_names, str):
        class_names = [class_names]

    if class_names is None:
        class_names = ontology_explorer(mode="classes")

    class_obj_map: Dict[str, Any] = {}
    for cn in class_names:
        if hasattr(onto, cn):
            class_obj_map[cn] = getattr(onto, cn)
        else:
            try:
                cls = onto.search_one(iri=BASE_IRI + cn)
            except Exception:
                cls = None
            if cls is not None:
                class_obj_map[cn] = cls

    class_obj_map = {k: v for k, v in class_obj_map.items() if v is not None}

    if mode == "data_properties":
        props = list(onto.data_properties())
    elif mode == "object_properties":
        props = list(onto.object_properties())
    else:
        raise ValueError("mode must be 'classes', 'data_properties', or 'object_properties'")

    prop_meta_list = []
    for p in props:
        p_iri = getattr(p, "iri", None)
        if not p_iri:
            continue
        p_iri_str = str(p_iri)
        p_name = p.name

        domains = [str(d.iri) for d in getattr(p, "domain", []) if getattr(d, "iri", None)]
        ranges_raw = getattr(p, "range", [])
        ranges = []
        for r in ranges_raw:
            iri = getattr(r, "iri", None)
            if iri:
                ranges.append(str(iri))
            else:
                ranges.append(str(r))

        prop_meta_list.append({
            "property": p,
            "property_iri": p_iri_str,
            "property_name": p_name,
            "domains": sorted(set(domains)),
            "ranges": sorted(set(ranges)),
        })

    result: Dict[str, List[Dict[str, Any]]] = {cn: [] for cn in class_obj_map.keys()}

    for class_local, cls_obj in class_obj_map.items():
        cls_iri = getattr(cls_obj, "iri", None)
        if not cls_iri:
            continue
        cls_iri_str = str(cls_iri)

        for meta in prop_meta_list:
            domains = meta["domains"]
            ranges  = meta["ranges"]

            if cls_iri_str in domains or cls_iri_str in ranges:
                result[class_local].append({
                    "property_iri": meta["property_iri"],
                    "property_name": meta["property_name"],
                    "domains": domains,
                    "ranges": ranges,
                })

    return result
