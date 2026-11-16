"""Core agent loop: one phase = one ReAct-style run. Notes persist across runs."""
import json
import re
from dataclasses import dataclass, field

from .llm import LLMError
from .parsing import parse_call

STOPS = ["</tool>", "OBSERVATION", "Observation:", "**Observation"]


@dataclass
class AgentResult:
    report: str
    finished: bool
    steps: int
    notes: list = field(default_factory=list)


class Agent:
    def __init__(self, llm, registry, prompts, variables, *, ctx=8192, max_steps=30,
                 max_obs=4000, notes_path=None, on_event=None):
        self.llm, self.registry, self.prompts = llm, registry, prompts
        self.ctx, self.max_steps, self.max_obs = ctx, max_steps, max_obs
        self.on_event, self.notes_path = on_event, notes_path
        self.notes, self.msgs, self.tried, self.seen = [], [], [], {}
        self.no_system = False
        self.repeatable = {"note", "write_file"}  # tools that may legitimately repeat identical calls
        self.names = set(registry.names()) | {"note", "finish"}
        self.fewshot = prompts.json("fewshot.json")
        self.base = prompts.text("system.md", TOOLS=prompts.text("tools.md"), **variables)
        if notes_path and notes_path.is_file():
            try:
                self.notes = json.loads(notes_path.read_text(encoding="utf-8"))
            except ValueError:
                pass

    # -- events -----------------------------------------------------------
    def emit(self, kind, **kw):
        if self.on_event:
            self.on_event(kind, kw)

    # -- messages -----------------------------------------------------------
    def add_user(self, text):
        if self.msgs and self.msgs[-1]["role"] == "user":
            self.msgs[-1]["content"] += "\n\n" + text
        else:
            self.msgs.append({"role": "user", "content": text})

    def system_text(self):
        s = self.base
        if self.notes:
            s += "\n\n" + self.prompts.text("notes_block.md", NOTES="\n".join(f"- {n}" for n in self.notes))
        if self.tried:
            s += "\n\n" + self.prompts.text("tried_block.md", TRIED="\n".join(self.tried[-25:]))
        return s

    def build(self):
        sys_t = self.system_text()
        shots = [dict(m) for m in self.fewshot]
        if not self.no_system:
            return [{"role": "system", "content": sys_t}] + shots + self.msgs
        shots[0]["content"] = sys_t + "\n\n---\n\n" + shots[0]["content"]
        return shots + self.msgs

    def est(self):
        n = len(self.system_text()) + sum(len(m["content"]) for m in self.fewshot + self.msgs)
        return n / 2.5

    def prune(self):
        budget = self.ctx - self.llm.max_tokens - 300
        while self.est() > budget:
            for m in self.msgs[1:]:
                if m["role"] == "user" and m["content"].startswith("OBSERVATION") \
                        and "elided" not in m["content"][:80]:
                    m["content"] = "OBSERVATION (elided to save context; see NOTES)"
                    break
            else:
                if len(self.msgs) > 5:
                    del self.msgs[1:3]
                else:
                    break

    def trunc(self, s):
        if len(s) <= self.max_obs:
            return s
        h = self.max_obs * 2 // 3
        t = self.max_obs - h
        return s[:h] + f"\n...[truncated {len(s) - self.max_obs} chars; narrow the query]...\n" + s[-t:]

    # -- tools ----------------------------------------------------------------
    def execute(self, call):
        n, a = call["name"], call["args"]
        if n == "note":
            t = str(a.get("text", "")).strip()
            if t:
                self.notes.append(t)
                self.emit("note", text=t)
                if self.notes_path:
                    self.notes_path.write_text(json.dumps(self.notes, ensure_ascii=False, indent=1),
                                               encoding="utf-8")
            return f"noted ({len(self.notes)} notes)"
        return self.registry.call(n, a)

    def log_try(self, step, call, obs):
        a = json.dumps(call["args"], ensure_ascii=False)
        first = (obs.splitlines() or [""])[0]
        self.tried.append(f"s{step} {call['name']} {a[:80]} -> {first[:60]}")

    def fallback_report(self):
        return self.prompts.text("fallback_report.md", N=len(self.tried),
                                 NOTES="\n".join(f"- {n}" for n in self.notes) or "(no notes were recorded)")

    # -- LLM call with recovery ---------------------------------------------------
    def generate(self):
        for _ in range(4):
            self.prune()
            try:
                return self.llm.chat(self.build(), STOPS, lambda t: self.emit("token", text=t))
            except LLMError as e:
                b = e.body.lower()
                if e.code == 400 and "think" in b and self.llm.think is not None:
                    self.llm.think = None
                elif e.code == 400 and not self.no_system and ("system" in b or "role" in b or "template" in b):
                    self.emit("warn", text="model lacks system role; embedding into user message")
                    self.no_system = True
                elif any(k in b for k in ("context", "exceed", "too long", "n_ctx")):
                    self.ctx = int(self.ctx * 0.7)
                    self.emit("warn", text=f"context overflow; budget reduced to {self.ctx}")
                else:
                    raise
        raise RuntimeError("LLM request failed repeatedly")

    # -- main loop --------------------------------------------------------------------
    def run(self, goal, max_steps=None, reset=True):
        max_steps = max_steps or self.max_steps
        if reset:
            self.msgs, self.tried, self.seen = [], [], {}
        self.add_user(goal)
        repeats = nocall = 0
        try:
            for step in range(1, max_steps + 1):
                self.emit("step", n=step, total=max_steps)
                text = re.sub(r"(</tool>\s*)+$", "", self.generate().rstrip())
                call = parse_call(text, self.names)
                stored = text + ("</tool>" if call and "<tool>" in text else "")
                if call and "<tool>" not in text:
                    stored = "<tool>" + json.dumps(call, ensure_ascii=False) + "</tool>"
                self.msgs.append({"role": "assistant", "content": stored})
                if not call:
                    nocall += 1
                    if nocall >= 3:
                        return AgentResult(text, False, step, list(self.notes))
                    self.add_user(self.prompts.text("format_error.md"))
                    continue
                nocall = 0
                if call["name"] == "finish":
                    return AgentResult(str(call["args"].get("report", text)), True, step, list(self.notes))
                self.emit("call", name=call["name"], args=call["args"])
                sig = json.dumps(call, sort_keys=True)
                if call["name"] not in self.repeatable and sig in self.seen:
                    obs = self.prompts.text("blocked.md", STEP=self.seen[sig])
                    repeats += 1
                    if repeats >= 5:
                        self.emit("warn", text="agent is looping; building report from notes")
                        return AgentResult(self.fallback_report(), False, step, list(self.notes))
                else:
                    obs = self.trunc(self.execute(call))
                    self.seen[sig] = step
                    self.log_try(step, call, obs)
                    repeats = 0
                self.emit("observation", text=obs)
                warn = ("\n\n" + self.prompts.text("steps_left.md")) if step == max_steps - 4 else ""
                self.add_user(f"OBSERVATION:\n{obs}{warn}")
            self.add_user(self.prompts.text("step_limit.md"))
            text = self.generate()
            call = parse_call(text, self.names)
            if call and call["name"] == "finish":
                return AgentResult(str(call["args"].get("report", text)), True, max_steps, list(self.notes))
            return AgentResult(self.fallback_report(), False, max_steps, list(self.notes))
        except KeyboardInterrupt:
            self.emit("warn", text="interrupted")
            return AgentResult("", False, 0, list(self.notes))
