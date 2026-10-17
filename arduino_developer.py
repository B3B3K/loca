#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""arduino_developer.py — Arduino/ESP32 firmware gelistirme ajani.

Kullanim:
    python arduino_developer.py <sketch_klasoru> [-g "gorev"]
"""
import argparse, json, os, re, sys

from agent_core import (
    BaseAgent, col, add_common_args, build_llm_from_args,
)

# --------------------------------------------------------------------------- #
# Arduino sabitleri
# --------------------------------------------------------------------------- #
SKIP_DIRS = {".git", ".agent", "node_modules", "build", ".pio", ".vscode", "__pycache__"}
TEXT_EXTS = (
    ".ino", ".cpp", ".c", ".h", ".hpp",
    ".json", ".yaml", ".yml", ".toml", ".ini",
    ".md", ".txt", ".rst",
    ".py", ".sh", ".bat",
    ".cmake", ".mk", "makefile",
    ".properties", ".cfg",
)
BIN_EXTS = (".hex", ".bin", ".elf", ".o", ".a", ".png", ".jpg", ".gif", ".ico",
            ".webp", ".zip", ".tar", ".gz", ".ttf", ".otf")
NO_WRITE_NAMES = {"boards.txt", "platform.txt"}
MUTATORS       = {"edit_file", "write_file", "delete_file", "undo"}

PLACEHOLDER_RE = re.compile(
    r"(?im)^\s*(?://|/\*|#)[^\n]*\b(rest of (the )?(code|file)|existing code|"
    r"remaining code|same as before|unchanged)\b|^\s*\.\.\.\s*$"
)
C_DECL = re.compile(
    r"^(\s*)(?:#(?:define|include|pragma|ifdef|ifndef|endif|if|else|elif)\b"
    r"|(?:class|struct|enum|union|namespace)\s+\w+"
    r"|(?:void|bool|int|uint\w*|int\w*|float|double|char|String|long|unsigned|static|inline)\s+\w+\s*\("
    r"|(?:void|bool|int|uint\w*|float|double|char|String|long)\s+\w+\s*;)"
)

SYSTEM = r"""You are an expert embedded systems and Arduino/ESP32 firmware developer agent (C and C++, Arduino framework, ESP-IDF).
You work INSIDE an existing, working project and implement the user's change with minimal, correct edits.
There is NO real compiler available. Your job is: read -> understand -> plan -> edit -> passive compile check.

PROJECT SNAPSHOT (root: @@PROJ@@)
@@INFO@@

CALL FORMAT: one or two plain sentences, then exactly one call, then STOP.
<tool>{"name": "find", "args": {"pattern": "BLEDevice::init", "glob": "*.ino"}}</tool>
Code blocks after the call:
<tool>{"name": "edit_file", "args": {"path": "backend/backend.ino"}}</tool>
<old>exact existing lines</old>
<new>replacement lines</new>

TOOLS ([x] = optional)
- project_info {}   board, framework, libraries, sketch structure, #include map.
- ls {[path]}   directory listing.
- find {pattern, [path], [glob], [ignore_case=true], [files_only=false], [limit=40]}   regex search.
- outline {file}   functions/classes/macros with line numbers (.ino/.cpp/.h only).
- read {file, [start=1], [end]}   numbered lines, max 250 per call.
- edit_file {path} + <old>/<new>   replace one exact block.
- write_file {path, [overwrite=false]} + <content>   create or overwrite a file.
- delete_file {path}   remove a file (backed up).   undo {path}   restore to session-start state.
- diff {[path]}   unified diff of everything changed this session.
- check {file}   passive compile check: bracket balance, #include consistency, obvious C++ issues.
- note {text}   durable plan item or fact; re-shown every turn.
- finish {report}   final report.

WORKFLOW
1 ORIENT: use the snapshot, find/outline only files that matter. Read before editing.
2 PLAN: note 2-6 concrete steps.
3 IMPLEMENT: one small edit at a time. After each edit run check on the modified file.
4 REVIEW: run diff, compare with request and existing code style, then finish.

