import os
import re
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from typing import List, Dict, Any, Optional
from owlready2 import get_ontology

ONTOLOGY_PATH = os.environ.get("MPBOOT_ONTOLOGY_PATH", "src/inputs/ontology/ontology.owl")

_OWL_ONTO = None
_BASE_IRI = None
_LOADED_FROM = None  # filesystem path we actually handed to owlready2 (may be a patched temp file)
_ALL_ONTOLOGY_NAMESPACES: List[str] = []  # all namespaces that contain local-domain classes


def _extract_all_namespaces_from_owl(owl_file_path: str) -> List[str]:
    """
    Parse all xmlns: prefix declarations from the OWL file root element and
    return a list of unique namespace IRIs that are plausibly ontology-local
    (i.e. not standard W3C/XSD/RDF/RDFS/OWL namespaces).

    Works for both RDF/XML (<rdf:RDF xmlns:...>) and OWL/XML (<Ontology>
    with <Prefix name="..." IRI="..."/> children).

    Used to widen _list_local_classes beyond the single base IRI so that
    multi-namespace ontologies (e.g. NPD) expose all their classes.
    """
    SKIP = {
        "http://www.w3.org/2002/07/owl#",
        "http://www.w3.org/2001/XMLSchema#",
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
        "http://www.w3.org/2000/01/rdf-schema#",
        "http://www.w3.org/XML/1998/namespace",
        "http://www.w3.org/2004/02/skos/core#",
        "http://purl.org/dc/elements/1.1/",
        "http://purl.org/dc/terms/",
    }
    namespaces: List[str] = []
    try:
        # Raw-text scan for xmlns[:name]="IRI" — ET doesn't expose these directly
        with open(owl_file_path, "r", encoding="utf-8", errors="replace") as f:
            head = f.read(4096)
        for match in re.finditer(r'xmlns(?::[a-zA-Z0-9_\-]*)?\s*=\s*"([^"]+)"', head):
            iri = match.group(1)
            if iri in SKIP:
                continue
            ns = iri if iri.endswith("#") or iri.endswith("/") else iri + "#"
            if ns not in namespaces:
                namespaces.append(ns)
        try:
            tree = ET.parse(owl_file_path)
            root = tree.getroot()
            for prefix in (root.findall('.//{http://www.w3.org/2002/07/owl#}Prefix') +
                           root.findall('.//Prefix')):
                iri = prefix.get('IRI', '')
                if not iri or iri in SKIP:
                    continue
                ns = iri if iri.endswith("#") or iri.endswith("/") else iri + "#"
                if ns not in namespaces:
                    namespaces.append(ns)
        except Exception:
            pass

    except Exception as e:
        print(f"[_extract_all_namespaces] failed: {e}")
    return namespaces


def _extract_base_iri_from_owl(owl_file_path: str) -> Optional[str]:
    """
    Extract the base IRI from the OWL file by parsing the XML.
    Looks for the default prefix (name="") in the Prefix declarations.
    Falls back to ontologyIRI, then xml:base.
    Returns None if nothing reliable is found.
    """
    try:
        tree = ET.parse(owl_file_path)
        root = tree.getroot()

        namespaces = {
            'owl': 'http://www.w3.org/2002/07/owl#',
            'rdf': 'http://www.w3.org/1999/02/22-rdf-syntax-ns#'
        }

        # Look for Prefix with name="" (default prefix). Works for OWL/XML inside RDF.
        for prefix in root.findall('.//owl:Prefix', namespaces):
            name_attr = prefix.get('name')
            iri_attr = prefix.get('IRI')
            if name_attr == "" and iri_attr:
                print(f"Found base IRI from default prefix: {iri_attr}")
                return iri_attr

        # Pure OWL/XML files put Prefix elements under the owl namespace.
        for prefix in root.findall('.//{http://www.w3.org/2002/07/owl#}Prefix'):
            name_attr = prefix.get('name')
            iri_attr = prefix.get('IRI')
            if name_attr == "" and iri_attr:
                print(f"Found base IRI from default prefix: {iri_attr}")
                return iri_attr

        # ontologyIRI attribute on the root Ontology element
        ontology_iri = root.get('ontologyIRI')
        if ontology_iri:
            base_iri = ontology_iri if ontology_iri.endswith('#') else ontology_iri + '#'
            print(f"Using ontologyIRI as base: {base_iri}")
            return base_iri

        # xml:base attribute (skip the owl ontology URL that OWL/XML files put there by default)
        xml_base = root.get('{http://www.w3.org/XML/1998/namespace}base')
        if xml_base and xml_base != 'http://www.w3.org/2002/07/owl#':
            base_iri = xml_base if xml_base.endswith('#') else xml_base + '#'
            print(f"Using xml:base as base: {base_iri}")
            return base_iri

        return None

    except Exception as e:
        print(f"Error parsing OWL file for base IRI: {e}")
        return None


