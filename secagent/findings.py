"""Persistent stores: candidate queue (scanner hits to triage) and verified findings."""
import json
from pathlib import Path

SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


def _load(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


class Candidates:
    def __init__(self, path):
        self.path = Path(path)
        self.items = _load(self.path, {})

    def save(self):
        self.path.write_text(json.dumps(self.items, ensure_ascii=False, indent=1), encoding="utf-8")

    def add(self, rule, title, severity, cwe, file, line, source):
        key = (rule, file, int(line or 0))
        for it in self.items.values():
            if (it["rule"], it["file"], it["line"]) == key:
                return None
        cid = f"C{len(self.items) + 1:03d}"
        self.items[cid] = {"id": cid, "rule": rule, "title": title, "severity": severity.lower(), "cwe": cwe,
                           "file": file, "line": int(line or 0), "source": source, "status": "pending", "note": ""}
        return cid

    def pending(self, min_severity="info"):
        lim = SEV_ORDER.get(min_severity, 4)
        rows = [i for i in self.items.values() if i["status"] == "pending" and SEV_ORDER.get(i["severity"], 4) <= lim]
        return sorted(rows, key=lambda i: (SEV_ORDER.get(i["severity"], 4), i["id"]))

    def resolve(self, cid, status, note=""):
        it = self.items.get(cid)
        if not it:
            return False
        it["status"], it["note"] = status, note[:200]
        self.save()
        return True

    def stats(self):
        s = {}
        for i in self.items.values():
            s[i["status"]] = s.get(i["status"], 0) + 1
        return s


class FindingStore:
    def __init__(self, path):
        self.path = Path(path)
        self.items = _load(self.path, [])
        self.rejected = set()

    def save(self):
        self.path.write_text(json.dumps(self.items, ensure_ascii=False, indent=1), encoding="utf-8")

    def add(self, **f):
        for it in self.items:
            if (it["file"], it["line"], it["title"].lower()) == (f["file"], f["line"], f["title"].lower()):
                return None
        f["id"] = f"F-{len(self.items) + 1:03d}"
        self.items.append(f)
        self.save()
        return f["id"]

    def sorted(self):
        return sorted(self.items, key=lambda f: (SEV_ORDER.get(f["severity"], 4), f["id"]))
