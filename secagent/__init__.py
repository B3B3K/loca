"""secagent - local, agentic pre-production security review (SAST + secrets + deps + manual review)."""
from pathlib import Path

from apkagent import Agent, LLM, PromptStore, ToolRegistry, Workflow

from .findings import Candidates, FindingStore
from .project import Project
from .report import verdict, write_all
from .tools import register_security_tools

PROMPTS = Path(__file__).parent / "prompts"

__all__ = ["build_agent", "review", "Project", "FindingStore", "Candidates", "LLM", "Workflow", "verdict"]


def build_agent(project_dir, out_dir, llm, *, excludes=(), prompts_dir=None, rules_dir=None, online=False,
                semgrep_config="auto", scanners=True, ctx=8192, max_steps=30, max_obs=4000, on_event=None):
    proj = Project(project_dir, excludes)
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    findings, cands = FindingStore(out / "findings.json"), Candidates(out / "candidates.json")
    reg = ToolRegistry()
    register_security_tools(reg, proj, findings, cands, rules_dir=rules_dir, online=online,
                            semgrep_config=semgrep_config, allow_scanners=scanners)
    prompts = PromptStore(prompts_dir, PROMPTS)  # user dir > secagent prompts > apkagent builtins
    langs = proj.languages()
    st = proj.stack()
    notes = [prompts.text(f"stacks/{k}.md") for k, on in (("nextjs", st["next"]), ("react", st["react"]),
                                                           ("typescript", st["typescript"])) if on]
    variables = {"PROJECT": str(proj.root), "NFILES": str(proj.count_files()),
                 "LANGS": ", ".join(f"{k} ({v})" for k, v in list(langs.items())[:8]) or "unknown",
                 "LAYOUT": proj.layout(), "STACK": proj.stack_label(),
                 "STACK_NOTES": "\n\n".join(notes) or "(no stack-specific notes)", "ONLINE": "allowed" if online else "NOT allowed (offline)"}
    agent = Agent(llm, reg, prompts, variables, ctx=ctx, max_steps=max_steps, max_obs=max_obs,
                  notes_path=out / ".notes.json", on_event=on_event)
    agent.repeatable |= {"report_finding", "dismiss", "next_candidates", "findings"}
    return agent, prompts, variables, proj, findings, cands


def review(project_dir, out_dir, llm, only=None, skip=(), **kw):
    """Run the whole workflow, write REPORT.md / findings.json / findings.sarif. Returns (verdict, counts)."""
    agent, prompts, variables, proj, findings, cands = build_agent(project_dir, out_dir, llm, **kw)
    st = proj.stack()
    cond = {"nextjs": lambda: bool(st["next"]), "react": lambda: bool(st["react"]),
            "typescript": lambda: st["typescript"]}
    reports = Workflow(agent, prompts, variables, conditions=cond).run(only=only, skip=skip)
    write_all(Path(out_dir).resolve(), proj, findings, cands, reports.get("summary", ""))
    return verdict(findings.items, cands)