def _infer_base_iri_from_ontology(onto) -> Optional[str]:
    """Post-load fallback: infer base IRI from the most common entity namespace."""
    def namespace_of(iri: Optional[str]) -> Optional[str]:
        if not iri:
            return None
        skip_prefixes = (
            "http://www.w3.org/2002/07/owl#",
            "http://www.w3.org/2001/XMLSchema#",
            "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
            "http://www.w3.org/2000/01/rdf-schema#",
        )
        for sp in skip_prefixes:
            if iri.startswith(sp):
                return None
        if "#" in iri:
            return iri.rsplit("#", 1)[0] + "#"
        return iri.rsplit("/", 1)[0] + "/"

    counter: Counter = Counter()
    try:
        for c in onto.classes():
            ns = namespace_of(getattr(c, "iri", None))
            if ns:
                counter[ns] += 1
        for p in onto.object_properties():
            ns = namespace_of(getattr(p, "iri", None))
            if ns:
                counter[ns] += 1
        for p in onto.data_properties():
            ns = namespace_of(getattr(p, "iri", None))
            if ns:
                counter[ns] += 1
    except Exception as e:
        print(f"Error inferring base IRI from ontology entities: {e}")
        return None

    if not counter:
        return None

    base_iri, count = counter.most_common(1)[0]
    print(f"Inferred base IRI from entity namespaces: {base_iri} (used by {count} entities)")
    return base_iri


def _patch_owlxml_for_owlready2(src_path: str, real_base_iri: Optional[str]) -> str:
    """
    Some OWL/XML files omit the `ontologyIRI` attribute on the <Ontology> root
    element. owlready2's optimized parser then raises KeyError: 'ontologyIRI'.

    This function writes a patched copy to a temp file with `ontologyIRI="..."`
    injected into the opening <Ontology> tag. The injected IRI is derived from
    the file's real default prefix (stripped of trailing '#') so that owlready2's
    internal bookkeeping matches the entity IRIs and classes appear under
    onto.classes().

    Returns the path of the patched file, or the original path if no patch
    was needed or possible.
    """
    try:
        text = open(src_path, "r", encoding="utf-8").read()
    except Exception:
        return src_path

    # Only OWL/XML files (root element is <Ontology>) hit this owlready2 bug.
    # RDF/XML files (root is <rdf:RDF>) go through a different parser and are fine.
    if "<Ontology" not in text[:1024]:
        return src_path

    m = re.search(r'<Ontology\b([^>]*)>', text)
    if not m:
        return src_path
    if 'ontologyIRI=' in m.group(1):
        return src_path  # already has the attribute

    if not real_base_iri:
        return src_path

    # ontologyIRI is the document IRI — same as the default namespace minus trailing '#'
    onto_iri = real_base_iri.rstrip('#')

    # Preferred: inject right after the xmlns="owl#" attribute
    patched, n = re.subn(
        r'<Ontology(\s+)xmlns="http://www\.w3\.org/2002/07/owl#"',
        f'<Ontology\\1xmlns="http://www.w3.org/2002/07/owl#"\n     ontologyIRI="{onto_iri}"',
        text,
        count=1,
    )
    if n == 0:
        # Fallback: inject as the first attribute after <Ontology
        patched = re.sub(
            r'<Ontology(\s+)',
            f'<Ontology\\1ontologyIRI="{onto_iri}" ',
            text,
            count=1,
        )

    tf = tempfile.NamedTemporaryFile(
        mode='w', encoding='utf-8', suffix='.owl', delete=False
    )
    tf.write(patched)
    tf.close()
    print(f"Patched OWL/XML (injected ontologyIRI={onto_iri}) -> {tf.name}")
    return tf.name


