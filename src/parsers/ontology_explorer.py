import os
import xml.etree.ElementTree as ET
from typing import List, Dict, Any, Optional
from owlready2 import get_ontology

ONTOLOGY_PATH = os.environ.get("MPBOOT_ONTOLOGY_PATH", "src/inputs/ontology/ontology.owl")

_OWL_ONTO = None
_BASE_IRI = None


def _extract_base_iri_from_owl(owl_file_path: str) -> str:
    """
    Extract the base IRI from the OWL file by parsing the XML.
    Looks for the default prefix (name="") in the Prefix declarations.
    Falls back to ontologyIRI if no default prefix is found.
    """
    try:
        tree = ET.parse(owl_file_path)
        root = tree.getroot()
        
        # Define namespace for OWL
        namespaces = {
            'owl': 'http://www.w3.org/2002/07/owl#',
            'rdf': 'http://www.w3.org/1999/02/22-rdf-syntax-ns#'
        }
        
        # Look for Prefix with name="" (default prefix)
        for prefix in root.findall('.//owl:Prefix', namespaces):
            name_attr = prefix.get('name')
            iri_attr = prefix.get('IRI')
            
            if name_attr == "" and iri_attr:
                print(f"Found base IRI from default prefix: {iri_attr}")
                return iri_attr
        
        # Fallback: use ontologyIRI attribute from root Ontology element
        ontology_iri = root.get('ontologyIRI')
        if ontology_iri:
            base_iri = ontology_iri if ontology_iri.endswith('#') else ontology_iri + '#'
            print(f"Using ontologyIRI as base: {base_iri}")
            return base_iri
        
        # Fallback: use xml:base attribute
        xml_base = root.get('{http://www.w3.org/XML/1998/namespace}base')
        if xml_base:
            base_iri = xml_base if xml_base.endswith('#') else xml_base + '#'
            print(f"Using xml:base as base: {base_iri}")
            return base_iri

        # Fallback: infer the dominant local ontology namespace from entity IRIs.
        namespaces = {
            'owl': 'http://www.w3.org/2002/07/owl#',
            'rdf': 'http://www.w3.org/1999/02/22-rdf-syntax-ns#',
            'rdfs': 'http://www.w3.org/2000/01/rdf-schema#',
        }
        iri_counts = {}

        def _bump(iri_value: str):
            if not iri_value or iri_value.startswith((
                'http://www.w3.org/',
                'https://www.w3.org/',
            )):
                return
            if '#' in iri_value:
                ns = iri_value.rsplit('#', 1)[0] + '#'
            elif '/' in iri_value:
                ns = iri_value.rsplit('/', 1)[0] + '/'
            else:
                return
            iri_counts[ns] = iri_counts.get(ns, 0) + 1

        for path in (
            './/owl:Class',
            './/owl:ObjectProperty',
            './/owl:DatatypeProperty',
            './/rdf:Description',
        ):
            for elem in root.findall(path, namespaces):
                _bump(
                    elem.get(f'{{{namespaces["rdf"]}}}about')
                    or elem.get(f'{{{namespaces["rdf"]}}}ID')
                    or ''
                )

        if iri_counts:
            base_iri = max(iri_counts.items(), key=lambda item: item[1])[0]
            print(f"Inferred base IRI from entity IRIs: {base_iri}")
            return base_iri
        
        # Last resort fallback
        print("Warning: Could not extract base IRI, using default")
        return "http://conference#"
        
    except Exception as e:
        print(f"Error parsing OWL file for base IRI: {e}")
        return "http://conference#"


def _load_ontology():
    """Load the ontology and extract the base IRI dynamically"""
    global _OWL_ONTO, _BASE_IRI, ONTOLOGY_PATH
    
    ONTOLOGY_PATH = os.environ.get("MPBOOT_ONTOLOGY_PATH", ONTOLOGY_PATH)
    
    if _OWL_ONTO is None:
        # Extract base IRI before loading
        _BASE_IRI = _extract_base_iri_from_owl(ONTOLOGY_PATH)
        
        # Load the ontology
        _OWL_ONTO = get_ontology("file://" + ONTOLOGY_PATH).load()
        
        print(f"Ontology loaded from: {ONTOLOGY_PATH}")
        print(f"Base IRI: {_BASE_IRI}")
    
    return _OWL_ONTO


def _get_base_iri() -> str:
    """Get the dynamically loaded base IRI"""
    global _BASE_IRI
    if _BASE_IRI is None:
        _load_ontology()  # This will set _BASE_IRI
    return _BASE_IRI


def _local_name(iri: str) -> str:
    """Extract local name from IRI"""
    if "#" in iri:
        return iri.split("#")[-1]
    return iri.rsplit("/", 1)[-1]


def _resolve_class(onto, class_name: str):
    """Resolve a class by local name to an Owlready2 class object, or None."""
    base_iri = _get_base_iri()
    
    if hasattr(onto, class_name):
        return getattr(onto, class_name)

    try:
        return onto.search_one(iri=base_iri + class_name)
    except Exception:
        return None


