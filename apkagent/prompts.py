"""File-based prompt store. Templates use @@NAME@@ placeholders.
Override any built-in prompt by putting a same-named file in your own directory."""
import json
import re
from pathlib import Path

BUILTIN = Path(__file__).parent / "prompts"
_VAR = re.compile(r"@@([A-Z_]+)@@")


class PromptStore:
    def __init__(self, *override_dirs):
        self.dirs = [Path(d) for d in override_dirs if d] + [BUILTIN]

    def path(self, name):
        for d in self.dirs:
            p = d / name
            if p.is_file():
                return p
        raise FileNotFoundError(f"prompt not found: {name} (searched {[str(d) for d in self.dirs]})")

    def raw(self, name):
        return self.path(name).read_text(encoding="utf-8")

    def text(self, name, **v):
        return _VAR.sub(lambda m: str(v.get(m.group(1), m.group(0))), self.raw(name)).strip()

    def json(self, name):
        return json.loads(self.raw(name))