def _load_ontology():
    """Load the ontology and extract the base IRI dynamically."""
    global _OWL_ONTO, _BASE_IRI, _LOADED_FROM, ONTOLOGY_PATH

    ONTOLOGY_PATH = os.environ.get("MPBOOT_ONTOLOGY_PATH", ONTOLOGY_PATH)

    if _OWL_ONTO is None:
        # Extract the real base IRI up front (works regardless of parse strategy)
        base_iri = _extract_base_iri_from_owl(ONTOLOGY_PATH)

        # First attempt: load the file as-is
        load_path = ONTOLOGY_PATH
        try:
            _OWL_ONTO = get_ontology("file://" + load_path).load()
        except Exception as e1:
            # owlready2 OWL/XML parser may raise KeyError: 'ontologyIRI' on files
            # that don't declare that attribute. Auto-patch and retry.
            print(f"Primary ontology load failed: {type(e1).__name__}: {e1}")
            patched_path = _patch_owlxml_for_owlready2(ONTOLOGY_PATH, base_iri)
            if patched_path != ONTOLOGY_PATH:
                try:
                    _OWL_ONTO = get_ontology("file://" + patched_path).load()
                    load_path = patched_path
                    print("Recovered via OWL/XML patch.")
                except Exception as e2:
                    print(f"Patched load also failed: {e2}")
                    raise
            else:
                raise

        _LOADED_FROM = load_path

        # Collect all ontology namespaces (for multi-namespace ontologies like NPD)
        _ALL_ONTOLOGY_NAMESPACES.clear()
        _ALL_ONTOLOGY_NAMESPACES.extend(_extract_all_namespaces_from_owl(ONTOLOGY_PATH))

        # If XML-based extraction failed earlier, fall back to entity-namespace inference
        if not base_iri:
            base_iri = _infer_base_iri_from_ontology(_OWL_ONTO)

        # Sanity-check: verify the extracted base_iri actually contains classes.
        # Some ontologies (e.g. NPD) have a "document IRI" as xml:base (npd-v2-merge#)
        # that contains no classes — all classes live under a different namespace
        # (npd-v2#). In that case, override with the most common class namespace so
        # that shorthand `:ClassName` resolves correctly in generated mappings.
        if base_iri:
            class_count_in_base = sum(
                1 for cls in _OWL_ONTO.classes()
                if str(getattr(cls, "iri", "")).startswith(base_iri)
            )
            if class_count_in_base == 0:
                inferred = _infer_base_iri_from_ontology(_OWL_ONTO)
                if inferred and inferred != base_iri:
                    print(f"  [base_iri] '{base_iri}' has 0 classes — "
                          f"overriding with inferred: '{inferred}'")
                    base_iri = inferred

        if not base_iri:
            print("Warning: Could not extract or infer base IRI, using default")
            base_iri = "http://conference#"

        _BASE_IRI = base_iri

        print(f"Ontology loaded from: {ONTOLOGY_PATH}"
              + (f"  (via patched: {_LOADED_FROM})" if _LOADED_FROM != ONTOLOGY_PATH else ""))
        print(f"Base IRI: {_BASE_IRI}")

    return _OWL_ONTO


def _get_base_iri() -> str:
    global _BASE_IRI
    if _BASE_IRI is None:
        _load_ontology()
    return _BASE_IRI


def _local_name(iri: str) -> str:
    if "#" in iri:
        return iri.split("#")[-1]
    return iri.rsplit("/", 1)[-1]


