#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agent.py - Android uygulama gelistirme ajani (Ollama / llama.cpp, yerel LLM).

Mevcut bir Gradle/Android projesi uzerinde calisir: kesfet -> planla -> kucuk duzenlemeler -> gradle build ile dogrula.
Kod JSON icinde degil <old>/<new>/<content> bloklarinda gonderilir (kucuk modeller icin kacis hatalarini onler).
"""
import argparse, atexit, difflib, fnmatch, json, os, re, shlex, shutil, subprocess, sys, tempfile, threading, time
import urllib.error, urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict

if os.name == "nt": os.system("")
TTY = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
STOPS = ["OBSERVATION", "Observation:", "**Observation"]
def col(s, code): return f"\033[{code}m{s}\033[0m" if TTY else s


# --------------------------------------------------------------------------- #
# LLM istemcisi (thinking destekli)
# --------------------------------------------------------------------------- #
class LLMError(Exception):
    def __init__(self, code, body):
        super().__init__(f"HTTP {code}: {body[:300]}")
        self.code, self.body = code, body


class LLM:
    def __init__(self, url, temperature, max_tokens, api_key=None, model=None, backend="ollama", ctx=32768):
        self.url, self.temperature, self.max_tokens = url.rstrip("/"), temperature, max_tokens
        self.api_key, self.model, self.backend, self.ctx = api_key, model, backend, ctx
        self.think = None        # True/False: thinking ac/kapat, None: parametre gonderme
        self.last_done = ""      # "length" => token butcesi bitti
        self.last_think = 0      # son cevapta thinking karakter sayisi

    @property
    def endpoint(self):
        return self.url + ("/api/chat" if self.backend == "ollama" else "/v1/chat/completions")

    def _headers(self):
        h = {"Content-Type": "application/json"}
        if self.api_key: h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def n_ctx(self):
        if self.backend == "ollama": return self.ctx
        try:
            req = urllib.request.Request(self.url + "/props", headers=self._headers())
            with urllib.request.urlopen(req, timeout=5) as r:
                return int(json.load(r)["default_generation_settings"]["n_ctx"])
        except Exception:
            return None

    def chat(self, messages, stop, on_token):
        """on_token(text, is_thinking). Donus: yalnizca nihai cevap metni (thinking dahil degil)."""
        if self.backend == "ollama":
            body = {"model": self.model, "messages": messages, "stream": True, "keep_alive": "30m",
                    "options": {"temperature": self.temperature, "num_predict": self.max_tokens,
                                "num_ctx": self.ctx, "stop": stop}}   # stop 'options' icinde olmali
            if self.think is not None: body["think"] = self.think
        else:
            body = {"messages": messages, "stream": True, "temperature": self.temperature,
                    "max_tokens": self.max_tokens, "stop": stop, "cache_prompt": True}
            if self.model: body["model"] = self.model
            if self.think is not None: body["chat_template_kwargs"] = {"enable_thinking": self.think}
        req = urllib.request.Request(self.endpoint, json.dumps(body).encode(), self._headers())
        try:
            resp = urllib.request.urlopen(req, timeout=900)
        except urllib.error.HTTPError as e:
            raise LLMError(e.code, e.read().decode("utf-8", "replace"))
        parts, self.last_done, self.last_think = [], "", 0
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line: continue
            if self.backend == "ollama":
                try: d = json.loads(line)
                except ValueError: continue
                m = d.get("message") or {}
                th, tok = m.get("thinking") or "", m.get("content") or ""
                if d.get("done"): self.last_done = d.get("done_reason") or "stop"
            else:
                if not line.startswith("data:"): continue
                data = line[5:].strip()
                if data == "[DONE]": break
                try: ch = json.loads(data).get("choices") or []
                except ValueError: continue
                if not ch: continue
                delta = ch[0].get("delta") or {}
                th = delta.get("reasoning_content") or ""
                tok = delta.get("content") or ""
                if ch[0].get("finish_reason"): self.last_done = ch[0]["finish_reason"]
            if th:
                self.last_think += len(th)
                on_token(th, True)
            if tok:
                parts.append(tok)
                on_token(tok, False)
            if self.backend == "ollama" and self.last_done: break
        return "".join(parts)


# --------------------------------------------------------------------------- #
# Arac cagrisi ayristirma:  <tool>{json}</tool>  + opsiyonel <old>/<new>/<content> bloklari
# --------------------------------------------------------------------------- #
BLOCK_RE = re.compile(r"<(old|new|content)>[ \t]*\r?\n?(.*?)(?:\r?\n?[ \t]*</\1>|\Z)", re.S)


def parse_call(text, names=None):
    dec = json.JSONDecoder(strict=False)
    m = re.search(r"<tool>", text)
    cands = ([(m.end(), True)] if m else []) + [(x.start(), False) for x in re.finditer(r"\{", text)]
    for s, trusted in cands:
        i = text.find("{", s)
        if i < 0: continue
        try: obj, end = dec.raw_decode(text[i:])
        except ValueError: continue
        if not (isinstance(obj, dict) and "name" in obj): continue
        if not trusted and names and obj["name"] not in names: continue
        args = obj.get("args") or obj.get("arguments") or obj.get("parameters") or {}
        if isinstance(args, str):
            try: args = json.loads(args)
            except ValueError: args = {}
        args = dict(args) if isinstance(args, dict) else {}
        pos = i + end
        close = re.match(r"\s*</tool>", text[pos:])
        if close: pos += close.end()
        for bm in BLOCK_RE.finditer(text, pos):
            args.setdefault(bm.group(1), bm.group(2))
            pos = max(pos, bm.end())
        return {"name": str(obj["name"]), "args": args, "end": pos, "tagged": m is not None}
    if names:  # `tool {json}` biciminde backtick yazan zayif modeller
        for mm in re.finditer(r"`\s*([a-z_0-9]+)\s*(\{.*?\})?\s*`", text, re.S):
            if mm.group(1) in names:
                try: args = json.loads(mm.group(2), strict=False) if mm.group(2) else {}
                except ValueError: continue
                if isinstance(args, dict):
                    return {"name": mm.group(1), "args": args, "end": mm.end(), "tagged": False}
    return None


# --------------------------------------------------------------------------- #
# Sabitler
# --------------------------------------------------------------------------- #
SKIP_DIRS = {".gradle", ".git", ".idea", ".agent", "node_modules", ".cxx", "captures", ".kotlin"}
TEXT_EXTS = (".kt", ".kts", ".java", ".xml", ".gradle", ".toml", ".properties", ".json", ".md", ".txt", ".pro",
             ".yml", ".yaml", ".cfg", ".proto", ".aidl", ".sh", ".bat", ".html", ".css", ".js")
BIN_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".apk", ".aab", ".jar", ".so", ".ttf", ".otf", ".ico",
            ".keystore", ".jks", ".zip", ".mp3", ".mp4", ".ogg", ".class", ".dex")
NO_WRITE_NAMES = {"local.properties", "gradle-wrapper.jar"}
GRADLE_TASKS = {"assembleDebug", "compileDebugKotlin", "compileDebugJavaWithJavac", "compileDebugSources",
                "lintDebug", "testDebugUnitTest"}
MUTATORS = {"edit_file", "write_file", "delete_file", "undo"}
PLACEHOLDER_RE = re.compile(r"(?im)^\s*(?://|/\*|#|<!--)[^\n]*\b(rest of (the )?(code|file)|existing code|"
                            r"remaining code|same as before|unchanged)\b|^\s*\.\.\.\s*$")
KT_DECL = re.compile(r"^(\s*)(?:@\w+(?:\([^)]*\))?\s+)*(?:(?:public|private|protected|internal|open|abstract|sealed|"
                     r"data|enum|annotation|inner|override|suspend|inline|const|lateinit|operator|companion|value|"
                     r"final|static)\s+)*(class|interface|object|fun|typealias)\b\s*(?:<[^>]*>\s*)?([\w.]+)")
JAVA_M = re.compile(r"^(\s{0,8})(?:public|private|protected)\s+(?:static\s+)?(?:final\s+)?(?:synchronized\s+)?"
                    r"[\w<>\[\],.? ]+?\s+(\w+)\s*\(")
NOISE_KW = ("if", "for", "while", "switch", "catch", "return", "new", "else")

SYSTEM = r"""You are an expert Android developer agent (Kotlin and Java, Jetpack Compose and XML Views, Gradle).
You work INSIDE an existing, working project and implement the user's change with minimal, correct, buildable edits.