RULES
1. Minimal diffs. Reuse libraries already in the project. Never add a library unless required.
2. Never touch board/hardware pin definitions unless the task explicitly changes hardware.
3. Never invent APIs. Mark UNVERIFIED when unsure.
4. No placeholders such as "..." or "// rest of code" anywhere.
5. Prefer edit_file with a small unique <old> (3-8 lines) over rewriting whole files.
6. Reply language: the user's language. Code, identifiers and code comments in English.
7. FORMAT: no markdown; one or two plain sentences, exactly one call, then STOP.
"""

FEWSHOT = [
    {"role": "user", "content": "EXAMPLE of the required turn format. Task: rename a BLE service UUID constant."},
    {"role": "assistant", "content": "I need to find where the UUID is defined.\n"
                                     "<tool>{\"name\": \"find\", \"args\": {\"pattern\": \"SERVICE_UUID\", \"glob\": \"*.h\"}}</tool>"},
    {"role": "user", "content": "OBSERVATION:\n2 matches in 1 files\nbackend/ble_config.h  (2 hits)\n"
                                "  3: #define SERVICE_UUID \"6E400001-B5A3-F393-E0A9-E50E24DCCA9E\"\n"
                                "  9: BLEService *pService = pServer->createService(SERVICE_UUID);"},
    {"role": "assistant", "content": "I will update the #define in ble_config.h.\n"
                                     "<tool>{\"name\": \"edit_file\", \"args\": {\"path\": \"backend/ble_config.h\"}}</tool>\n"
                                     "<old>\n#define SERVICE_UUID \"6E400001-B5A3-F393-E0A9-E50E24DCCA9E\"\n</old>\n"
                                     "<new>\n#define SERVICE_UUID \"12345678-1234-1234-1234-123456789abc\"\n</new>"},
    {"role": "user", "content": "OBSERVATION:\nedited backend/ble_config.h (1 replacement)\n\n"
                                "(End of example. The real task follows. Always answer with one short plain sentence "
                                "and exactly one call, no markdown.)"},
    {"role": "assistant", "content": "Understood. One short sentence and one call per turn."},
]


# --------------------------------------------------------------------------- #
# Agent
# --------------------------------------------------------------------------- #
class ArduinoAgent(BaseAgent):
    TOOLS = {"project_info", "ls", "find", "outline", "read",
             "edit_file", "write_file", "delete_file", "undo",
             "diff", "check", "note", "finish"}
    FEWSHOT        = FEWSHOT
    SKIP_DIRS      = SKIP_DIRS
    TEXT_EXTS      = TEXT_EXTS
    BIN_EXTS       = BIN_EXTS
    NO_WRITE_NAMES = NO_WRITE_NAMES
    MUTATORS       = MUTATORS

    def __init__(self, llm, proj, state, ctx, max_steps, max_obs):
        super().__init__(llm, proj, state, ctx, max_steps, max_obs)
        self.system_base = (
            SYSTEM.replace("@@PROJ@@", os.path.basename(self.root))
                  .replace("@@INFO@@", self.t_project_info({})[:2200])
        )

    # ---- C/C++ passive check ----------------------------------------------- #
    @staticmethod
    def _balance_c(text):
        t = re.sub(r'/\*[\s\S]*?\*/', '', text)
        t = re.sub(r'//[^\n]*', '', t)
        t = re.sub(r'"(?:\\.|[^"\\])*"', '""', t)
        t = re.sub(r"'(?:\\.|[^'\\])'", "''", t)
        return (t.count("{") - t.count("}"), t.count("(") - t.count(")"), t.count("[") - t.count("]"))

    def _passive_check(self, rel, content):
        issues = []
        ext = os.path.splitext(rel)[1].lower()
        if ext in (".ino", ".cpp", ".c", ".h", ".hpp"):
            b = self._balance_c(content)
            if b != (0, 0, 0):
                issues.append(f"bracket imbalance — braces:{b[0]:+d}  parens:{b[1]:+d}  brackets:{b[2]:+d}")
            includes = re.findall(r'^\s*#include\s*[<"]([^>"]+)[>"]', content, re.M)
            seen_inc = set()
            for inc in includes:
                if inc in seen_inc:
                    issues.append(f"duplicate #include: {inc}")
                seen_inc.add(inc)
            if re.search(r'\bdelay\s*\(', content) and re.search(r'\bvTaskDelay\s*\(', content):
                issues.append("WARNING: mixing delay() and vTaskDelay()")
        elif ext == ".json":
            try:
                json.loads(content)
            except ValueError as e:
                issues.append(f"invalid JSON: {e}")
        return "\n".join(issues)

    def _post_edit_check(self, rel, before, after):
        ext = os.path.splitext(rel)[1].lower()
        if ext in (".ino", ".cpp", ".c", ".h", ".hpp"):
            b_b, a_b = self._balance_c(before), self._balance_c(after)
            if a_b != b_b:
                return f"\nWARNING: bracket balance changed ({b_b} -> {a_b}); re-read the edited area."
        return ""

    def _placeholder_re(self):
        return PLACEHOLDER_RE

    # ---- Araclar ------------------------------------------------------------ #
    def t_project_info(self, a):
        o, root = [], self.root
        ino_files = list(self._walk(root, "*.ino"))
        cpp_files = list(self._walk(root, "*.cpp")) + list(self._walk(root, "*.c"))
        h_files   = list(self._walk(root, "*.h"))  + list(self._walk(root, "*.hpp"))
        o.append(f"sketch .ino: {len(ino_files)}  .cpp/.c: {len(cpp_files)}  .h/.hpp: {len(h_files)}")
        if ino_files:
            o.append("ino files: " + ", ".join(self._rel(f) for f in ino_files[:10]))
        all_includes = []
        for fp in ino_files + cpp_files + h_files:
            try:
                with open(fp, encoding="utf-8", errors="replace") as fh:
                    for l in fh:
                        m = re.match(r'^\s*#include\s*[<"]([^>"]+)[>"]', l)
                        if m:
                            all_includes.append(m.group(1))
            except OSError:
                pass
        unique_inc = sorted(set(all_includes))
        if unique_inc:
            o.append("includes: " + ", ".join(unique_inc[:40]))
        for fname in ("platformio.ini", "boards.txt", ".vscode/arduino.json", "sketch.json"):
            fp = os.path.join(root, fname)
            if os.path.isfile(fp):
                o.append(f"[{fname}]\n" + open(fp, encoding="utf-8", errors="replace").read()[:800])
                break
        flags = []
        inc_str = " ".join(unique_inc)
        for kw, label in (("WiFi", "WiFi"), ("BLE|NimBLE", "BLE"), ("FreeRTOS", "FreeRTOS"),
                          ("ESPAsyncWeb|AsyncWeb", "AsyncWebServer"), ("ArduinoJson|cJSON", "JSON"),
                          ("Wire\\.h|SoftI2C", "I2C"), ("SPI\\.h", "SPI"), ("EEPROM", "EEPROM"),
                          ("LittleFS|SPIFFS", "FS")):
            if re.search(kw, inc_str):
                flags.append(label)
        if flags:
            o.append("detected features: " + ", ".join(flags))
        return "\n".join(o)

    def t_outline(self, a):
        p = self._file(a.get("file") or a.get("path", ""))
        ext = os.path.splitext(p)[1].lower()
        if ext not in (".ino", ".cpp", ".c", ".h", ".hpp"):
            return "error: outline works for .ino/.cpp/.c/.h/.hpp only; use read"
        rows = []
        with open(p, encoding="utf-8", errors="replace") as f:
            for i, l in enumerate(f, 1):
                if C_DECL.match(l):
                    rows.append(f"{i}: {l.rstrip()[:140]}")
        return f"{self._rel(p)}: {len(rows)} declarations\n" + "\n".join(rows[:120])

    def t_check(self, a):
        rel = str(a.get("file") or a.get("path", "")).strip()
        if not rel:
            return "error: provide file path"
        try:
            p = self._file(rel)
            content = open(p, encoding="utf-8", errors="replace").read()
            result  = self._passive_check(self._rel(p), content)
            return result if result else "OK — no issues detected"
        except ValueError as e:
            return f"error: {e}"


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Arduino/ESP32 firmware gelistirme ajani (Ollama / llama.cpp)")
    ap.add_argument("project", help="Arduino sketch kok klasoru")
    ap.add_argument("-g", "--goal", help="tek seferlik gorev; yoksa interaktif")
    ap.add_argument("--state", help="notlar/yedekler (varsayilan <proje>/.agent)")
    add_common_args(ap, default_temp=0.4, default_max_tokens=8192, default_max_steps=40)
    a = ap.parse_args()

    if not os.path.isdir(a.project):
        sys.exit(f"Klasor yok: {a.project}")
    llm, url, model = build_llm_from_args(a)
    ctx = llm.n_ctx() or a.ctx
    print(col(f"[*] {a.backend} {url} model={model} ctx={ctx} max_tokens={a.max_tokens} think={llm.think}", "33"))

    agent = ArduinoAgent(llm, a.project, a.state or os.path.join(a.project, ".agent"),
                         ctx, a.max_steps, a.max_obs)
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
    print("Gorevi yaz. Komutlar: /notes /diff [yol] /undo <yol> /check <yol> /think on|off /reset /quit")
    while True:
        try:
            line = input(col("arduino> ", "35")).strip()
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
        elif line.startswith("/check "):
            print(agent.tool({"name": "check", "args": {"file": line[7:].strip()}}))
        elif line.startswith("/think"):
            llm.think = line.endswith("on"); print(f"think={llm.think}")
        else:
            handle(line)


if __name__ == "__main__":
    main()