def _resolve_class(onto, class_name: str):
    """Resolve a class by local name. Three-step chain:
      1. Python attribute access (fast path)
      2. Exact IRI match on the inferred base IRI
      3. Local-name scan across all classes (bypasses base-IRI dependency)
    """
    base_iri = _get_base_iri()

    if hasattr(onto, class_name):
        obj = getattr(onto, class_name)
        if getattr(obj, "iri", None):
            return obj

    try:
        hit = onto.search_one(iri=base_iri + class_name)
        if hit is not None:
            return hit
    except Exception:
        pass

    try:
        for cls in onto.classes():
            iri = getattr(cls, "iri", None)
            if iri and _local_name(str(iri)) == class_name:
                return cls
    except Exception:
        pass

    return None


def _list_local_classes(onto) -> List[str]:
    base_iri = _get_base_iri()

    # Use all known ontology namespaces if available (multi-namespace ontologies
    # like NPD have classes spread across several prefixes, not just the base IRI).
    # Fall back to base IRI only if namespace list is empty.
    namespaces = _ALL_ONTOLOGY_NAMESPACES if _ALL_ONTOLOGY_NAMESPACES else [base_iri]

    out: List[str] = []
    for cls in onto.classes():
        iri = getattr(cls, "iri", None)
        if not iri:
            continue
        iri_str = str(iri)
        if any(iri_str.startswith(ns) for ns in namespaces):
            out.append(_local_name(iri_str))
    return sorted(set(out))


def _parse_xml_prop_domains_ranges(owl_file_path: str, base_iri: str) -> Dict[str, Dict[str, List[str]]]:
    """
    Fallback: parse property domain/range axioms directly from the OWL file using
    ElementTree, supplementing what owlready2 returns.

    Handles TWO OWL serialization styles:

    OWL/XML  (<Ontology> root):
      Uses <ObjectPropertyDomain>/<ObjectPropertyRange> axiom elements with
      <Class abbreviatedIRI=":Foo"/> or <Class IRI="..."/>.
      Supports <ObjectUnionOf>/<ObjectIntersectionOf> — flattened to member classes.

    RDF/XML  (<rdf:RDF> root):
      Uses <owl:ObjectProperty rdf:about="..."> blocks containing
      <rdfs:domain rdf:resource="..."/> and <rdfs:range rdf:resource="..."/> children.
      Also handles <owl:DatatypeProperty> and <rdf:Property> the same way.

    Returns dict keyed by full property IRI:
      { prop_iri: {"domains": [...], "ranges": [...]} }
    """
    OWL_NS  = "http://www.w3.org/2002/07/owl#"
    RDF_NS  = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    RDFS_NS = "http://www.w3.org/2000/01/rdf-schema#"

    result: Dict[str, Dict[str, List[str]]] = {}

    try:
        tree = ET.parse(owl_file_path)
        root = tree.getroot()
    except Exception as e:
        print(f"[xml_prop_meta] Could not parse {owl_file_path}: {e}")
        return result

    root_tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag

    # ── OWL/XML path ──────────────────────────────────────────────────────────
    if root_tag == "Ontology":

        def expand_iri(raw: Optional[str]) -> Optional[str]:
            if not raw:
                return None
            if raw.startswith(":"):
                return base_iri + raw[1:]
            return raw

        def collect_classes(elem) -> List[str]:
            out: List[str] = []
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag == "Class":
                expanded = expand_iri(elem.get("abbreviatedIRI")) or expand_iri(elem.get("IRI"))
                if expanded:
                    out.append(expanded)
            elif tag in ("ObjectUnionOf", "ObjectIntersectionOf"):
                for child in elem:
                    out.extend(collect_classes(child))
            elif tag != "ObjectComplementOf":
                for child in elem:
                    out.extend(collect_classes(child))
            return out

        axiom_tags = {
            f"{{{OWL_NS}}}ObjectPropertyDomain": "domains",
            f"{{{OWL_NS}}}ObjectPropertyRange":  "ranges",
            f"{{{OWL_NS}}}DataPropertyDomain":   "domains",
            f"{{{OWL_NS}}}DataPropertyRange":    "ranges",
            "ObjectPropertyDomain": "domains",
            "ObjectPropertyRange":  "ranges",
            "DataPropertyDomain":   "domains",
            "DataPropertyRange":    "ranges",
        }
        prop_tags = {
            f"{{{OWL_NS}}}ObjectProperty", f"{{{OWL_NS}}}DataProperty",
            "ObjectProperty", "DataProperty",
        }

        for axiom in root:
            slot = axiom_tags.get(axiom.tag)
            if slot is None:
                continue
            children = list(axiom)
            if not children or children[0].tag not in prop_tags:
                continue
            prop_elem = children[0]
            prop_iri = (expand_iri(prop_elem.get("abbreviatedIRI")) or
                        expand_iri(prop_elem.get("IRI")))
            if not prop_iri:
                continue
            if prop_iri not in result:
                result[prop_iri] = {"domains": [], "ranges": []}
            for class_elem in children[1:]:
                for cls_iri in collect_classes(class_elem):
                    if cls_iri not in result[prop_iri][slot]:
                        result[prop_iri][slot].append(cls_iri)

    # ── RDF/XML path ──────────────────────────────────────────────────────────
    else:
        prop_element_tags = {
            f"{{{OWL_NS}}}ObjectProperty",
            f"{{{OWL_NS}}}DatatypeProperty",
            f"{{{OWL_NS}}}AnnotationProperty",
            f"{{{RDF_NS}}}Property",
        }
        domain_tag = f"{{{RDFS_NS}}}domain"
        range_tag  = f"{{{RDFS_NS}}}range"
        about_attr = f"{{{RDF_NS}}}about"
        res_attr   = f"{{{RDF_NS}}}resource"

        for elem in root:
            if elem.tag not in prop_element_tags:
                continue
            prop_iri = elem.get(about_attr) or elem.get(f"{{{RDF_NS}}}ID")
            if not prop_iri:
                continue
            if prop_iri not in result:
                result[prop_iri] = {"domains": [], "ranges": []}
            for child in elem:
                if child.tag == domain_tag:
                    val = child.get(res_attr)
                    if val and val not in result[prop_iri]["domains"]:
                        result[prop_iri]["domains"].append(val)
                elif child.tag == range_tag:
                    val = child.get(res_attr)
                    if val and val not in result[prop_iri]["ranges"]:
                        result[prop_iri]["ranges"].append(val)

    return result