def _list_local_classes(onto) -> List[str]:
    """List local classes filtered by BASE_IRI."""
    base_iri = _get_base_iri()
    
    out: List[str] = []
    for cls in onto.classes():
        iri = getattr(cls, "iri", None)
        if not iri:
            continue
        if str(iri).startswith(base_iri):
            out.append(_local_name(str(iri)))
    return sorted(set(out))


def _prop_meta(onto) -> Dict[str, List[Dict[str, Any]]]:
    """
    Build metadata lists for both data + object properties.
    Returns:
      {
        "data_properties": [...],
        "object_properties": [...]
      }
    Each item has: property_iri, property_name, domains, ranges
    """
    def one_list(props):
        meta_list = []
        for p in props:
            p_iri = getattr(p, "iri", None)
            if not p_iri:
                continue

            domains = [
                str(d.iri) for d in getattr(p, "domain", [])
                if getattr(d, "iri", None)
            ]

            ranges_raw = getattr(p, "range", [])
            ranges: List[str] = []
            for r in ranges_raw:
                iri = getattr(r, "iri", None)
                if iri:
                    ranges.append(str(iri))
                else:
                    ranges.append(str(r))

            meta_list.append({
                "property_iri": str(p_iri),
                "property_name": p.name,
                "domains": sorted(set(domains)),
                "ranges": sorted(set(ranges)),
            })
        return meta_list

    return {
        "data_properties": one_list(list(onto.data_properties())),
        "object_properties": one_list(list(onto.object_properties())),
    }


def ontology_explorer(
    mode: str = "classes",
    class_name: Optional[str] = None
) -> Dict[str, Any]:
    """
    Modes (JSON outputs):
      - "classes":
          {"mode":"classes","base_iri":..., "classes":[...]}
      - "class_properties":
          {"mode":"class_properties","class":..., "base_iri":..., "data_properties":[...], "object_properties":[...]}
      - "linked_classes":
          {"mode":"linked_classes","class":..., "base_iri":..., "linked_classes":[...], "links":[...]}
          where links contains per-property edges describing the connection.
    """
    onto = _load_ontology()
    base_iri = _get_base_iri()

    if mode == "classes":
        return {
            "mode": "classes",
            "base_iri": base_iri,
            "classes": _list_local_classes(onto),
        }

    if not class_name:
        raise ValueError("class_name is required for mode 'class_properties' and 'linked_classes'")

    cls_obj = _resolve_class(onto, class_name)
    if cls_obj is None or not getattr(cls_obj, "iri", None):
        return {
            "mode": mode,
            "base_iri": base_iri,
            "class": class_name,
            "error": "Class not found in ontology",
        }

    cls_iri = str(getattr(cls_obj, "iri"))

    meta = _prop_meta(onto)

    if mode == "class_properties":
        def filter_for_class(prop_list):
            out = []
            for m in prop_list:
                if cls_iri in m["domains"] or cls_iri in m["ranges"]:
                    out.append(m)
            return out

        return {
            "mode": "class_properties",
            "base_iri": base_iri,
            "class": class_name,
            "class_iri": cls_iri,
            "data_properties": filter_for_class(meta["data_properties"]),
            "object_properties": filter_for_class(meta["object_properties"]),
        }

    if mode == "linked_classes":
        # Linked classes are derived from object properties that touch this class in domain or range.
        linked: set[str] = set()
        links: List[Dict[str, Any]] = []

        for m in meta["object_properties"]:
            touches_as_domain = cls_iri in m["domains"]
            touches_as_range = cls_iri in m["ranges"]
            if not (touches_as_domain or touches_as_range):
                continue

            # Extract class IRIs from domains/ranges that look like BASE_IRI + LocalName
            domain_locals = [
                _local_name(d) for d in m["domains"]
                if d.startswith(base_iri)
            ]
            range_locals = [
                _local_name(r) for r in m["ranges"]
                if isinstance(r, str) and r.startswith(base_iri)
            ]

            # Determine "other side" classes relative to class_name
            others: List[str] = []
            if touches_as_domain:
                others.extend(range_locals)
            if touches_as_range:
                others.extend(domain_locals)

            # Remove self, add to linked
            others = [o for o in others if o and o != class_name]
            for o in others:
                linked.add(o)

            links.append({
                "property_iri": m["property_iri"],
                "property_name": m["property_name"],
                "touches": {
                    "as_domain": touches_as_domain,
                    "as_range": touches_as_range,
                },
                "domains": m["domains"],
                "ranges": m["ranges"],
                "linked_classes_via_property": sorted(set(others)),
            })

        return {
            "mode": "linked_classes",
            "base_iri": base_iri,
            "class": class_name,
            "class_iri": cls_iri,
            "linked_classes": sorted(linked),
            "links": links,
        }

    raise ValueError("mode must be 'classes', 'class_properties', or 'linked_classes'")
