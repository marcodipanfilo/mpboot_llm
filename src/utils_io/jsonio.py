import os
import json

def load_json(base: str, name: str):
    path = os.path.join(base, name)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
