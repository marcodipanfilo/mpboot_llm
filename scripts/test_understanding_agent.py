import json
from agents.understanding import understanding

if __name__ == "__main__":
    out = understanding("persons")
    print(json.dumps(out, indent=2))
