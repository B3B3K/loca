"""Security tools registered into an apkagent ToolRegistry."""
import math
import os
import re
from collections import defaultdict
from fnmatch import fnmatch

from apkagent.tools import as_bool, as_int

from . import deps as depmod
from . import scanners as scan
from .findings import SEV_ORDER
from .project import MANIFESTS, SENSITIVE_NAMES
from .rules import load_rules

ROUTE_RE = re.compile(
    r"""(@\w+\.(route|get|post|put|delete|patch)\s*\(|@(app|router|bp|blueprint)\.\w+\s*\(|\b(app|router)\.(get|post|put|delete|patch|use|all)\s*\(\s*["'`/]|"""
    r"""@(Get|Post|Put|Delete|Patch|Request)Mapping|@(Controller|RestController|Path|WebServlet)\b|\bpath\(\s*["'r]|\burl\(\s*r?["']|"""
    r"""http\.HandleFunc|\.(GET|POST|PUT|DELETE)\(\s*"/|\bHandle(Func)?\(|^\s*(get|post|put|delete|patch)\s+["']/|\$_(GET|POST|REQUEST)\b|"""
    r"""\bargparse\b|sys\.argv|\bclick\.command|@Scheduled|@KafkaListener|@SqsListener|lambda_handler)""")
PLACEHOLDER = ("example", "changeme", "change_me", "your", "xxx", "dummy", "sample", "placeholder", "<", ">", "${",
               "{{", "process.env", "os.environ", "getenv", "null", "none", "true", "false", "password", "secret")


def entropy(s):
    if not s:
        return 0.0
    return -sum(c / len(s) * math.log2(c / len(s)) for c in (s.count(x) for x in set(s)))


def mask(v):
    return f"{v[:4]}***({len(v)})"


def redact(text, rules):
    for r in rules:
        text = r["rx"].sub(lambda m: m.group(0).replace(m.group(r["group"] or 0), mask(m.group(r["group"] or 0))), text)
    return text


