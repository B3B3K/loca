"""Multi-phase agentic workflow. Phases come from prompts/workflow.json (overridable).
Each phase runs with a fresh message history but shares NOTES, which keeps context small."""
from dataclasses import dataclass


@dataclass
class Phase:
    id: str
    prompt: str
    max_steps: int = 15
    when: str = ""  # name of a condition; phase is skipped when it evaluates false


class Workflow:
    def __init__(self, agent, prompts, variables, config="workflow.json", conditions=None):
        self.agent, self.prompts, self.vars = agent, prompts, variables
        self.conditions = conditions or {}
        self.phases = [Phase(**p) for p in prompts.json(config)["phases"]]

    def run(self, only=None, skip=()):
        out_dir = self.agent.notes_path.parent if self.agent.notes_path else None
        reports = {}
        for ph in self.phases:
            if (only and ph.id not in only) or ph.id in skip:
                continue
            if ph.when and not self.conditions.get(ph.when, lambda: True)():
                self.agent.emit("warn", text=f"phase {ph.id} skipped (condition '{ph.when}' not met)")
                continue
            self.agent.emit("phase", id=ph.id)
            goal = self.prompts.text(ph.prompt, **self.vars)
            res = self.agent.run(goal, max_steps=ph.max_steps, reset=True)
            reports[ph.id] = res.report
            status = "done" if res.finished else "INCOMPLETE"
            self.agent.notes.append(f"[phase {ph.id} {status}] " + " ".join(res.report.split())[:300])
            if out_dir:
                (out_dir / "reports").mkdir(exist_ok=True)
                (out_dir / "reports" / f"{ph.id}.md").write_text(res.report, encoding="utf-8")
        if out_dir:
            body = "\n\n".join(f"## {k}\n\n{v}" for k, v in reports.items())
            (out_dir / "REPORT.md").write_text("# Agent report\n\n" + body, encoding="utf-8")
        return reports
