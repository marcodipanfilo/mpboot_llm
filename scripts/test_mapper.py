import json
from agents.mapper import mapper

if __name__ == "__main__":
    out = mapper("persons", base_prefix="http://conference#")
    print(json.dumps(out["plan_json"], indent=2))
    print(json.dumps(out["column_map_json"], indent=2))
    print(out["r2rml_ttl"])