def _prop_meta(onto) -> Dict[str, List[Dict[str, Any]]]:
    # ── XML fallback: parse domain/range directly from the OWL file ──────────
    # owlready2 silently returns [] for domains expressed as ObjectUnionOf (OWL/XML)
    # and may miss rdfs:domain/range on RDF/XML ontologies with complex structures.
    # We parse the raw XML and merge results below — only filling gaps, never
    # overwriting what owlready2 already resolved.
    xml_dr: Dict[str, Dict[str, List[str]]] = {}
    try:
        src = ONTOLOGY_PATH
        base_iri = _get_base_iri()
        if src and os.path.isfile(src):
            xml_dr = _parse_xml_prop_domains_ranges(src, base_iri)
    except Exception as e:
        print(f"[xml_prop_meta] fallback parse failed: {e}")

    def one_list(props):
        meta_list = []
        for p in props:
            p_iri = getattr(p, "iri", None)
            if not p_iri:
                continue
            p_iri = str(p_iri)

            # owlready2 path (unchanged)
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

            # ── merge XML fallback results (additive only) ────────────────────
            xml_entry = xml_dr.get(p_iri, {})
            for d in xml_entry.get("domains", []):
                if d not in domains:
                    domains.append(d)
            for r in xml_entry.get("ranges", []):
                if r not in ranges:
                    ranges.append(r)

            meta_list.append({
                "property_iri": p_iri,
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
        linked: set[str] = set()
        links: List[Dict[str, Any]] = []

        for m in meta["object_properties"]:
            touches_as_domain = cls_iri in m["domains"]
            touches_as_range = cls_iri in m["ranges"]
            if not (touches_as_domain or touches_as_range):
                continue

            domain_locals = [
                _local_name(d) for d in m["domains"]
                if d.startswith(base_iri)
            ]
            range_locals = [
                _local_name(r) for r in m["ranges"]
                if isinstance(r, str) and r.startswith(base_iri)
            ]

            others: List[str] = []
            if touches_as_domain:
                others.extend(range_locals)
            if touches_as_range:
                others.extend(domain_locals)

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