#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""android_developer.py — Android (Kotlin/Java/Gradle) gelistirme ajani.

Kullanim:
    python android_developer.py <proje_klasoru> [-g "gorev"]
"""
import argparse, json, os, re, shutil, subprocess, sys, threading, time
import xml.etree.ElementTree as ET
from collections import defaultdict

from agent_core import (
    BaseAgent, LLM, col, TTY, add_common_args, build_llm_from_args,
)

# --------------------------------------------------------------------------- #
# Android sabitleri
# --------------------------------------------------------------------------- #
SKIP_DIRS     = {".gradle", ".git", ".idea", ".agent", "node_modules", ".cxx", "captures", ".kotlin"}
TEXT_EXTS     = (".kt", ".kts", ".java", ".xml", ".gradle", ".toml", ".properties", ".json", ".md",
                 ".txt", ".pro", ".yml", ".yaml", ".cfg", ".proto", ".aidl", ".sh", ".bat", ".html",
                 ".css", ".js")
BIN_EXTS      = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".apk", ".aab", ".jar", ".so", ".ttf",
                 ".otf", ".ico", ".keystore", ".jks", ".zip", ".mp3", ".mp4", ".ogg", ".class", ".dex")
NO_WRITE_NAMES = {"local.properties", "gradle-wrapper.jar"}
GRADLE_TASKS   = {"assembleDebug", "compileDebugKotlin", "compileDebugJavaWithJavac",
                  "compileDebugSources", "lintDebug", "testDebugUnitTest"}
MUTATORS       = {"edit_file", "write_file", "delete_file", "undo"}

PLACEHOLDER_RE = re.compile(
    r"(?im)^\s*(?://|/\*|#|<!--)[^\n]*\b(rest of (the )?(code|file)|existing code|"
    r"remaining code|same as before|unchanged)\b|^\s*\.\.\.\s*$"
)
KT_DECL = re.compile(
    r"^(\s*)(?:@\w+(?:\([^)]*\))?\s+)*(?:(?:public|private|protected|internal|open|abstract|sealed|"
    r"data|enum|annotation|inner|override|suspend|inline|const|lateinit|operator|companion|value|"
    r"final|static)\s+)*(class|interface|object|fun|typealias)\b\s*(?:<[^>]*>\s*)?([\w.]+)"
)
JAVA_M = re.compile(
    r"^(\s{0,8})(?:public|private|protected)\s+(?:static\s+)?(?:final\s+)?(?:synchronized\s+)?"
    r"[\w<>\[\],.? ]+?\s+(\w+)\s*\("
)
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
1. Minimal diffs: change only what the task needs. Keep the project's architecture, naming, formatting and UI toolkit.
   Reuse libraries already in the project.
2. Do NOT change Gradle/AGP/Kotlin/compileSdk versions or add dependencies unless the task requires it.
   Never touch signing configs, local.properties or keystores.
3. Never invent APIs. Mark UNVERIFIED when unsure. Respect minSdk.
4. User-visible text goes to res/values/strings.xml. Register new Activities/Services in manifest. Add imports.
5. No placeholders such as "..." or "// rest of code" anywhere.
6. Prefer edit_file with a small unique <old> (3-8 lines) over rewriting.
7. If the same error survives 2 attempts: read surrounding code, note what you learned, change approach.
8. Reply language: the user's language. Code, identifiers and code comments in English.
9. FORMAT: no markdown; one or two plain sentences, exactly one call, then STOP. Never write an OBSERVATION yourself.
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
    {"role": "user", "content": "OBSERVATION:\nedited app/src/main/res/values/strings.xml (1 replacement)\n\n"
                                "(End of example. The real task follows. Always answer with one short plain sentence "
                                "and exactly one call, no markdown.)"},
    {"role": "assistant", "content": "Understood. One short sentence and one call per turn."},
]


# --------------------------------------------------------------------------- #
# Agent
# --------------------------------------------------------------------------- #
class AndroidAgent(BaseAgent):
    TOOLS = {"project_info", "manifest", "ls", "find", "outline", "read",
             "edit_file", "write_file", "delete_file", "undo", "diff",
             "build", "adb", "note", "finish"}
    FEWSHOT       = FEWSHOT
    SKIP_DIRS     = SKIP_DIRS
    TEXT_EXTS     = TEXT_EXTS
    BIN_EXTS      = BIN_EXTS
    NO_WRITE_NAMES = NO_WRITE_NAMES
    MUTATORS      = MUTATORS

    def __init__(self, llm, proj, state, ctx, max_steps, max_obs, build_timeout=900):
        super().__init__(llm, proj, state, ctx, max_steps, max_obs)
        self.build_timeout = build_timeout
        self.last_build    = "not run yet"
        self.system_base   = (
            SYSTEM.replace("@@PROJ@@", os.path.basename(self.root))
                  .replace("@@INFO@@", self.t_project_info({})[:2200])
        )

    # ---- Kotlin/Java bracket check override -------------------------------- #
    @staticmethod
    def _balance_kt(t):
        t = re.sub(r'"""[\s\S]*?"""', '""', t)
        t = re.sub(r'/\*[\s\S]*?\*/', '', t)
        t = re.sub(r'//[^\n]*', '', t)
        t = re.sub(r'"(?:\\.|[^"\\\n])*"', '""', t)
        t = re.sub(r"'(?:\\.|[^'\\\n])'", "''", t)
        return (t.count("{") - t.count("}"), t.count("(") - t.count(")"), t.count("[") - t.count("]"))

    def _post_edit_check(self, rel, before, after):
        r = rel.lower()
        if r.endswith((".kt", ".java", ".kts", ".gradle")):
            b, a_ = self._balance_kt(before or ""), self._balance_kt(after)
            if a_ != b:
                return f"\nWARNING: bracket balance changed ({b} -> {a_}); re-read the edited area."
        elif r.endswith(".xml"):
            try:
                ET.fromstring(after.encode("utf-8"))
            except ET.ParseError as e:
                return f"\nWARNING: XML is not well-formed: {e}"
        elif r.endswith(".json"):
            try:
                json.loads(after)
            except ValueError as e:
                return f"\nWARNING: invalid JSON: {e}"
        return ""

    def system_text(self):
        s = super().system_text()
        s += f"\nLAST BUILD: {self.last_build}" + (" (files edited since then)" if self.dirty else "")
        return s

    # ---- Kesif araclari ---------------------------------------------------- #
    def _in_build_dir(self, full):
        d = os.path.dirname(full)
        while d.startswith(self.root) and d != self.root:
            if os.path.basename(d) == "build" and any(
                os.path.exists(os.path.join(os.path.dirname(d), n))
                for n in ("build.gradle", "build.gradle.kts")
            ):
                return True
            d = os.path.dirname(d)
        return False

    def _res(self, p, write=False):
        full = super()._res(p, write)
        if write and self._in_build_dir(full):
            rel = os.path.relpath(full, self.root).replace(os.sep, "/")
            raise ValueError(f"writing to '{rel}' is not allowed (generated/internal dir)")
        return full

    def t_project_info(self, a):
        o, root = [], self.root
        has = lambda n: os.path.exists(os.path.join(root, n))
        if not (has("settings.gradle") or has("settings.gradle.kts")):
            o.append("WARNING: no settings.gradle(.kts) in root; is this the project root?")
        o.append(f"gradlew: {'yes' if self._gradlew() else 'NO'}")
        st = next((open(os.path.join(root, n), encoding="utf-8", errors="replace").read()
                   for n in ("settings.gradle.kts", "settings.gradle") if has(n)), "")
        mods = sorted(set(re.findall(r"""["'](:[\\w:\\-]+)["']""", st)))
        if mods:
            o.append("modules: " + ", ".join(mods))
        for gf in list(self._gradle_files())[:4]:
            t = open(gf, encoding="utf-8", errors="replace").read()
            rel = self._rel(gf)
            o.append(f"[{rel}]")
            nm  = re.findall(r"""(namespace|applicationId)\s*=?\s*["']([^"']+)["']""", t)
            sdk = re.findall(r"(compileSdk|minSdk|targetSdk)(?:Version)?\s*=?\s*\(?\s*(\d+)", t)
            if nm or sdk:
                o.append("  " + " ".join(f"{k}={v}" for k, v in nm + sdk))
            flags = [k for k, w in (
                ("compose", "compose"), ("viewBinding", "viewbinding"), ("dataBinding", "databinding"),
                ("hilt", "hilt"), ("room", "room"), ("retrofit", "retrofit"),
                ("coroutines", "coroutines"), ("navigation", "navigation"),
            ) if w in t.lower()]
            if flags:
                o.append("  uses: " + ", ".join(flags))
        kt = sum(1 for _ in self._walk(root, "*.kt"))
        jv = sum(1 for _ in self._walk(root, "*.java"))
        lay = sum(1 for f in self._walk(root, "*.xml") if f"{os.sep}layout" in f)
        o.append(f"sources: {kt} .kt, {jv} .java, {lay} layout xml")
        return "\n".join(o)

    def _gradle_files(self):
        for dp, dirs, fns in os.walk(self.root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and d != "build" and d != "src"]
            for fn in fns:
                if fn in ("build.gradle", "build.gradle.kts"):
                    yield os.path.join(dp, fn)

    def t_manifest(self, a):
        ms = [f for f in self._walk(self.root, "AndroidManifest.xml")
              if f"{os.sep}src{os.sep}main{os.sep}" in f]
        mod = str(a.get("module", "")).strip(":/")
        if mod:
            ms = [f for f in ms if self._rel(f).startswith(mod)] or ms
        if not ms:
            return "error: AndroidManifest.xml not found"
        ns, o = "{http://schemas.android.com/apk/res/android}", []
        for mp in ms[:2]:
            try:
                r = ET.parse(mp).getroot()
            except ET.ParseError:
                o.append(f"[{self._rel(mp)}] unparsable")
                continue
            o.append(f"[{self._rel(mp)}] package={r.get('package')}")
            o.append("permissions: " + ", ".join(sorted(
                p.get(ns + "name", "").replace("android.permission.", "")
                for p in r.findall("uses-permission"))[:40]))
            app = r.find("application")
            if app is None:
                continue
            for kind in ("activity", "service", "receiver", "provider"):
                items = []
                for e in app.findall(kind):
                    launcher = any(c.get(ns + "name", "").endswith("LAUNCHER") for c in e.iter("category"))
                    items.append(e.get(ns + "name", "") + (" [LAUNCHER]" if launcher else "")
                                 + (" [exported]" if e.get(ns + "exported") == "true" else ""))
                if items:
                    label = "activities" if kind == "activity" else kind + "s"
                    o.append(f"{label} ({len(items)}): " + "; ".join(items[:20]))
        return "\n".join(o)

    def t_outline(self, a):
        p = self._file(a.get("file") or a.get("path", ""))
        if not p.endswith((".kt", ".java")):
            return "error: outline works for .kt/.java only; use read"
        rows = []
        with open(p, encoding="utf-8", errors="replace") as f:
            for i, l in enumerate(f, 1):
                m = KT_DECL.match(l)
                if m and len(m.group(1)) <= 8:
                    rows.append(f"{i}: {l.rstrip()[:130]}")
                elif p.endswith(".java"):
                    j = JAVA_M.match(l)
                    if j and j.group(2) not in NOISE_KW:
                        rows.append(f"{i}: {l.rstrip()[:130]}")
        return f"{self._rel(p)}: {len(rows)} declarations\n" + "\n".join(rows[:120])

    # ---- Build / ADB -------------------------------------------------------- #
    def _gradlew(self):
        n = "gradlew.bat" if os.name == "nt" else "gradlew"
        p = os.path.join(self.root, n)
        return p if os.path.isfile(p) else None

    def t_build(self, a):
        import shlex as _shlex
        task = str(a.get("task") or "assembleDebug").strip()
        m = re.fullmatch(r"((?::\w[\w\-]*)*)/?(\w+)", task)
        if not m or m.group(2) not in GRADLE_TASKS:
            return f"error: task must be one of {', '.join(sorted(GRADLE_TASKS))}"
        mod  = str(a.get("module") or "").strip(":")
        full = (m.group(1) or (f":{mod}:" if mod else "")) + m.group(2)
        gw   = self._gradlew()
        if gw:
            cmd = ([gw] if os.name == "nt" else (["sh", gw] if not os.access(gw, os.X_OK) else [gw]))
        else:
            if not shutil.which("gradle"):
                return "error: no gradlew in project and no gradle on PATH"
            cmd = ["gradle"]
        cmd += [full, "--console=plain"]
        print(col(f"  [build] {' '.join(cmd[-2:])} ...", "33"))
        t0, lines = time.time(), []
        p = subprocess.Popen(cmd, cwd=self.root, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, errors="replace")
        timer = threading.Timer(self.build_timeout, p.kill)
        timer.start()
        try:
            for l in p.stdout:
                l = l.rstrip()
                lines.append(l)
                if TTY and l.startswith("> Task"):
                    sys.stdout.write(col("\r    " + l[:70].ljust(70), "90"))
                    sys.stdout.flush()
            p.wait()
        finally:
            timer.cancel()
        if TTY:
            print()
        ok, secs = p.returncode == 0, int(time.time() - t0)
        if p.returncode in (-9, -15, None) and secs >= self.build_timeout:
            return f"error: build timed out after {secs}s"
        self.last_build = f"{full} {'OK' if ok else 'FAILED'} (step-time {secs}s)"
        if ok:
            self.dirty = False
        return self._summarize_build(lines, ok, full, secs)

    def _norm_path(self, p):
        p = re.sub(r"^file://", "", p.replace("\\", "/"))
        root = self.root.replace("\\", "/")
        for q in (p, p.lstrip("/")):
            if q.lower().startswith(root.lower() + "/"):
                return q[len(root) + 1:]
        return p

    def _summarize_build(self, lines, ok, task, secs):
        warns = sum(1 for l in lines if l.startswith("w: ") or ": warning:" in l)
        if ok:
            return f"BUILD SUCCESSFUL ({task}, {secs}s, {warns} warnings)"
        errs, seen = [], set()
        pats = [
            re.compile(r"^e: (.+?):(\d+):(\d+)\s*:?\s*(.*)$"),
            re.compile(r"^e: (.+?): \((\d+), (\d+)\): (.*)$"),
            re.compile(r"^(.+?\.java):(\d+): error: (.*)$"),
        ]
        for l in lines:
            for rx in pats:
                m = rx.match(l)
                if m:
                    f, ln, msg = self._norm_path(m.group(1)), int(m.group(2)), m.group(m.lastindex)
                    key = (f, ln, msg)
                    if key not in seen:
                        seen.add(key)
                        src = self._src_line(f, ln)
                        errs.append(f"{f}:{ln}: {msg[:200]}" + (f"\n    > {src}" if src else ""))
                    break
        out = [f"BUILD FAILED ({task}, {secs}s): {len(errs)} compile errors"] + errs[:25]
        if not errs:
            other = [l for l in lines if re.search(
                r"(AAPT|error:|Could not resolve|Execution failed|Duplicate class|"
                r"Manifest merger failed|Unresolved reference)", l) and not l.startswith("e: ")][:8]
            out += (["other:"] + [o[:220] for o in other]) if other else []
        return "\n".join(out)

    def _src_line(self, rel, ln):
        try:
            with open(os.path.join(self.root, rel), encoding="utf-8", errors="replace") as f:
                for i, l in enumerate(f, 1):
                    if i == ln:
                        return l.strip()[:140]
        except OSError:
            pass
        return ""

    def t_adb(self, a):
        adb = shutil.which("adb")
        if not adb:
            return "error: adb not found in PATH"
        import shlex as _shlex
        op = str(a.get("op", ""))
        num = lambda v, d: max(1, min(int(v or d), 500))
        ok  = lambda v: bool(re.fullmatch(r"[\w.$/]+", str(v or "")))
        if op == "devices":
            cmd = [adb, "devices"]
        elif op == "install":
            apk = self._res(a.get("apk", ""))
            if not apk.endswith(".apk") or not os.path.isfile(apk):
                return "error: apk path not found (build assembleDebug first)"
            cmd = [adb, "install", "-r", apk]
        elif op == "start":
            if not ok(a.get("component")):
                return "error: component like com.pkg/.MainActivity"
            cmd = [adb, "shell", "am", "start", "-n", a["component"]]
        elif op == "stop":
            if not ok(a.get("package")):
                return "error: package name required"
            cmd = [adb, "shell", "am", "force-stop", a["package"]]
        elif op == "clear_logcat":
            cmd = [adb, "logcat", "-c"]
        elif op == "logcat":
            flt = _shlex.split(str(a.get("filter") or "AndroidRuntime:E *:S"))
            cmd = [adb, "logcat", "-d", "-t", str(num(a.get("lines"), 200))] + flt
        else:
            return "error: op must be devices | install | start | stop | logcat | clear_logcat"
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=60, text=True, errors="replace")
            return (r.stdout + r.stderr).strip()[-4000:] or "(no output)"
        except Exception as e:
            return f"error: {e}"


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Android gelistirme ajani (Ollama / llama.cpp)")
    ap.add_argument("project", help="Android projesinin kok klasoru")
    ap.add_argument("-g", "--goal", help="tek seferlik gorev; yoksa interaktif")
    ap.add_argument("--state",         help="notlar/yedekler (varsayilan <proje>/.agent)")
    ap.add_argument("--build-timeout", type=int, default=900)
    add_common_args(ap, default_temp=0.6, default_max_tokens=8192, default_max_steps=40)
    a = ap.parse_args()

    if not os.path.isdir(a.project):
        sys.exit(f"Klasor yok: {a.project}")
    llm, url, model = build_llm_from_args(a)
    ctx = llm.n_ctx() or a.ctx
    print(col(f"[*] {a.backend} {url} model={model} ctx={ctx} max_tokens={a.max_tokens} think={llm.think}", "33"))

    agent = AndroidAgent(llm, a.project, a.state or os.path.join(a.project, ".agent"),
                         ctx, a.max_steps, a.max_obs, a.build_timeout)
    print(col(f"[*] proje={agent.root}\n[*] durum/yedek={agent.state}", "33"))

    def handle(goal):
        rep = agent.run_goal(goal)
        if rep:
            print(col("\n====== RAPOR ======", "35") + f"\n{rep}\n")
            with open(os.path.join(agent.state, "REPORT.md"), "a", encoding="utf-8") as f:
                f.write(f"## {goal}\n\n{rep}\n\n")

    if a.goal:
        handle(a.goal)
        return
    print("Gorevi yaz. Komutlar: /notes /diff [yol] /undo <yol> /build [task] /think on|off /reset /quit")
    while True:
        try:
            line = input(col("android> ", "35")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line in ("/quit", "/q"):
            break
        elif line == "/notes":
            print("\n".join(f"- {n}" for n in agent.notes) or "(bos)")
        elif line == "/reset":
            agent.msgs.clear(); agent.tried.clear(); agent.seen.clear()
            print("konusma sifirlandi (notlar duruyor)")
        elif line.startswith("/diff"):
            print(agent.t_diff({"path": line[5:].strip() or None}))
        elif line.startswith("/undo "):
            print(agent.tool({"name": "undo", "args": {"path": line[6:].strip()}}))
        elif line.startswith("/build"):
            print(agent.tool({"name": "build", "args": {"task": line[6:].strip() or "assembleDebug"}}))
        elif line.startswith("/think"):
            llm.think = line.endswith("on"); print(f"think={llm.think}")
        else:
            handle(line)


if __name__ == "__main__":
    main()
