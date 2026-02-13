from ontology.explorer import ontology_explorer

if __name__ == "__main__":
    all_classes = ontology_explorer(mode="classes")
    print("Classes:", all_classes[:10], "...")

    dprops = ontology_explorer(mode="data_properties", class_names="Person")
    print("\nData properties for Person:")
    for p in dprops.get("Person", []):
        print(" -", p["property_name"], "domains=", p["domains"], "ranges=", p["ranges"])

    oprops = ontology_explorer(mode="object_properties", class_names="Person")
    print("\nObject properties for Person:")
    for p in oprops.get("Person", []):
        print(" -", p["property_name"], "domains=", p["domains"], "ranges=", p["ranges"])
