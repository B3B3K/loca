"""python -m secagent --project ./myapp --backend ollama --model qwen2.5-coder:14b"""
import argparse
import sys

from apkagent.cli import reporter

from . import LLM, review
from .findings import SEV_ORDER


def main(argv=None):
    p = argparse.ArgumentParser(prog="secagent", description="Local agentic pre-production security review")
    p.add_argument("--project", required=True); p.add_argument("--out", default="security_out")
    p.add_argument("--backend", choices=["llama", "ollama"], default="ollama")
    p.add_argument("--url"); p.add_argument("--model"); p.add_argument("--api-key")
    p.add_argument("--ctx", type=int, default=16384); p.add_argument("--temperature", type=float, default=0.1)
    p.add_argument("--max-tokens", type=int, default=1024); p.add_argument("--max-steps", type=int, default=30)
    p.add_argument("--max-obs", type=int, default=4000); p.add_argument("--no-think", action="store_true")
    p.add_argument("--exclude", nargs="*", default=[], help="extra directory names to skip")
    p.add_argument("--prompts-dir"); p.add_argument("--rules-dir")
    p.add_argument("--only", nargs="*"); p.add_argument("--skip", nargs="*", default=[])
    p.add_argument("--online", action="store_true", help="allow OSV.dev lookups (sends package names/versions only)")
    p.add_argument("--no-scanners", action="store_true", help="never run semgrep/bandit/gitleaks/trivy")
    p.add_argument("--semgrep-config", default="auto", help="'auto' needs network; use a local rules path to stay offline")
    p.add_argument("--fail-on", choices=list(SEV_ORDER), help="exit code 2 if a finding at/above this severity exists")
    a = p.parse_args(argv)

    url = a.url or ("http://127.0.0.1:11434" if a.backend == "ollama" else "http://127.0.0.1:8080")
    llm = LLM(url, a.temperature, a.max_tokens, a.api_key, a.model, a.backend, a.ctx)
    if a.no_think:
        llm.think = False
    v, counts = review(a.project, a.out, llm, only=a.only, skip=a.skip, excludes=a.exclude, prompts_dir=a.prompts_dir,
                       rules_dir=a.rules_dir, online=a.online, semgrep_config=a.semgrep_config,
                       scanners=not a.no_scanners, ctx=a.ctx, max_steps=a.max_steps, max_obs=a.max_obs, on_event=reporter)
    print(f"\nVerdict: {v}  {counts}\nReport: {a.out}/REPORT.md  SARIF: {a.out}/findings.sarif")
    if a.fail_on and any(counts[s] for s in SEV_ORDER if SEV_ORDER[s] <= SEV_ORDER[a.fail_on]):
        sys.exit(2)


if __name__ == "__main__":
    main()
