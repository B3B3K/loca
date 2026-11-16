"""Dependency manifest parsing + optional OSV.dev lookup (sends only package names/versions)."""
import json
import re
import urllib.request

ECO = {"requirements": "PyPI", "npm": "npm", "go": "Go", "cargo": "crates.io", "composer": "Packagist",
       "gem": "RubyGems"}


def parse_deps(project):
    """Return (deps, unpinned): deps = [(eco, name, version, file)]."""
    deps, unpinned = [], []
    for rel, full in project.iter_files(globs=("requirements*.txt", "package.json", "package-lock.json", "go.mod",
                                                "Cargo.lock", "composer.lock", "Gemfile.lock"), include_all=True):
        name, text = full.name, full.read_text(encoding="utf-8", errors="replace")
        try:
            if name.startswith("requirements"):
                for ln in text.splitlines():
                    ln = ln.split("#")[0].strip()
                    m = re.match(r"^([A-Za-z0-9_.\-]+)(\[[^\]]*\])?\s*(==|>=|~=|<=|>|<|!=)?\s*([\w.\-*]+)?", ln)
                    if not ln or ln.startswith("-") or not m:
                        continue
                    if m.group(3) == "==":
                        deps.append((ECO["requirements"], m.group(1), m.group(4), rel))
                    else:
                        unpinned.append(f"{rel}: {ln}")
            elif name == "package-lock.json":
                data = json.loads(text)
                for k, v in (data.get("packages") or {}).items():
                    if k.startswith("node_modules/") and v.get("version"):
                        deps.append((ECO["npm"], k.split("node_modules/")[-1], v["version"], rel))
                for k, v in (data.get("dependencies") or {}).items():
                    if isinstance(v, dict) and v.get("version"):
                        deps.append((ECO["npm"], k, v["version"], rel))
            elif name == "package.json":
                data = json.loads(text)
                for sect in ("dependencies", "devDependencies"):
                    for k, v in (data.get(sect) or {}).items():
                        if re.match(r"^[\^~<>*]|latest|^x$|git|http", str(v)):
                            unpinned.append(f"{rel}: {k}@{v}")
            elif name == "go.mod":
                for m in re.finditer(r"^\s*(?:require\s+)?([\w.\-/]+\.[\w.\-/]+)\s+(v[\w.\-+]+)", text, re.M):
                    deps.append((ECO["go"], m.group(1), m.group(2), rel))
            elif name == "Cargo.lock":
                for m in re.finditer(r'name = "([^"]+)"\nversion = "([^"]+)"', text):
                    deps.append((ECO["cargo"], m.group(1), m.group(2), rel))
            elif name == "composer.lock":
                for p in json.loads(text).get("packages", []):
                    deps.append((ECO["composer"], p["name"], p["version"].lstrip("v"), rel))
            elif name == "Gemfile.lock":
                for m in re.finditer(r"^    ([\w\-.]+) \(([\d][^)]*)\)$", text, re.M):
                    deps.append((ECO["gem"], m.group(1), m.group(2), rel))
        except (ValueError, KeyError):
            continue
    seen, uniq = set(), []
    for d in deps:
        if d[:3] not in seen:
            seen.add(d[:3])
            uniq.append(d)
    return uniq, unpinned


def _post(url, payload):
    req = urllib.request.Request(url, json.dumps(payload).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def osv_lookup(deps, max_details=30):
    """Return [(eco, name, version, file, vuln_id, summary)]."""
    out = []
    for i in range(0, len(deps), 500):
        chunk = deps[i:i + 500]
        res = _post("https://api.osv.dev/v1/querybatch", {"queries": [
            {"package": {"name": n, "ecosystem": e}, "version": v} for e, n, v, _ in chunk]})
        for d, r in zip(chunk, res.get("results", [])):
            for v in r.get("vulns", []) or []:
                out.append((*d, v["id"], ""))
    for k, row in enumerate(out[:max_details]):
        try:
            with urllib.request.urlopen(f"https://api.osv.dev/v1/vulns/{row[4]}", timeout=20) as r:
                d = json.load(r)
            sev = ""
            for s in d.get("database_specific", {}).get("severity", []) if isinstance(
                    d.get("database_specific", {}).get("severity"), list) else [d.get("database_specific", {}).get("severity", "")]:
                sev = str(s)
            out[k] = (*row[:5], f"{sev} {d.get('summary', '')}".strip()[:120])
        except Exception:
            pass
    return out


# ---------------- curated advisory hints (offline) ----------------
import os as _os
from pathlib import Path as _Path


def _ver(s):
    m = re.match(r"v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", str(s).strip())
    return tuple(int(x or 0) for x in m.groups()) if m else (0, 0, 0)


def affected(version, entry):
    v, intro = _ver(version), _ver(entry.get("introduced", "0"))
    if v < intro:
        return False
    fixed = [_ver(f) for f in entry["fixed"]]
    same_minor = [f for f in fixed if f[:2] == v[:2]]
    if same_minor:
        return any(v < f for f in same_minor)
    same_major = [f for f in fixed if f[0] == v[0]]
    if same_major:
        return any(v < f for f in same_major)
    return bool(entry.get("eol_below_fixed")) and v[0] < min(f[0] for f in fixed)


def load_advisories(extra_dir=None):
    data = json.loads((_Path(__file__).parent / "rules" / "advisories.json").read_text(encoding="utf-8"))
    if extra_dir and (_Path(extra_dir) / "advisories.json").is_file():
        data["entries"] += json.loads((_Path(extra_dir) / "advisories.json").read_text(encoding="utf-8"))["entries"]
    return data


def local_advisories(project, extra_dir=None):
    """Return list of (package, version, exact, file, entry) for curated advisory hits."""
    data, hits, seen = load_advisories(extra_dir), [], set()
    names = {n for e in data["entries"] for n in e["package"]}
    exact = {}
    deps, _ = parse_deps(project)
    for eco, name, ver, rel in deps:
        if name in names:
            exact[(name, ver)] = rel
    declared = {}
    for k, (rng, rel) in project.stack()["packages"].items():
        if k in names and not any(n == k for n, _ in exact):
            declared[k] = (re.sub(r"^[\^~>=<\s]+", "", rng), rel)
    cases = [(n, v, True, rel) for (n, v), rel in exact.items()] + \
            [(n, v, False, rel) for n, (v, rel) in declared.items() if re.match(r"\d", v)]
    for n, v, ex, rel in cases:
        for e in data["entries"]:
            if n in e["package"] and affected(v, e) and (n, v, e["title"]) not in seen:
                seen.add((n, v, e["title"]))
                hits.append((n, v, ex, rel, e))
    return hits