PROJECT SNAPSHOT (root: @@PROJ@@)
@@INFO@@

CALL FORMAT: one or two plain sentences, then exactly one call, then STOP (the system answers with an OBSERVATION).
<tool>{"name": "find", "args": {"pattern": "class MainActivity", "glob": "*.kt"}}</tool>
Code NEVER goes inside the JSON. Tools that change files take code in blocks placed right after the call:
<tool>{"name": "edit_file", "args": {"path": "app/src/main/java/com/x/Foo.kt"}}</tool>
<old>
exact existing lines, copied from `read` output WITHOUT the "N: " prefixes, same indentation
</old>
<new>
replacement lines
</new>
New file: <tool>{"name": "write_file", "args": {"path": "..."}}</tool> followed by <content>...</content>.

TOOLS ([x] = optional)
- project_info {}   modules, SDK levels, plugins, dependencies, UI toolkit, architecture hints.
- manifest {[module]}   permissions, application class, activities (LAUNCHER marked), services, receivers.
- ls {[path]}   directory listing.
- find {pattern, [path], [glob], [ignore_case=true], [files_only=false], [limit=40]}   regex search in project text files.
- outline {file}   classes/functions with line numbers of one .kt/.java file (cheaper than reading it whole).
- read {file, [start=1], [end]}   numbered lines, max 250 per call.
- edit_file {path} + <old>/<new>   replace one exact block (must match exactly once; [replace_all=true] for all).
- write_file {path, [overwrite=false]} + <content>   create a new file (for existing files use edit_file).
- delete_file {path}   remove a file (backed up).   undo {path}   restore a file to its session-start state.
- diff {[path]}   unified diff of everything changed this session.
- build {[task=assembleDebug], [module]}   run Gradle, returns compact errors with the offending source lines.
  Tasks: assembleDebug, compileDebugKotlin (fast check), compileDebugJavaWithJavac, lintDebug, testDebugUnitTest.
- adb {op, ...}   op=devices | install {apk} | start {component} | stop {package} | logcat {[filter],[lines]} | clear_logcat.
- note {text}   durable plan item or fact; re-shown every turn, survives context pruning and restarts.
- finish {report}   final report.

WORKFLOW
1 ORIENT: use the snapshot, then find/outline only the files that matter. Read before editing. Never guess paths.
2 PLAN: note 2-6 concrete steps (which files change and why).
3 IMPLEMENT: one small edit at a time. After each logical change run build and fix every reported error first.
4 REVIEW: run diff, compare with the request and the project's style, then finish.

RULES
1. Minimal diffs: change only what the task needs. Keep the project's architecture, naming, formatting and UI toolkit
   (Compose vs XML Views). Reuse libraries already in the project.
2. Do NOT change Gradle/AGP/Kotlin/compileSdk versions or add dependencies unless the task requires it (then say so in
   the report). Never touch signing configs, local.properties or keystores.
3. Never invent APIs. If unsure a class or method exists, find a usage in the project or mark it UNVERIFIED. Respect
   minSdk (guard newer APIs with Build.VERSION.SDK_INT). Runtime permissions need a manifest entry AND a request flow.
4. User-visible text goes to res/values/strings.xml. Register new Activities/Services/Receivers in the manifest. Add the
   imports your code needs.
5. No placeholders such as "..." or "// rest of code" anywhere. Blocks contain real, complete code.
6. Prefer edit_file with a small unique <old> (3-8 lines incl. context) over rewriting. Keep each write_file under
   ~150 lines; split bigger things into several files.
7. If the same error survives 2 attempts: read the surrounding code, note what you learned, change approach.
8. A green build means it compiles, not that it works. Claim runtime behaviour only if adb logcat showed it. The final
   report lists: changed files, build status, what is UNVERIFIED, suggested manual tests.
9. Reply language: the user's language. Code, identifiers and code comments in English.
10. FORMAT: no markdown in your turns; one or two plain sentences, exactly one call, then STOP. Never write an
    OBSERVATION yourself. Never repeat a call listed in ALREADY TRIED.
