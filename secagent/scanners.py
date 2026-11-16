"""Optional external scanners (semgrep, bandit, gitleaks, trivy): run if installed, normalise output."""
import json
import os
import shutil
import subprocess
import tempfile

SEV = {"error": "high", "warning": "medium", "info": "low", "critical": "critical", "high": "high",
       "medium": "medium", "low": "low", "unknown": "info", "moderate": "medium"}


def installed():
    return [n for n in ("semgrep", "bandit", "gitleaks", "trivy") if shutil.which(n)]


def _sev(s):
    return SEV.get(str(s).lower(), "info")


def _cwe(x):
    if isinstance(x, list):
        x = x[0] if x else ""
    return str(x).split(":")[0].strip() if x else ""


def parse_semgrep(d):
    for r in d.get("results", []):
        e = r.get("extra", {})
        yield dict(rule=r["check_id"].split(".")[-1], title=e.get("message", "")[:140], severity=_sev(e.get("severity")),
                   file=r["path"], line=r["start"]["line"], cwe=_cwe(e.get("metadata", {}).get("cwe")))


def parse_bandit(d):
    for r in d.get("results", []):
        yield dict(rule=r["test_id"], title=r["issue_text"][:140], severity=_sev(r["issue_severity"]),
                   file=r["filename"], line=r["line_number"], cwe=f"CWE-{r.get('issue_cwe', {}).get('id', '')}")


def parse_gitleaks(d):
    for r in d or []:
        yield dict(rule=r.get("RuleID", "secret"), title=f"Leaked secret: {r.get('Description', '')}"[:140],
                   severity="high", file=r.get("File", ""), line=r.get("StartLine", 0), cwe="CWE-798")


def parse_trivy(d):
    for res in d.get("Results", []) or []:
        tgt = res.get("Target", "")
        for v in res.get("Vulnerabilities") or []:
            s = _sev(v.get("Severity"))
            if s in ("critical", "high", "medium"):
                yield dict(rule=v["VulnerabilityID"], severity=s, file=tgt, line=0, cwe="CWE-1104",
                           title=f"{v.get('PkgName')} {v.get('InstalledVersion')}: {v['VulnerabilityID']} "
                                 f"(fixed: {v.get('FixedVersion', 'n/a')})")
        for m in res.get("Misconfigurations") or []:
            yield dict(rule=m["ID"], title=m.get("Title", "")[:140], severity=_sev(m.get("Severity")), file=tgt,
                       line=(m.get("CauseMetadata") or {}).get("StartLine", 0), cwe="CWE-16")
        for s in res.get("Secrets") or []:
            yield dict(rule=s.get("RuleID", "secret"), title=s.get("Title", "secret")[:140], severity=_sev(s.get("Severity")),
                       file=tgt, line=s.get("StartLine", 0), cwe="CWE-798")


def run(name, target, semgrep_config="auto", timeout=900):
    """Return list of normalised items, or raise RuntimeError."""
    exe = shutil.which(name)
    if not exe:
        raise RuntimeError(f"{name} not installed")
    tmp = None
    if name == "semgrep":
        cmd = [exe, "scan", "--config", semgrep_config, "--json", "--quiet", "--metrics=off", target]
        parse = parse_semgrep
    elif name == "bandit":
        cmd, parse = [exe, "-r", target, "-f", "json", "-q"], parse_bandit
    elif name == "gitleaks":
        tmp = tempfile.mktemp(suffix=".json")
        cmd, parse = [exe, "detect", "--source", target, "--no-git", "--redact", "-f", "json", "-r", tmp], parse_gitleaks
    elif name == "trivy":
        cmd = [exe, "fs", "--quiet", "--scanners", "vuln,secret,misconfig", "-f", "json", target]
        parse = parse_trivy
    else:
        raise RuntimeError("unknown scanner")
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    raw = p.stdout
    if tmp:
        raw = open(tmp).read() if os.path.exists(tmp) else "[]"
        if os.path.exists(tmp):
            os.remove(tmp)
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        raise RuntimeError(f"{name} produced no JSON: {(p.stderr or raw)[-300:]}")
    return list(parse(data))