def register_security_tools(reg, project, findings, cands, *, rules_dir=None, online=False,
                            semgrep_config="auto", allow_scanners=True):
    sast_rules = load_rules("sast.json", rules_dir)
    secret_rules = load_rules("secrets.json", rules_dir)

    def queue(item, source):
        return cands.add(item["rule"], item["title"], item["severity"], item.get("cwe", ""),
                         item["file"], item.get("line", 0), source)

    @reg.register()
    def tree():
        langs = project.languages()
        names, sens = [], []
        for rel, full in project.iter_files(include_all=True):
            if full.name in MANIFESTS or full.name.startswith("Dockerfile") or rel.startswith(".github/workflows"):
                names.append(rel)
            if any(fnmatch(full.name, g) for g in SENSITIVE_NAMES):
                sens.append(rel)
        top = [f"{k}: {v}" for k, v in list(langs.items())[:10]]
        return "\n".join([f"files: {project.count_files()}", "languages: " + (", ".join(top) or "(none)"),
                          "layout:", project.layout(),
                          "manifests/CI: " + (", ".join(names[:30]) or "(none)"),
                          "sensitive-looking files (review, are they committed?): " + (", ".join(sens[:30]) or "(none)")])

    @reg.register()
    def entrypoints(path="", limit=60):
        out = []
        for rel, full in project.iter_files(path, globs=("*.py", "*.js", "*.jsx", "*.ts", "*.tsx", "*.java", "*.kt",
                                                         "*.go", "*.rb", "*.php")):
            for n, ln in enumerate(project.lines(full), 1):
                if len(ln) < 400 and ROUTE_RE.search(ln):
                    out.append(f"{rel}:{n}: {ln.strip()[:150]}")
                    if len(out) >= as_int(limit, 60):
                        return "\n".join(out) + "\n... (limit reached; narrow with path=)"
        return "\n".join(out) or "(no entry points detected by pattern)"

    @reg.register()
    def find(pattern, path="", glob="*", ignore_case=True, files_only=False, limit=40):
        rx = re.compile(pattern, re.I if as_bool(ignore_case, True) else 0)
        limit, fo, hits = as_int(limit, 40), as_bool(files_only), []
        for rel, full in project.iter_files(path, (glob,)):
            for n, ln in enumerate(project.lines(full), 1):
                if len(ln) < 1500 and rx.search(ln):
                    hits.append(rel if fo else f"{rel}:{n}: {ln.strip()[:200]}")
                    if fo:
                        break
                    if len(hits) >= limit:
                        return "\n".join(hits) + f"\n... (limit {limit}; narrow the query)"
            if fo and len(hits) >= limit:
                break
        return "\n".join(hits) or "(no matches)"

    @reg.register()
    def read(file, start=1, end=None):
        lines = project.lines(project.resolve(file))
        s = max(1, as_int(start, 1))
        e = min(as_int(end, s + 119), s + 249, len(lines))
        return "\n".join(f"{i:5d}  {lines[i - 1][:300]}" for i in range(s, e + 1)) or "(empty range)"

    @reg.register()
    def ls(path=""):
        base = project.resolve(path)
        rows = []
        for e in sorted(base.iterdir(), key=lambda x: (not x.is_dir(), x.name)):
            if e.is_dir():
                rows.append(f"{e.name}/" + ("  (ignored)" if e.name in project.excludes else
                                            f"  ({sum(len(f) for _, _, f in os.walk(e))} files)"))
            else:
                rows.append(f"{e.name}  ({e.stat().st_size} B)")
        return "\n".join(rows) or "(empty)"

    @reg.register()
    def sast_scan(category="", path="", limit=25):
        sel = [r for r in sast_rules if not category or category.lower() in (r.get("category", ""), r["id"].lower())
               or category.lower() in r["id"].lower()]
        hits = defaultdict(list)
        for rel, full in project.iter_files(path):
            app = [r for r in sel if any(fnmatch(full.name, g) for g in r["globs"])]
            if not app:
                continue
            for n, ln in enumerate(project.lines(full), 1):
                if len(ln) > 1500:
                    continue
                for r in app:
                    if r["rx"].search(ln):
                        hits[r["id"]].append((rel, n, ln.strip()[:140]))
        meta = {r["id"]: r for r in sel}
        rows, added = [], 0
        for rid in sorted(hits, key=lambda k: (SEV_ORDER[meta[k]["severity"]], -len(hits[k]))):
            r = meta[rid]
            for rel, n, _ in hits[rid][:30]:
                added += bool(cands.add(rid, r["title"], r["severity"], r["cwe"], rel, n, "sast_scan"))
            rows.append(f"[{r['severity'].upper()}] {rid} ({r['cwe']}) x{len(hits[rid])}: {r['title']}")
            rows += [f"    {rel}:{n}: {code}" for rel, n, code in hits[rid][:2]]
        cands.save()
        head = f"{sum(map(len, hits.values()))} hits in {len(hits)} rules; {added} new candidates queued (use next_candidates)."
        return head + "\n" + "\n".join(rows[:as_int(limit, 25) * 3])

    @reg.register()
    def secrets_scan(path=""):
        hits, added = [], 0
        for rel, full in project.iter_files(path, include_all=False):
            for n, ln in enumerate(project.lines(full), 1):
                if len(ln) > 2000:
                    continue
                for r in secret_rules:
                    if r.get("globs") and not any(fnmatch(full.name, g) for g in r["globs"]):
                        continue
                    if any(fnmatch(full.name, g) for g in r.get("exclude_globs", [])):
                        continue
                    if r["group"] and re.search(r"\.(example|sample|template)$", full.name):
                        continue  # placeholders live in example files: only fixed-format tokens are checked there
                    m = r["rx"].search(ln)
                    if not m:
                        continue
                    val = m.group(r["group"]) if r["group"] else m.group(0)
                    if r["min_entropy"]:
                        if any(p in val.lower() for p in PLACEHOLDER) or entropy(val) < r["min_entropy"]:
                            continue
                    hits.append(f"[{r['severity'].upper()}] {r['id']} {rel}:{n}: {mask(val)}")
                    added += bool(cands.add(r["id"], f"Possible hardcoded secret: {r['title']}", r["severity"],
                                            "CWE-798", rel, n, "secrets_scan"))
                    break  # one finding per line: specific rules come before the generic one
        cands.save()
        return f"{len(hits)} possible secrets (values masked); {added} new candidates queued.\n" + "\n".join(hits[:40])

    @reg.register()
    def dep_audit(online=False):
        deps, unpinned = depmod.parse_deps(project)
        by = defaultdict(int)
        for d in deps:
            by[d[0]] += 1
        out = [f"dependencies (pinned versions): {dict(by) or 'none found'}", f"unpinned/ranged: {len(unpinned)}"]
        out += [f"  {u}" for u in unpinned[:15]]
        hints = depmod.local_advisories(project, rules_dir)
        if hints:
            out.append("curated advisory hints (offline list, verify with npm audit):")
        for n, v, ex, rel, e in hints:
            out.append(f"  {n}@{v}{'' if ex else ' (declared range base; check lockfile)'} in {rel}: {e['title'][:110]} "
                       f"-> fixed {', '.join(e['fixed'])}")
            cands.add("ADV-" + n, f"{n}@{v} below patched releases: {e['title'][:100]} (fixed {', '.join(e['fixed'])})",
                      e["severity"], "CWE-1104", rel, 0, "advisory-hints")
        cands.save()
        if as_bool(online):
            if not online_allowed:
                out.append("online CVE lookup disabled (start with --online to allow OSV.dev queries)")
            else:
                try:
                    vulns = depmod.osv_lookup(deps)
                    out.append(f"known vulnerabilities: {len(vulns)}")
                    for e, n, v, f, vid, summ in vulns[:40]:
                        out.append(f"  {n}@{v} {vid} {summ}")
                        cands.add(vid, f"Vulnerable dependency {n}@{v}: {vid} {summ}"[:160], "high", "CWE-1104", f, 0, "osv")
                    cands.save()
                except Exception as e:
                    out.append(f"OSV lookup failed: {e}")
        else:
            out.append("offline mode: CVE lookup not performed (pass online=true if allowed, or run trivy).")
        return "\n".join(out)

    online_allowed = online

    @reg.register()
    def scanners():
        inst = scan.installed()
        return ("installed scanners: " + ", ".join(inst)) if inst else "no external scanners installed (use sast_scan/secrets_scan)"

    @reg.register()
    def run_scanner(name, path=""):
        if not allow_scanners:
            return "external scanners disabled"
        items = scan.run(name, str(project.resolve(path)), semgrep_config)
        added, by = 0, defaultdict(int)
        for it in items:
            f = it["file"]
            if os.path.isabs(f):
                try:
                    f = project.rel(f)
                except ValueError:
                    pass
            it["file"] = f
            by[it["severity"]] += 1
            added += bool(queue(it, name))
        cands.save()
        return f"{name}: {len(items)} results {dict(by)}; {added} new candidates queued (use next_candidates)."

    @reg.register()
    def next_candidates(n=5, min_severity="info"):
        pend = cands.pending(min_severity)
        if not pend:
            return f"no pending candidates. stats: {cands.stats()}"
        out = [f"{len(pend)} pending (showing {min(len(pend), as_int(n, 5))}); stats: {cands.stats()}"]
        for c in pend[:as_int(n, 5)]:
            out.append(f"\n### {c['id']} [{c['severity'].upper()}] {c['rule']} {c['cwe']} - {c['title']}\n{c['file']}:{c['line']}")
            if c["line"] <= 0:
                out.append("  (dependency/file-level finding: inspect the manifest or lockfile, no code line)")
                continue
            try:
                lines = project.lines(project.resolve(c["file"]))
                lo = max(1, c["line"] - 3)
                for i in range(lo, min(len(lines), c["line"] + 3) + 1):
                    out.append(f"{'>' if i == c['line'] else ' '}{i:5d}  {lines[i - 1][:160]}")
            except Exception:
                out.append("  (file not readable)")
        return "\n".join(out)

    @reg.register()
    def dismiss(ids, reason):
        ids = [x.strip() for x in (ids if isinstance(ids, list) else str(ids).replace(";", ",").split(",")) if str(x).strip()]
        ok = [i for i in ids if cands.resolve(i, "dismissed", str(reason))]
        return f"dismissed {ok}" + (f"; unknown ids {sorted(set(ids) - set(ok))}" if len(ok) != len(ids) else "")

    @reg.register()
    def report_finding(title, severity, file, line, evidence, cwe="", impact="", fix="", confidence="medium",
                       candidate_id=""):
        sev = str(severity).lower()
        if sev not in SEV_ORDER:
            return f"error: severity must be one of {', '.join(SEV_ORDER)}"
        try:
            full = project.resolve(file)
            lines = project.lines(full)
        except Exception:
            return f"error: file '{file}' not found. Use a path relative to the project root (see find/ls)."
        line = as_int(line, 0)
        if not 1 <= line <= len(lines):
            return f"error: line must be 1..{len(lines)}"
        if not str(evidence).strip():
            return "error: evidence required: copy the exact vulnerable code line(s)."
        norm = lambda s: re.sub(r"\s+", " ", s).strip()
        needle = norm(str(evidence).splitlines()[0])[:80]
        window = norm(" ".join(lines[max(0, line - 6):line + 5]))
        verified = bool(needle) and needle in window
        key = (file, line, str(title).lower())
        if not verified and key not in findings.rejected:
            findings.rejected.add(key)
            return (f"error: evidence not found near {file}:{line}. Re-read the code and copy the exact line, "
                    "or fix the line number. (Resubmitting the same finding will store it as UNVERIFIED.)")
        fid = findings.add(title=str(title)[:160], severity=sev, file=str(file), line=line, cwe=str(cwe),
                           evidence=redact(str(evidence)[:600], secret_rules), impact=str(impact)[:500],
                           fix=str(fix)[:600], confidence=str(confidence).lower(), verified=verified)
        if fid is None:
            return "duplicate finding ignored"
        if candidate_id:
            cands.resolve(candidate_id, "reported", fid)
        return f"recorded {fid} ({'verified' if verified else 'UNVERIFIED'})"

    @reg.register()
    def findings_list():
        rows = [f"{f['id']} [{f['severity']}] {f['title']} - {f['file']}:{f['line']}" + ("" if f["verified"] else " (unverified)")
                for f in findings.sorted()]
        return "\n".join(rows) or "(no findings yet)"
    reg.add("findings", findings_list)


    from .nextjs import register_nextjs_tools
    register_nextjs_tools(reg, project, cands, rules_dir)