"""

FEWSHOT = [
    {"role": "user", "content": "EXAMPLE of the required turn format. Task: rename a button label."},
    {"role": "assistant", "content": "I need the string resource that holds the label.\n"
                                     "<tool>{\"name\": \"find\", \"args\": {\"pattern\": \"btn_save\", \"glob\": \"*.xml\"}}</tool>"},
    {"role": "user", "content": "OBSERVATION:\n1 matches in 1 files\napp/src/main/res/values/strings.xml  (1 hits)\n"
                                "  7: <string name=\"btn_save\">Save</string>"},
    {"role": "assistant", "content": "I will change the label text in strings.xml.\n"
                                     "<tool>{\"name\": \"edit_file\", \"args\": {\"path\": \"app/src/main/res/values/strings.xml\"}}</tool>\n"
                                     "<old>\n<string name=\"btn_save\">Save</string>\n</old>\n"
                                     "<new>\n<string name=\"btn_save\">Save changes</string>\n</new>"},
    {"role": "user", "content": "OBSERVATION:\nedited app/src/main/res/values/strings.xml (1 replacement)\n\n(End of example. The real "
                                "task follows. Always answer with one short plain sentence and exactly one call, no markdown.)"},
    {"role": "assistant", "content": "Understood. One short sentence and one call per turn."},
]


# --------------------------------------------------------------------------- #
# Ajan
# --------------------------------------------------------------------------- #
class Agent:
    TOOLS = {"project_info", "manifest", "ls", "find", "outline", "read", "edit_file", "write_file", "delete_file",
             "undo", "diff", "build", "adb", "note", "finish"}

    def __init__(self, llm, proj, state, ctx, max_steps, max_obs, build_timeout=900):
        self.llm, self.root = llm, os.path.realpath(proj)
        self.state = os.path.realpath(state)
        self.ctx, self.max_steps, self.max_obs, self.build_timeout = ctx, max_steps, max_obs, build_timeout
        self.msgs, self.no_system, self.notes = [], False, []
        self.tried, self.seen, self.originals = [], {}, {}
        self.dirty, self.finish_warned, self.last_build = False, False, "not run yet"
        os.makedirs(self.state, exist_ok=True)
        self.backup_dir = os.path.join(self.state, "backups", time.strftime("%Y%m%d-%H%M%S"))
        self.notes_file = os.path.join(self.state, "notes.json")
        self._git_exclude()
        try:
            with open(self.notes_file, encoding="utf-8") as f: self.notes = json.load(f)
            print(col(f"[*] {len(self.notes)} onceki not yuklendi (devam modu)", "33"))
        except (OSError, ValueError):
            pass
        self.system_base = (SYSTEM.replace("@@PROJ@@", os.path.basename(self.root))
                            .replace("@@INFO@@", self.t_project_info({})[:2200]))

    # ---- yardimcilar ---------------------------------------------------------- #
    def _git_exclude(self):
        ex = os.path.join(self.root, ".git", "info", "exclude")
        try:
            if os.path.isdir(os.path.dirname(ex)):
                cur = open(ex, encoding="utf-8").read() if os.path.exists(ex) else ""
                if ".agent/" not in cur:
                    with open(ex, "a", encoding="utf-8") as f: f.write("\n.agent/\n")
        except OSError:
            pass

    def _res(self, p, write=False):
        p = str(p or "").replace("\\", "/").lstrip("/")
        full = os.path.realpath(os.path.join(self.root, p))
        if full != self.root and not full.startswith(self.root + os.sep):
            raise ValueError("path escapes project")
        if write:
            rel = os.path.relpath(full, self.root).replace(os.sep, "/")
            parts = rel.split("/")
            if any(x in SKIP_DIRS for x in parts) or self._in_build_dir(full):
                raise ValueError(f"writing to '{rel}' is not allowed (generated/internal dir)")
            if parts[-1] in NO_WRITE_NAMES or rel.lower().endswith(BIN_EXTS):
                raise ValueError(f"'{rel}' is protected or binary; not writable")
        return full

    def _in_build_dir(self, full):
        d = os.path.dirname(full)
        while d.startswith(self.root) and d != self.root:
            if os.path.basename(d) == "build" and any(
                    os.path.exists(os.path.join(os.path.dirname(d), n)) for n in ("build.gradle", "build.gradle.kts")):
                return True
            d = os.path.dirname(d)
        return False

    def _rel(self, full): return os.path.relpath(full, self.root).replace(os.sep, "/")

    def _walk(self, base, glob=None):
        if os.path.isfile(base):
            yield base
            return
        for dp, dirs, fns in os.walk(base):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not (
                d == "build" and any(os.path.exists(os.path.join(dp, n)) for n in ("build.gradle", "build.gradle.kts"))))
            for fn in sorted(fns):
                if glob:
                    if not fnmatch.fnmatch(fn, glob): continue
                elif not fn.lower().endswith(TEXT_EXTS):
                    continue
                fp = os.path.join(dp, fn)
                try:
                    if os.path.getsize(fp) <= 1_500_000: yield fp
                except OSError:
                    continue

    def _file(self, rel, write=False):
        """Yol coz; yoksa tekil dosya adiyla eslestir (kucuk modeller yolu sik karistirir)."""
        p = self._res(rel, write)
        if os.path.isfile(p): return p
        base = os.path.basename(str(rel).replace("\\", "/"))
        hits = [f for f in self._walk(self.root, base) if os.path.basename(f) == base] if base else []
        if len(hits) == 1: return hits[0]
        if len(hits) > 1:
            raise ValueError("ambiguous file name, use one of: " + ", ".join(self._rel(h) for h in hits[:6]))
        raise ValueError(f"file not found: {rel} (use find with files_only=true and glob)")

    def _src_line(self, rel, ln):
        try:
            with open(os.path.join(self.root, rel), encoding="utf-8", errors="replace") as f:
                for i, l in enumerate(f, 1):
                    if i == ln: return l.strip()[:140]
        except OSError:
            pass
        return ""

    def _save_notes(self):
        with open(self.notes_file, "w", encoding="utf-8") as f: json.dump(self.notes, f, ensure_ascii=False, indent=1)

    def _backup(self, full):
        rel = self._rel(full)
        if rel in self.originals: return
        if os.path.exists(full):
            dst = os.path.join(self.backup_dir, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(full, dst)
            self.originals[rel] = dst
        else:
            self.originals[rel] = None

    @staticmethod
    def _balance(t):
        t = re.sub(r'"""[\s\S]*?"""', '""', t)
        t = re.sub(r'/\*[\s\S]*?\*/', '', t)
        t = re.sub(r'//[^\n]*', '', t)
        t = re.sub(r'"(?:\\.|[^"\\\n])*"', '""', t)
        t = re.sub(r"'(?:\\.|[^'\\\n])'", "''", t)
        return (t.count("{") - t.count("}"), t.count("(") - t.count(")"), t.count("[") - t.count("]"))

    def _check(self, rel, before, after):
        r = rel.lower()
        if r.endswith((".kt", ".java", ".kts", ".gradle")):
            b, a = self._balance(before or ""), self._balance(after)
            if a != b:
                return f"\nWARNING: bracket balance changed (braces/parens/brackets {b} -> {a}); re-read the edited area."
        elif r.endswith(".xml"):
            try: ET.fromstring(after.encode("utf-8"))
            except ET.ParseError as e: return f"\nWARNING: XML is not well-formed: {e}"
        elif r.endswith(".json"):
            try: json.loads(after)
            except ValueError as e: return f"\nWARNING: invalid JSON: {e}"
        return ""

    # ---- mesaj yonetimi -------------------------------------------------------- #
    def add_user(self, text):
        if self.msgs and self.msgs[-1]["role"] == "user": self.msgs[-1]["content"] += "\n\n" + text
        else: self.msgs.append({"role": "user", "content": text})

    def system_text(self):
        s = self.system_base
        if self.notes: s += "\nNOTES SO FAR:\n" + "\n".join(f"- {n}" for n in self.notes[-40:])
        if self.originals: s += "\nFILES CHANGED THIS SESSION: " + ", ".join(sorted(self.originals))
        s += f"\nLAST BUILD: {self.last_build}" + (" (files edited since then)" if self.dirty else "")
        if self.tried:
            s += "\nALREADY TRIED (never repeat):\n" + "\n".join(self.tried[-20:])
        return s

    def log_try(self, step, call, obs):
        a = json.dumps({k: (v if len(str(v)) < 60 else "...") for k, v in call["args"].items()}, ensure_ascii=False)
        self.tried.append(f"s{step} {call['name']} {a[:80]} -> {(obs.splitlines() or [''])[0][:60]}")

    def fallback_report(self):
        return ("(otomatik: ajan finish cagirmadi)\nDegisen dosyalar: " + (", ".join(sorted(self.originals)) or "yok")
                + f"\nSon build: {self.last_build}\nNotlar:\n" + ("\n".join(f"- {n}" for n in self.notes) or "(yok)"))

    def build_msgs(self):
        sys_t, shots = self.system_text(), [dict(m) for m in FEWSHOT]
        if not self.no_system: return [{"role": "system", "content": sys_t}] + shots + self.msgs
        shots[0]["content"] = sys_t + "\n\n---\n\n" + shots[0]["content"]
        return shots + self.msgs

    def est(self):
        return (len(self.system_text()) + sum(len(m["content"]) for m in FEWSHOT + self.msgs)) // 2.5

    def prune(self):
        budget = max(self.ctx // 2, self.ctx - self.llm.max_tokens - 300)
        while self.est() > budget:
            for m in self.msgs[:-6]:   # 1) eski kod bloklarini kisalt
                if m["role"] == "assistant" and "[code elided]" not in m["content"] and BLOCK_RE.search(m["content"]):
                    m["content"] = BLOCK_RE.sub(lambda x: f"<{x.group(1)}>[code elided]</{x.group(1)}>", m["content"])
                    break
            else:
                for m in self.msgs[1:]:  # 2) eski gozlemleri kisalt
                    if m["role"] == "user" and m["content"].startswith("OBSERVATION") and "elided" not in m["content"][:80]:
                        m["content"] = "OBSERVATION (elided to save context; see NOTES)"
                        break
                else:
                    if len(self.msgs) > 5: del self.msgs[1:3]
                    else: break

    def trunc(self, s):
        if len(s) <= self.max_obs: return s
        h = self.max_obs * 2 // 3
        return s[:h] + f"\n...[truncated {len(s) - self.max_obs} chars; narrow the query]...\n" + s[-(self.max_obs - h):]

    # ---- LLM cagrisi ------------------------------------------------------------ #
    def generate(self):
        mode = {"think": None}

        def on_tok(t, think):
            if mode["think"] != think:
                mode["think"] = think
                sys.stdout.write("\n" + col("[dusunuyor] " if think else "[cevap] ", "90" if think else "36"))
            sys.stdout.write(col(t, "90") if think else t)
            sys.stdout.flush()
        for _ in range(4):
            self.prune()
            try:
                return self.llm.chat(self.build_msgs(), STOPS, on_tok)
            except LLMError as e:
                b = e.body.lower()
                if e.code == 400 and "think" in b and self.llm.think is not None:
                    print(col("\n[!] Model 'think' parametresini desteklemiyor, kapatildi.", "33")); self.llm.think = None
                elif e.code == 400 and not self.no_system and ("system" in b or "role" in b or "template" in b):
                    print(col("\n[!] system rolu desteklenmiyor, user mesajina gomuluyor.", "33")); self.no_system = True
                elif any(k in b for k in ("context", "exceed", "too long", "n_ctx")):
                    self.ctx = int(self.ctx * 0.7)
                    print(col(f"\n[!] Context tasti, butce {self.ctx} token'a dusuruldu.", "33"))
                else:
                    raise
        raise RuntimeError("LLM istegi tekrar tekrar basarisiz")

    # ---- ana dongu -------------------------------------------------------------- #
    def tool(self, call):
        n, a = call["name"], call["args"]
        fn = getattr(self, "t_" + n, None) if re.fullmatch(r"[a-z_]+", n) and n in self.TOOLS else None
        if not fn: return f"error: unknown tool '{n}'. Tools: {', '.join(sorted(self.TOOLS))}."
        try: return fn(a)
        except Exception as e:  # arac hatasi ajana geri bildirilir, dongu dusmez
            return f"error: {type(e).__name__}: {e}"

    @staticmethod
    def _brief(args):
        return json.dumps({k: (v if len(str(v)) < 90 else f"<{len(str(v))} chars>") for k, v in args.items()},
                          ensure_ascii=False)

    def run_goal(self, goal):
        self.add_user(goal)
        repeats = nocall = 0
        try:
            for step in range(1, self.max_steps + 1):
                print(col(f"\n== adim {step}/{self.max_steps} ==", "34"))
                raw = self.generate()
                text = re.sub(r"<think>.*?(?:</think>|\Z)", "", raw, flags=re.S).strip()
                call = parse_call(text, self.TOOLS)
                if call:
                    stored = text[:call["end"]] if call["tagged"] else (
                        "<tool>" + json.dumps({"name": call["name"], "args": call["args"]}, ensure_ascii=False) + "</tool>")
                    if call["tagged"] and "</tool>" not in stored:
                        stored = re.sub(r"(\})\s*$", r"\1</tool>", stored, count=1) if not BLOCK_RE.search(stored) else stored
                else:
                    stored = text or "(empty)"
                self.msgs.append({"role": "assistant", "content": stored})
                print()
                if not call:
                    nocall += 1
                    if nocall >= 3: return self.fallback_report()
                    if self.llm.last_done == "length":
                        print(col("[!] max-tokens doldu (thinking cok uzun olabilir): --max-tokens artir.", "31"))
                        self.add_user("Your answer was cut off because thinking used the whole token budget. Think much "
                                      "less and reply NOW with one short sentence and exactly one <tool> call.")
                    else:
                        self.add_user("FORMAT ERROR: no <tool> call found. Reply with one short plain sentence and exactly "
                                      "one call, e.g. <tool>{\"name\": \"read\", \"args\": {\"file\": \"...\"}}</tool>. "
                                      "Use finish when done. Never write an OBSERVATION yourself.")
                    continue
                nocall = 0
                if call["name"] == "finish":
                    if self.dirty and self.originals and not self.finish_warned \
                            and self._gradlew():
                        self.finish_warned = True
                        self.add_user("OBSERVATION:\nBLOCKED: you edited files since the last successful build. Run build "
                                      "(compileDebugKotlin or assembleDebug) first, fix errors, then call finish again.")
                        continue
                    return str(call["args"].get("report", text))
                print(col(f"  > {call['name']} {self._brief(call['args'])}", "32"))
                sig = json.dumps({k: v for k, v in call.items() if k in ("name", "args")}, sort_keys=True)
                if call["name"] not in ({"note"} | MUTATORS) and sig in self.seen:
                    obs = (f"BLOCKED (not executed): identical call already ran at step {self.seen[sig]} and nothing "
                           "changed since. Read ALREADY TRIED and NOTES, then use a different tool, range or pattern.")
                    repeats += 1
                    if repeats >= 5:
                        print(col("[!] ajan dongude, rapor notlardan uretiliyor", "31"))
                        return self.fallback_report()
                else:
                    obs = self.trunc(self.tool(call))
                    self.seen[sig] = step
                    self.log_try(step, call, obs)
                    repeats = 0
                    if call["name"] in MUTATORS and not obs.startswith("error"): self.seen.clear()
                shown = obs.splitlines()
                print(col("\n".join("    " + l for l in shown[:15]) +
                          (f"\n    ... (+{len(shown) - 15} satir)" if len(shown) > 15 else ""), "37"))
                warn = ("\n\n(4 steps left: note confirmed facts, run a final build, then call finish.)"
                        if step == self.max_steps - 4 else "")
                self.add_user(f"OBSERVATION:\n{obs}{warn}")
            self.add_user("Step limit reached. Call finish now: changed files, build status, UNVERIFIED items, next steps.")
            call = parse_call(self.generate(), self.TOOLS)
            return str(call["args"].get("report", "")) if call and call["name"] == "finish" else self.fallback_report()
        except KeyboardInterrupt:
            print(col("\n[kesildi]", "31"))
            self.add_user("[user interrupted the previous run]")
            return None

    # ---- kesif araclari ----------------------------------------------------------- #
    def t_note(self, a):
        t = str(a.get("text", "")).strip()
        if t:
            self.notes.append(t); self._save_notes()
            print(col(f"  [note] {t}", "36"))
        return f"noted ({len(self.notes)} notes)"

    def t_ls(self, a):
        p = self._res(a.get("path", ""))
        if not os.path.isdir(p): return "error: not a directory"
        rows = []
        for name in sorted(os.listdir(p)):
            if name in SKIP_DIRS: continue
            fp = os.path.join(p, name)
            rows.append(f"{name}/  ({sum(1 for _ in self._walk(fp, '*'))} files)" if os.path.isdir(fp)
                        else f"{name}  ({os.path.getsize(fp)} B)")
        return "\n".join(rows[:80]) + (f"\n... +{len(rows) - 80} more" if len(rows) > 80 else "") if rows else "(empty)"

    def t_read(self, a):
        p = self._file(a.get("file") or a.get("path", ""))
        start = max(1, int(a.get("start", 1)))
        end = min(int(a.get("end", start + 149)), start + 249)
        out, total = [], 0
        with open(p, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, 1):
                total = i
                if start <= i <= end: out.append(f"{i}: {line.rstrip()[:300]}")
        return f"{self._rel(p)} lines {start}-{min(end, total)} of {total}\n" + "\n".join(out)

    def t_find(self, a):
        pat = str(a.get("pattern", ""))
        if not pat: return "error: empty pattern"
        try: rx = re.compile(pat, re.I if a.get("ignore_case", True) else 0)
        except re.error as e: return f"error: bad regex: {e}"
        base, glob, fo = self._res(a.get("path", "")), a.get("glob"), bool(a.get("files_only", False))
        limit, hits, total = min(int(a.get("limit", 40)), 100), defaultdict(list), 0
        for fp in self._walk(base, glob):
            try:
                with open(fp, encoding="utf-8", errors="replace") as fh:
                    for i, l in enumerate(fh, 1):
                        if rx.search(l):
                            hits[self._rel(fp)].append((i, l.strip())); total += 1
                            if fo: break
            except OSError:
                continue
        if not hits: return "no matches"
        if fo: return f"{len(hits)} files\n" + "\n".join(list(hits)[:limit])
        out, n = [], 0
        for f, hs in hits.items():
            if n >= limit:
                out.append("... more files; narrow with path/glob or a more specific pattern"); break
            out.append(f"{f}  ({len(hs)} hits)")
            for ln, tx in hs[:3]:
                out.append(f"  {ln}: {tx[:170]}"); n += 1
        return f"{total} matches in {len(hits)} files\n" + "\n".join(out)

    def t_outline(self, a):
        p = self._file(a.get("file") or a.get("path", ""))
        if not p.endswith((".kt", ".java")): return "error: outline works for .kt/.java only; use read"
        rows = []
        with open(p, encoding="utf-8", errors="replace") as f:
            for i, l in enumerate(f, 1):
                m = KT_DECL.match(l)
                if m and len(m.group(1)) <= 8:
                    rows.append(f"{i}: {l.rstrip()[:130]}")
                elif p.endswith(".java"):
                    j = JAVA_M.match(l)
                    if j and j.group(2) not in NOISE_KW: rows.append(f"{i}: {l.rstrip()[:130]}")
                elif re.match(r"^(?:private |internal )?(?:val|var)\s+\w+", l):
                    rows.append(f"{i}: {l.rstrip()[:130]}")
        return f"{self._rel(p)}: {len(rows)} declarations\n" + "\n".join(rows[:120])

    def _gradle_files(self):
        for dp, dirs, fns in os.walk(self.root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and d != "build" and d != "src"]
            for fn in fns:
                if fn in ("build.gradle", "build.gradle.kts"): yield os.path.join(dp, fn)

    def t_project_info(self, a):
        o, root = [], self.root
        has = lambda n: os.path.exists(os.path.join(root, n))
        if not (has("settings.gradle") or has("settings.gradle.kts")):
            o.append("WARNING: no settings.gradle(.kts) in root; is this the project root?")
        o.append(f"gradlew: {'yes' if self._gradlew() else 'NO'}")
        st = next((open(os.path.join(root, n), encoding="utf-8", errors="replace").read()
                   for n in ("settings.gradle.kts", "settings.gradle") if has(n)), "")
        mods = sorted(set(re.findall(r"""["'](:[\w:\-]+)["']""", st)))
        if mods: o.append("modules: " + ", ".join(mods))
        tom = os.path.join(root, "gradle", "libs.versions.toml")
        if os.path.isfile(tom):
            vs = re.search(r"\[versions\]\s*\n(.*?)(?:\n\[|\Z)", open(tom, encoding="utf-8", errors="replace").read(), re.S)
            if vs: o.append("version catalog [versions]: " + "; ".join(
                l.strip().replace(" ", "") for l in vs.group(1).splitlines() if "=" in l)[:600])
        for gf in list(self._gradle_files())[:4]:
            t = open(gf, encoding="utf-8", errors="replace").read()
            rel = self._rel(gf)
            ids = sorted(set(re.findall(r"""\bid\s*\(?\s*["']([\w.\-]+)["']""", t) + re.findall(r"""\balias\s*\(\s*([\w.\-]+)\s*\)""", t)))
            o.append(f"[{rel}]")
            nm = re.findall(r"""(namespace|applicationId)\s*=?\s*["']([^"']+)["']""", t)
            sdk = re.findall(r"(compileSdk|minSdk|targetSdk)(?:Version)?\s*=?\s*\(?\s*(\d+)", t)
            if nm or sdk: o.append("  " + " ".join(f"{k}={v}" for k, v in nm + sdk))
            tl = t.lower()
            flags = [k for k, w in (("compose", "compose"), ("viewBinding", "viewbinding"), ("dataBinding", "databinding"),
                                    ("hilt", "hilt"), ("room", "room"), ("retrofit", "retrofit"), ("coroutines", "coroutines"),
                                    ("navigation", "navigation"), ("ksp", "ksp"), ("kapt", "kapt")) if w in tl]
            if flags: o.append("  uses: " + ", ".join(flags))
            if ids: o.append("  plugins: " + ", ".join(ids[:8]))
            deps = re.findall(r"^\s*(implementation|api|kapt|ksp|annotationProcessor)\s*\(?\s*(.+?)\)?\s*$", t, re.M)
            if deps: o.append("  deps: " + " | ".join(f"{k} {v[:70]}" for k, v in deps[:14]))
        kt = sum(1 for f in self._walk(root, "*.kt")); jv = sum(1 for f in self._walk(root, "*.java"))
        lay = sum(1 for f in self._walk(root, "*.xml") if f"{os.sep}layout" in f)
        o.append(f"sources: {kt} .kt, {jv} .java, {lay} layout xml")
        return "\n".join(o)

    def t_manifest(self, a):
        ms = [f for f in self._walk(self.root, "AndroidManifest.xml")
              if f"{os.sep}src{os.sep}main{os.sep}" in f]
        mod = str(a.get("module", "")).strip(":/")
        if mod: ms = [f for f in ms if self._rel(f).startswith(mod)] or ms
        if not ms: return "error: AndroidManifest.xml not found"
        ns, o = "{http://schemas.android.com/apk/res/android}", []
        for mp in ms[:2]:
            try: r = ET.parse(mp).getroot()
            except ET.ParseError:
                o.append(f"[{self._rel(mp)}] unparsable"); continue
            o.append(f"[{self._rel(mp)}] package={r.get('package')}")
            o.append("permissions: " + ", ".join(sorted(p.get(ns + "name", "").replace("android.permission.", "")
                                                       for p in r.findall("uses-permission"))[:40]))
            app = r.find("application")
            if app is None: continue
            o.append(f"application: name={app.get(ns + 'name')} theme={app.get(ns + 'theme')}")
            for kind in ("activity", "service", "receiver", "provider"):
                items = []
                for e in app.findall(kind):
                    launcher = any(c.get(ns + "name", "").endswith("LAUNCHER") for c in e.iter("category"))
                    items.append(e.get(ns + "name", "") + (" [LAUNCHER]" if launcher else "") +
                                 (" [exported]" if e.get(ns + "exported") == "true" else ""))
                if items: o.append(f"{ {'activity': 'activities'}.get(kind, kind + 's') } ({len(items)}): " + "; ".join(items[:20]))
        return "\n".join(o)

    # ---- degistirme araclari --------------------------------------------------------- #
    @staticmethod
    def _strip_ln(s):
        ls = s.split("\n")
        ne = [l for l in ls if l.strip()]
        if ne and all(re.match(r"^\d+: ?", l) for l in ne): return "\n".join(re.sub(r"^\d+: ?", "", l) for l in ls)
        return s

    @staticmethod
    def _indent(l): return len(l) - len(l.lstrip(" "))

    def t_edit_file(self, a):
        old, new = a.get("old"), a.get("new")
        if not old or new is None:
            return "error: put the exact existing lines in <old>...</old> and the replacement in <new>...</new> after the call"
        p = self._file(a.get("path") or a.get("file", ""), write=True)
        raw = open(p, encoding="utf-8", errors="replace", newline="").read()
        crlf = "\r\n" in raw
        text = raw.replace("\r\n", "\n")
        old, new = str(old).replace("\r\n", "\n"), str(new).replace("\r\n", "\n")
        if PLACEHOLDER_RE.search(new): return "error: <new> contains a placeholder ('...' / 'rest of code'); write real code"
        note, cnt = "", text.count(old)
        if cnt == 0 and self._strip_ln(old) != old:
            old, new = self._strip_ln(old), self._strip_ln(new); cnt = text.count(old)
        if cnt == 0:  # bosluk/girinti toleransli eslesme
            tl, ol = text.split("\n"), old.strip("\n").split("\n")
            ost = [x.strip() for x in ol]
            idx = [i for i in range(len(tl) - len(ol) + 1) if [x.strip() for x in tl[i:i + len(ol)]] == ost]
            if len(idx) == 1:
                i = idx[0]
                d = self._indent(tl[i]) - self._indent(ol[0])
                nl = new.strip("\n").split("\n")
                nl = [(" " * d + l if d > 0 else l[min(-d, self._indent(l)):]) if l.strip() else l for l in nl]
                text2 = "\n".join(tl[:i] + nl + tl[i + len(ol):])
                return self._commit(p, raw, text, text2, crlf, 1, " (whitespace-tolerant match)")
            if len(idx) > 1: return f"error: whitespace-insensitive match is ambiguous ({len(idx)} places); add more context lines"
            first = next((l.strip() for l in old.split("\n") if l.strip()), "")
            close = difflib.get_close_matches(first, [l.strip() for l in tl], n=1, cutoff=0.6)
            where = next((i + 1 for i, l in enumerate(tl) if close and l.strip() == close[0]), None)
            return ("error: <old> not found in file." + (f" Closest line {where}: {tl[where - 1].strip()[:120]}. "
                    f"read lines {max(1, where - 3)}-{where + 8} and copy exactly." if where else " Use read to copy exact text."))
        if cnt > 1 and not a.get("replace_all"):
            lines = [text[:m.start()].count("\n") + 1 for m in re.finditer(re.escape(old), text)][:6]
            return f"error: <old> matches {cnt} times (lines {lines}); add surrounding lines or pass replace_all=true"
        return self._commit(p, raw, text, text.replace(old, new), crlf, cnt, note)

    def _commit(self, p, raw, before, after, crlf, n, note=""):
        if after == before: return "error: edit changes nothing"
        self._backup(p)
        with open(p, "w", encoding="utf-8", newline="") as f: f.write(after.replace("\n", "\r\n") if crlf else after)
        self.dirty = True
        return f"edited {self._rel(p)} ({n} replacement{'s' if n != 1 else ''}){note}" + self._check(self._rel(p), before, after)

    def t_write_file(self, a):
        rel, content = str(a.get("path", "")).strip(), a.get("content")
        if not rel or content is None or len(str(content)) > 60_000:
            return "error: need path and a <content>...</content> block (<60KB) after the call"
        content = str(content)
        if PLACEHOLDER_RE.search(content): return "error: content has a placeholder ('...' / 'rest of code'); write real code"
        p = self._res(rel, write=True)
        existed = os.path.exists(p)
        if existed and not a.get("overwrite"):
            return f"error: {rel} already exists; use edit_file (or overwrite=true to replace it entirely)"
        before = open(p, encoding="utf-8", errors="replace").read() if existed else ""
        self._backup(p)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        content = content.rstrip("\n") + "\n"
        with open(p, "w", encoding="utf-8", newline="") as f: f.write(content)
        self.dirty = True
        return f"{'overwrote' if existed else 'created'} {rel} ({content.count(chr(10))} lines)" + self._check(rel, before, content)

    def t_delete_file(self, a):
        p = self._file(a.get("path") or a.get("file", ""), write=True)
        self._backup(p); os.remove(p); self.dirty = True
        return f"deleted {self._rel(p)} (restore with undo)"

    def t_undo(self, a):
        rel = self._rel(self._res(a.get("path") or a.get("file", "")))
        if rel not in self.originals: return f"error: {rel} was not changed in this session"
        src, dst = self.originals.pop(rel), os.path.join(self.root, rel)
        if src is None:
            if os.path.exists(dst): os.remove(dst)
            return f"removed new file {rel}"
        shutil.copy2(src, dst); self.dirty = True
        return f"restored {rel} to session-start state"

    def t_diff(self, a):
        want = self._rel(self._res(a["path"])) if a.get("path") else None
        out = []
        for rel, bak in sorted(self.originals.items()):
            if want and rel != want: continue
            cur = os.path.join(self.root, rel)
            old = open(bak, encoding="utf-8", errors="replace").read().splitlines() if bak else []
            new = open(cur, encoding="utf-8", errors="replace").read().splitlines() if os.path.exists(cur) else []
            if not bak: out.append(f"+++ new file {rel} ({len(new)} lines)"); continue
            d = list(difflib.unified_diff(old, new, f"a/{rel}", f"b/{rel}", n=2, lineterm=""))
            out += d or [f"(no net change in {rel})"]
        return "\n".join(out) or "(no changes in this session)"

    # ---- build / adb --------------------------------------------------------------------- #
    def _gradlew(self):
        n = "gradlew.bat" if os.name == "nt" else "gradlew"
        p = os.path.join(self.root, n)
        return p if os.path.isfile(p) else None

    def t_build(self, a):
        task = str(a.get("task") or "assembleDebug").strip()
        m = re.fullmatch(r"((?::[\w\-]+)*:)?(\w+)", task)
        if not m or m.group(2) not in GRADLE_TASKS:
            return f"error: task must be one of {', '.join(sorted(GRADLE_TASKS))} (optional module via 'module')"
        mod = str(a.get("module") or "").strip(":")
        full = (m.group(1) or (f":{mod}:" if mod else "")) + m.group(2)
        gw = self._gradlew()
        cmd = ([gw] if os.name == "nt" else (["sh", gw] if not os.access(gw, os.X_OK) else [gw])) if gw else None
        if cmd is None:
            if not shutil.which("gradle"): return "error: no gradlew in project and no gradle on PATH"
            cmd = ["gradle"]
        cmd += [full, "--console=plain"]
        print(col(f"  [build] {' '.join(cmd[-2:])} ...", "33"))
        t0, lines = time.time(), []
        p = subprocess.Popen(cmd, cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
        timer = threading.Timer(self.build_timeout, p.kill); timer.start()
        try:
            for l in p.stdout:
                l = l.rstrip(); lines.append(l)
                if TTY and l.startswith("> Task"): sys.stdout.write(col("\r    " + l[:70].ljust(70), "90")); sys.stdout.flush()
            p.wait()
        finally:
            timer.cancel()
        if TTY: print()
        ok, secs = p.returncode == 0, int(time.time() - t0)
        if p.returncode in (-9, -15, None) and secs >= self.build_timeout: return f"error: build timed out after {secs}s"
        self.last_build = f"{full} {'OK' if ok else 'FAILED'} (step-time {secs}s)"
        if ok: self.dirty = False
        return self._summarize_build(lines, ok, full, secs)

    def _norm_path(self, p):
        p = re.sub(r"^file://", "", p.replace("\\", "/"))
        root = self.root.replace("\\", "/")
        for q in (p, p.lstrip("/")):
            if q.lower().startswith(root.lower() + "/"): return q[len(root) + 1:]
        return p

    def _summarize_build(self, lines, ok, task, secs):
        warns = sum(1 for l in lines if l.startswith("w: ") or ": warning:" in l)
        if ok: return f"BUILD SUCCESSFUL ({task}, {secs}s, {warns} warnings)"
        errs, seen = [], set()
        pats = [re.compile(r"^e: (.+?):(\d+):(\d+)\s*:?\s*(.*)$"), re.compile(r"^e: (.+?): \((\d+), (\d+)\): (.*)$"),
                re.compile(r"^(.+?\.java):(\d+): error: (.*)$")]
        for l in lines:
            for i, rx in enumerate(pats):
                m = rx.match(l)
                if m:
                    f, ln, msg = self._norm_path(m.group(1)), int(m.group(2)), m.group(m.lastindex)
                    key = (f, ln, msg)
                    if key not in seen:
                        seen.add(key); src = self._src_line(f, ln)
                        errs.append(f"{f}:{ln}: {msg[:200]}" + (f"\n    > {src}" if src else ""))
                    break
        other = [l for l in lines if re.search(r"(AAPT|error:|Could not resolve|Execution failed|Duplicate class|"
                                               r"Manifest merger failed|Unresolved reference)", l) and not l.startswith("e: ")][:8]
        wrong = []
        for i, l in enumerate(lines):
            if l.startswith("* What went wrong"): wrong = [x for x in lines[i + 1:i + 6] if x.strip()]; break
        out = [f"BUILD FAILED ({task}, {secs}s): {len(errs)} compile errors"] + errs[:25]
        if len(errs) > 25: out.append(f"... +{len(errs) - 25} more errors (fix these first)")
        if other and not errs: out += ["other:"] + [o[:220] for o in other]
        if wrong and not errs: out += ["what went wrong:"] + [w[:220] for w in wrong]
        if not errs and not other and not wrong: out += ["tail:"] + [l[:200] for l in lines[-20:]]
        return "\n".join(out)

    def t_adb(self, a):
        adb = shutil.which("adb")
        if not adb: return "error: adb not found in PATH"
        op, num = str(a.get("op", "")), lambda v, d: max(1, min(int(v or d), 500))
        ok = lambda v: bool(re.fullmatch(r"[\w.$/]+", str(v or "")))
        if op == "devices": cmd = [adb, "devices"]
        elif op == "install":
            apk = self._res(a.get("apk", ""))
            if not apk.endswith(".apk") or not os.path.isfile(apk): return "error: apk path not found (build assembleDebug first)"
            cmd = [adb, "install", "-r", apk]
        elif op == "start":
            if not ok(a.get("component")): return "error: component like com.pkg/.MainActivity"
            cmd = [adb, "shell", "am", "start", "-n", a["component"]]
        elif op == "stop":
            if not ok(a.get("package")): return "error: package name required"
            cmd = [adb, "shell", "am", "force-stop", a["package"]]
        elif op == "clear_logcat": cmd = [adb, "logcat", "-c"]
        elif op == "logcat":
            flt = shlex.split(str(a.get("filter") or "AndroidRuntime:E *:S"))
            if not all(re.fullmatch(r"[\w.*]+:[VDIWEFS]|[\w.*]+", t) for t in flt): return "error: bad logcat filter"
            cmd = [adb, "logcat", "-d", "-t", str(num(a.get("lines"), 200))] + flt
        else: return "error: op must be devices | install | start | stop | logcat | clear_logcat"
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=60, text=True, errors="replace")
            return (r.stdout + r.stderr).strip()[-4000:] or "(no output)"
        except Exception as e:
            return f"error: {e}"


# --------------------------------------------------------------------------- #
# llama.cpp sunucusu (opsiyonel) ve CLI
# --------------------------------------------------------------------------- #
def start_server(a):
    exe = a.server_bin or shutil.which("llama-server")
    if not exe: sys.exit("llama-server bulunamadi: PATH'e ekle veya --server-bin ver.")
    cmd = [exe, "-m", a.model, "--port", str(a.port), "-c", str(a.ctx)] + shlex.split(a.server_args or "")
    log = tempfile.NamedTemporaryFile("w", suffix=".log", delete=False)
    print(col(f"[*] llama-server baslatiliyor (log: {log.name})", "33"))
    proc = subprocess.Popen(cmd, stdout=log, stderr=log)
    atexit.register(proc.terminate)
    url = f"http://127.0.0.1:{a.port}"
    for _ in range(600):
        if proc.poll() is not None: sys.exit(f"llama-server cikti, logu incele: {log.name}")
        try:
            with urllib.request.urlopen(url + "/health", timeout=2) as r:
                if r.status == 200: return url
        except Exception:
            pass
        time.sleep(1)
    sys.exit("llama-server zamaninda hazir olmadi.")


def main():
    ap = argparse.ArgumentParser(description="Android uygulama gelistirme ajani (Ollama / llama.cpp)")
    ap.add_argument("project", help="Android projesinin kok klasoru (settings.gradle(.kts) burada)")
    ap.add_argument("-g", "--goal", help="tek seferlik gorev; yoksa interaktif")
    ap.add_argument("--backend", choices=("ollama", "llama"), default="ollama")
    ap.add_argument("-u", "--url", help="backend URL; Ollama varsayilan http://127.0.0.1:11434")
    ap.add_argument("-m", "--model", help="GGUF yolu (llama backend)")
    ap.add_argument("--server-bin"); ap.add_argument("--server-args")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--ctx", type=int, default=32768)
    ap.add_argument("--api-key")
    ap.add_argument("--model-name", default="qwen3.5:latest")
    ap.add_argument("--temp", type=float, default=0.6, help="thinking aciksa cok dusuk sicaklik dongu yapar")
    ap.add_argument("--max-tokens", type=int, default=8192, help="thinking DAHIL cevap basina limit")
    ap.add_argument("--max-steps", type=int, default=40)
    ap.add_argument("--max-obs", type=int, default=5000)
    ap.add_argument("--state", help="notlar/yedekler klasoru (varsayilan <proje>/.agent)")
    ap.add_argument("--build-timeout", type=int, default=900)
    ap.add_argument("--no-think", action="store_true", help="thinking kapali (daha hizli, daha az akilli)")
    a = ap.parse_args()
    if not os.path.isdir(a.project): sys.exit(f"Klasor yok: {a.project}")
    if a.backend == "ollama":
        if a.model: sys.exit("--backend ollama ile -m kullanilmaz; --model-name kullan.")
        url, model = (a.url or "http://127.0.0.1:11434").rstrip("/"), a.model_name
    else:
        url, model = (start_server(a) if a.model else (a.url or "http://127.0.0.1:8080")), a.model_name or None
    llm = LLM(url, a.temp, a.max_tokens, a.api_key, model, a.backend, a.ctx)
    llm.think = not a.no_think
    ctx = llm.n_ctx() or a.ctx
    print(col(f"[*] {a.backend} {url} model={model} ctx={ctx} max_tokens={a.max_tokens} think={llm.think}", "33"))
    agent = Agent(llm, a.project, a.state or os.path.join(a.project, ".agent"), ctx, a.max_steps, a.max_obs, a.build_timeout)
    print(col(f"[*] proje={agent.root}\n[*] durum/yedek={agent.state}", "33"))

    def handle(goal):
        rep = agent.run_goal(goal)
        if rep:
            print(col("\n====== RAPOR ======", "35") + f"\n{rep}\n")
            with open(os.path.join(agent.state, "REPORT.md"), "a", encoding="utf-8") as f: f.write(f"## {goal}\n\n{rep}\n\n")
    if a.goal: handle(a.goal); return
    print("Gorevi yaz. Komutlar: /notes /diff [yol] /undo <yol> /build [task] /think on|off /reset /quit")
    while True:
        try: line = input(col("dev> ", "35")).strip()
        except (EOFError, KeyboardInterrupt): print(); break
        if not line: continue
        if line in ("/quit", "/q"): break
        elif line == "/notes": print("\n".join(f"- {n}" for n in agent.notes) or "(bos)")
        elif line == "/reset": agent.msgs.clear(); agent.tried.clear(); agent.seen.clear(); print("konusma sifirlandi (notlar duruyor)")
        elif line.startswith("/diff"): print(agent.t_diff({"path": line[5:].strip() or None}))
        elif line.startswith("/undo "): print(agent.tool({"name": "undo", "args": {"path": line[6:].strip()}}))
        elif line.startswith("/build"): print(agent.tool({"name": "build", "args": {"task": line[6:].strip() or "assembleDebug"}}))
        elif line.startswith("/think"): llm.think = line.endswith("on"); print(f"think={llm.think}")
        else: handle(line)


if __name__ == "__main__": main()

