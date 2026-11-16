"""Rule loader: built-in rules/*.json plus optional user rules dir (same file name, additive)."""
import json
import re
from pathlib import Path

BUILTIN = Path(__file__).parent / "rules"


def load_rules(name, extra_dir=None):
    rules = json.loads((BUILTIN / name).read_text(encoding="utf-8"))
    if extra_dir and (Path(extra_dir) / name).is_file():
        rules += json.loads((Path(extra_dir) / name).read_text(encoding="utf-8"))
    out = []
    for r in rules:
        try:
            r = dict(r)
            r["rx"] = re.compile(r["pattern"])
            out.append(r)
        except re.error:
            continue
    return out
