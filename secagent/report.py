"""Report writers: Markdown, JSON (findings.json already), SARIF 2.1.0."""
import datetime
import json

from .findings import SEV_ORDER


def verdict(findings, cands=None):
    """BLOCK / CONDITIONAL / OK, or INCOMPLETE when high/critical scanner candidates were never triaged."""
    c = {s: 0 for s in SEV_ORDER}
    for f in findings:
        c[f["severity"]] += 1
    pend = len(cands.pending("high")) if cands is not None else 0
    c["untriaged_high_critical"] = pend
    if c["critical"] or c["high"]:
        return "BLOCK", c
    if pend:
        return "INCOMPLETE", c
    if c["medium"]:
        return "CONDITIONAL", c
    return "OK", c


def to_markdown(project, findings, cands, summary=""):
    items = findings.sorted()
    v, c = verdict(items, cands)
    out = [f"# Security review: {project.root.name}", "",
           f"Date: {datetime.date.today()}  |  Production verdict (automated): **{v}**", "",
           "| Critical | High | Medium | Low | Info |", "|---|---|---|---|---|",
           f"| {c['critical']} | {c['high']} | {c['medium']} | {c['low']} | {c['info']} |", "",
           f"Candidate triage: {cands.stats() or 'none'}"
           + (f" ({c['untriaged_high_critical']} high/critical candidates were NOT triaged: verdict is incomplete)"
              if c["untriaged_high_critical"] else "") + ". "
           "Unverified findings could not be matched to the cited code and need manual confirmation.", ""]
    if summary:
        out += ["## Executive summary", "", summary.strip(), ""]
    out.append("## Findings")
    for f in items:
        out += ["", f"### {f['id']} [{f['severity'].upper()}] {f['title']}",
                f"`{f['file']}:{f['line']}`  |  {f.get('cwe') or 'CWE n/a'}  |  confidence: {f['confidence']}  |  "
                + ("verified" if f["verified"] else "**UNVERIFIED**"), "",
                "```", f["evidence"], "```"]
        if f["impact"]:
            out += ["", f"**Impact:** {f['impact']}"]
        if f["fix"]:
            out += ["", f"**Fix:** {f['fix']}"]
    if not items:
        out += ["", "_No findings recorded._"]
    return "\n".join(out) + "\n"


def to_sarif(findings):
    lvl = {"critical": "error", "high": "error", "medium": "warning", "low": "note", "info": "note"}
    rules, results = {}, []
    for f in findings.sorted():
        rid = f.get("cwe") or "SEC-GENERIC"
        rules.setdefault(rid, {"id": rid, "shortDescription": {"text": rid}})
        results.append({"ruleId": rid, "level": lvl[f["severity"]],
                        "message": {"text": f"{f['title']}. {f['impact']} Fix: {f['fix']}".strip()},
                        "locations": [{"physicalLocation": {"artifactLocation": {"uri": f["file"]},
                                                            "region": {"startLine": f["line"]}}}],
                        "properties": {"id": f["id"], "verified": f["verified"], "confidence": f["confidence"]}})
    return {"version": "2.1.0", "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
            "runs": [{"tool": {"driver": {"name": "secagent", "rules": list(rules.values())}}, "results": results}]}


def write_all(out_dir, project, findings, cands, summary=""):
    (out_dir / "REPORT.md").write_text(to_markdown(project, findings, cands, summary), encoding="utf-8")
    (out_dir / "findings.sarif").write_text(json.dumps(to_sarif(findings), indent=1), encoding="utf-8")
