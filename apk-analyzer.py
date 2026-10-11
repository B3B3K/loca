#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agent.py - JADX + Ollama/llama.cpp Android APK reverse-engineering agent."""
import argparse, atexit, fnmatch, json, os, re, select, shlex, shutil, subprocess, sys, tempfile, time, urllib.error, urllib.request, uuid, zlib
import xml.etree.ElementTree as ET
from collections import defaultdict
if os.name == "nt": os.system("")
TTY = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
STOPS = ["</tool>", "OBSERVATION", "Observation:", "**Observation"]
def col(s, code): return f"\033[{code}m{s}\033[0m" if TTY else s

class LLMError(Exception):
    def __init__(self, code, body):
        super().__init__(f"HTTP {code}: {body[:300]}")
        self.code, self.body = code, body


class LLM:
    def __init__(self, url, temperature, max_tokens, api_key=None, model=None,
                 backend="llama", ctx=8192):
        self.url = url.rstrip("/")
        self.temperature, self.max_tokens, self.api_key = temperature, max_tokens, api_key
        self.model = model
        self.backend = backend
        self.ctx = ctx
        self.think = None  # ollama: False => thinking kapali (tokenlari yemesin)

    @property
    def endpoint(self):
        if self.backend == "ollama":
            return self.url + "/api/chat"
        return self.url + "/v1/chat/completions"

    def _headers(self):
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def n_ctx(self):
        # llama.cpp exposes the server context through /props.
        # Ollama's native API does not expose the active num_ctx with a simple GET,
        # so the configured --ctx is used for pruning.
        if self.backend == "ollama":
            return self.ctx
        try:
            req = urllib.request.Request(self.url + "/props", headers=self._headers())
            with urllib.request.urlopen(req, timeout=5) as r:
                d = json.load(r)
            return int(d["default_generation_settings"]["n_ctx"])
        except Exception:
            return None

    def chat(self, messages, stop, on_token):
        if self.backend == "ollama":
            body = {
                "model": self.model,
                "messages": messages,
                "stream": True,
                "options": {
                    "temperature": self.temperature,
                    "num_predict": self.max_tokens,
                    "num_ctx": self.ctx,
                },
                "stop": stop,
                "keep_alive": "10m",
            }
            if self.think is not None:
                body["think"] = self.think
        else:
            body = {"messages": messages, "stream": True, "temperature": self.temperature,
                    "max_tokens": self.max_tokens, "stop": stop, "cache_prompt": True}
            if self.model:
                body["model"] = self.model

        req = urllib.request.Request(self.endpoint, json.dumps(body).encode(), self._headers())
        try:
            resp = urllib.request.urlopen(req, timeout=900)
        except urllib.error.HTTPError as e:
            raise LLMError(e.code, e.read().decode("utf-8", "replace"))

        parts = []
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            if self.backend == "ollama":
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                tok = (d.get("message") or {}).get("content") or ""
                if tok:
                    parts.append(tok)
                    on_token(tok)
                if d.get("done"):
                    break
            else:
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    ch = json.loads(data).get("choices") or []
                except ValueError:
                    continue
                if not ch:
                    continue
                tok = (ch[0].get("delta") or {}).get("content") or ""
                if tok:
                    parts.append(tok)
                    on_token(tok)
        return "".join(parts)


def parse_call(text, names=None):
    dec = json.JSONDecoder(strict=False)
    starts = []
    m = re.search(r"<tool>", text)
    if m:
        starts.append(m.end())
    starts += [m2.start() for m2 in re.finditer(r"\{", text)]
    for s in starts:
        i = text.find("{", s)
        if i < 0:
            continue
        try:
            obj, _ = dec.raw_decode(text[i:])
        except ValueError:
            continue
        if isinstance(obj, dict) and "name" in obj:
            args = obj.get("args") or obj.get("arguments") or obj.get("parameters") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {"cmd": args}
            return {"name": str(obj["name"]), "args": args if isinstance(args, dict) else {}}
    if names:  # zayıf modeller `tool {json}` biçimini backtick içinde yazar
        for m in re.finditer(r"`\s*([a-z_0-9]+)\s*(\{.*?\})?\s*`", text, re.S):
            if m.group(1) in names:
                try:
                    args = json.loads(m.group(2), strict=False) if m.group(2) else {}
                except ValueError:
                    continue
                if isinstance(args, dict):
                    return {"name": m.group(1), "args": args}
    return None


class Agent:
    TOOLS = {"r2", "sh", "note", "finish"}
    FEWSHOT = [
        {"role": "user", "content": "EXAMPLE of the required turn format. Task: find the entry point."},
        {"role": "assistant", "content": "I need the function list first.\n"
                                         "<tool>{\"name\": \"r2\", \"args\": {\"cmd\": \"afl~main\"}}</tool>"},
        {"role": "user", "content": "OBSERVATION:\n0x00401136    1 24  sym.main\n\n(End of example. The real task "
                                    "follows. Always answer with one short plain sentence and exactly one <tool> call, "
                                    "no markdown.)"},
        {"role": "assistant", "content": "Understood. One short sentence and one <tool> call per turn."},
    ]

    def __init__(self, llm, r2, path, ctx, max_steps, max_obs, unsafe=False):
        self.llm, self.r2, self.path = llm, r2, path
        self.ctx, self.max_steps, self.max_obs, self.unsafe = ctx, max_steps, max_obs, unsafe
        self.notes, self.msgs, self.no_system = [], [], False
        self.tried, self.seen = [], {}
        info = r2.run("iI~arch,bits,os,format,stripped,canary,nx,pic,relro,static") or "(unavailable)"
        self.system_base = SYSTEM.format(path=path, info=info)

    # ---- mesaj yönetimi ---------------------------------------------------- #
    def add_user(self, text):
        if self.msgs and self.msgs[-1]["role"] == "user":
            self.msgs[-1]["content"] += "\n\n" + text
        else:
            self.msgs.append({"role": "user", "content": text})

    def system_text(self):
        s = self.system_base
        if self.notes:
            s += "\nNOTES SO FAR:\n" + "\n".join(f"- {n}" for n in self.notes)
        if self.tried:
            s += ("\nALREADY TRIED (never repeat these; results were not useful or are in NOTES):\n"
                  + "\n".join(self.tried[-25:]))
        return s

    def log_try(self, step, call, obs):
        a = json.dumps(call["args"], ensure_ascii=False)
        first = (obs.splitlines() or [""])[0]
        self.tried.append(f"s{step} {call['name']} {a[:80]} -> {first[:60]}")

    def fallback_report(self):
        return ("(auto-generated from notes: the agent never called finish)\n"
                + ("\n".join(f"- {n}" for n in self.notes) or "(no notes were recorded)")
                + f"\n\n{len(self.tried)} distinct tool calls were made.")

    def build(self):
        sys_t = self.system_text()
        shots = [dict(m) for m in self.FEWSHOT]
        if not self.no_system:
            return [{"role": "system", "content": sys_t}] + shots + self.msgs
        if shots:
            shots[0]["content"] = sys_t + "\n\n---\n\n" + shots[0]["content"]
            return shots + self.msgs
        msgs = [dict(m) for m in self.msgs]
        msgs[0]["content"] = sys_t + "\n\n---\n\n" + msgs[0]["content"]
        return msgs

    def est(self):
        return (len(self.system_text()) + sum(len(m["content"]) for m in self.FEWSHOT + self.msgs)) // 2.5

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

    # ---- araçlar ------------------------------------------------------------ #
    def trunc(self, s):
        if len(s) <= self.max_obs:
            return s
        h = self.max_obs * 2 // 3
        t = self.max_obs - h
        return (s[:h] + f"\n...[truncated {len(s) - self.max_obs} chars; narrow the query "
                f"(~grep, smaller range, single function)]...\n" + s[-t:])

    def tool(self, call):
        n, a = call["name"], call["args"]
        if n == "r2":
            cmd = str(a.get("cmd", "")).strip()
            if not cmd:
                return "error: empty cmd"
            bad = None if self.unsafe else r2_blocked(cmd)
            return f"error: {bad}" if bad else (self.r2.run(cmd) or "(no output)")
        if n == "sh":
            try:
                argv = shlex.split(str(a.get("cmd", "")))
            except ValueError as e:
                return f"error: {e}"
            if not argv or (argv[0] not in SH_ALLOW and not self.unsafe):
                return f"error: '{argv[0] if argv else ''}' not allowed. Allowed: {', '.join(sorted(SH_ALLOW))}"
            try:
                p = subprocess.run(argv, capture_output=True, timeout=60, text=True, errors="replace")
                return (p.stdout + p.stderr).strip() or "(no output)"
            except Exception as e:
                return f"error: {e}"
        if n == "note":
            t = str(a.get("text", "")).strip()
            if t:
                self.notes.append(t)
                print(col(f"  [note] {t}", "36"))
            return f"noted ({len(self.notes)} notes)"
        return f"error: unknown tool '{n}'. Use r2, sh, note or finish."

    # ---- LLM çağrısı (hata toleranslı) --------------------------------------- #
    def generate(self):
        for _ in range(4):
            self.prune()
            try:
                if TTY:
                    sys.stdout.write("\033[90m")
                try:
                    return self.llm.chat(self.build(), STOPS,
                                         lambda t: (sys.stdout.write(t), sys.stdout.flush()))
                finally:
                    if TTY:
                        sys.stdout.write("\033[0m")
                        sys.stdout.flush()
            except LLMError as e:
                b = e.body.lower()
                if e.code == 400 and "think" in b and self.llm.think is not None:
                    self.llm.think = None
                elif e.code == 400 and not self.no_system and ("system" in b or "role" in b or "template" in b):
                    print(col("\n[!] Model system rolünü desteklemiyor, user mesajına gömülüyor.", "33"))
                    self.no_system = True
                elif any(k in b for k in ("context", "exceed", "too long", "n_ctx")):
                    self.ctx = int(self.ctx * 0.7)
                    print(col(f"\n[!] Context taştı, bütçe {self.ctx} token'a düşürüldü.", "33"))
                else:
                    raise
        raise RuntimeError("LLM isteği tekrar tekrar başarısız")

    # ---- ana döngü ----------------------------------------------------------- #
    def run_goal(self, goal):
        self.add_user(goal)
        last_sig, repeats, nocall = None, 0, 0
        try:
            for step in range(1, self.max_steps + 1):
                print(col(f"\n── adım {step}/{self.max_steps} ──", "34"))
                text = re.sub(r"(</tool>\s*)+$", "", self.generate().rstrip())
                call = parse_call(text, self.TOOLS)
                stored = text + ("</tool>" if call and "<tool>" in text else "")
                if call and "<tool>" not in text:  # geçmişte doğru format görsün
                    stored = "<tool>" + json.dumps(call, ensure_ascii=False) + "</tool>"
                self.msgs.append({"role": "assistant", "content": stored})
                print()
                if not call:
                    nocall += 1
                    if nocall >= 3:
                        return text
                    self.add_user("FORMAT ERROR: no <tool> call found. Reply with ONE short plain sentence (no markdown) "
                                  "and then exactly one call like <tool>{\"name\": \"" + sorted(self.TOOLS)[0] +
                                  "\", \"args\": {}}</tool> using a real tool name and args. Use finish when done. "
                                  "Never write an OBSERVATION yourself.")
                    continue
                nocall = 0
                if call["name"] == "finish":
                    return str(call["args"].get("report", text))
                print(col(f"  ▶ {call['name']} {json.dumps(call['args'], ensure_ascii=False)}", "32"))
                sig = json.dumps(call, sort_keys=True)
                if call["name"] not in ("note", "write_file") and sig in self.seen:
                    obs = (f"BLOCKED (not executed): you already ran this exact call at step {self.seen[sig]}. "
                           "Read ALREADY TRIED and NOTES, then use a different tool, path, glob or pattern "
                           "(for example read a specific file range, or find with path=<app package dir>).")
                    repeats += 1
                    if repeats >= 5:
                        print(col("[!] ajan dongude, rapor notlardan uretiliyor", "31"))
                        return self.fallback_report()
                else:
                    obs = self.trunc(self.tool(call))
                    self.seen[sig] = step
                    self.log_try(step, call, obs)
                    repeats = 0
                shown = obs.splitlines()
                print(col("\n".join("    " + l for l in shown[:15]) +
                          (f"\n    ... (+{len(shown) - 15} satır)" if len(shown) > 15 else ""), "37"))
                warn = ("\n\n(4 steps left: record every confirmed fact with note, then call finish.)"
                        if step == self.max_steps - 4 else "")
                self.add_user(f"OBSERVATION:\n{obs}{warn}")
            self.add_user("Step limit reached. Call finish now with a report built from NOTES "
                          "(label each item VERIFIED or UNVERIFIED).")
            text = self.generate()
            call = parse_call(text, self.TOOLS)
            if call and call["name"] == "finish":
                return str(call["args"].get("report", text))
            return self.fallback_report()
        except KeyboardInterrupt:
            print(col("\n[kesildi]", "31"))
            self.add_user("[user interrupted the previous run]")
            return None


def start_server(a):
    exe = a.server_bin or shutil.which("llama-server")
    if not exe:
        sys.exit("llama-server bulunamadı: PATH'e ekle veya --server-bin ver.")
    cmd = [exe, "-m", a.model, "--port", str(a.port), "-c", str(a.ctx)] + shlex.split(a.server_args or "")
    log = tempfile.NamedTemporaryFile("w", suffix=".log", delete=False)
    print(col(f"[*] llama-server başlatılıyor (log: {log.name})", "33"))
    proc = subprocess.Popen(cmd, stdout=log, stderr=log)
    atexit.register(proc.terminate)
    url = f"http://127.0.0.1:{a.port}"
    for _ in range(600):
        if proc.poll() is not None:
            sys.exit(f"llama-server çıktı, logu incele: {log.name}")
        try:
            with urllib.request.urlopen(url + "/health", timeout=2) as r:
                if r.status == 200:
                    return url
        except Exception:
            pass
        time.sleep(1)
    sys.exit("llama-server zamanında hazır olmadı.")


class _Compat:
    Agent = Agent
    LLM = LLM
    start_server = staticmethod(start_server)
    col = staticmethod(col)
R = _Compat()


SYSTEM = r"""You are an Android APK reverse-engineering agent. MISSION: recover the device communication protocol
(Bluetooth LE GATT and/or HTTP API) used by the app @@APK@@ and produce a clean, independent, documented
open-source SDK in: @@LANG@@.

WORKSPACE
- "src": decompiled sources (read-only). Package: @@PKG@@. Source files: @@NFILES@@. Layout:
@@LAYOUT@@
- "out": your output directory (SDK, PROTOCOL.md). Use write_file to create files there.

CALL FORMAT (exactly one call per turn, then stop and wait for the observation):
<tool>{"name": "find", "args": {"pattern": "writeCharacteristic", "glob": "*.java"}}</tool>

TOOLS (args in braces; [x] = optional)
- manifest {}                                   package, permissions, services/activities, BLE features.
- ble_scan {}                                   all 128-bit UUIDs (with files) + files ranked by BLE API usage.
- http_scan {}                                  URLs and Retrofit-style endpoints with files.
- find {pattern, [path], [glob], [ignore_case=true], [files_only=false], [limit=40], [all=false]}   regex search in src.
                                                Library code (androidx, kotlin, com/google, okhttp...) is hidden unless all=true.
                                                Prefer specific patterns: UUID\.fromString, "[0-9a-f]{8}-[0-9a-f]{4}-", writeCharacteristic.
- read {file, [start=1], [end], [where="src"|"out"]}   numbered lines (max 250 per call). Paths are relative to the root.
- ls {[path], [where]}                          directory listing.
- calc {op, ...}                                op="jbytes": {"values":"-86, 1, 0x10"} -> hex (Java bytes are SIGNED).
                                                op="hex2jbytes": {"hex":"aa0110"}.
                                                op="crc": {"hex":"<frame bytes>", "expect":"<checksum hex>"} brute-forces
                                                  algorithm x byte-range x endianness; without expect prints all algos.
- note {text}                                   durable finding WITH evidence (file:line). Notes are re-shown every turn
                                                and survive context pruning and restarts. Record everything important.
- write_file {path, content, [append=false]}    write into out. Keep each file under ~120 lines; use many small files.
                                                .py files are syntax-checked automatically.
- finish {report}                               final report (what was recovered, what is UNVERIFIED, next steps).

PHASES
1 RECON: manifest, ble_scan, http_scan. Note the main package, BLE manager/callback classes, candidate UUIDs.
2 MAP: find the BluetoothGattCallback implementation; trace connect -> service discovery -> enable notifications
  (CCCD 0x2902) -> write path. Assign each UUID a role (write / notify / read). Check whether HTTP is only cloud sync.
3 FRAMING: header/magic, length, command id, payload, checksum/CRC (verify with calc crc on frames you construct),
  endianness, MTU/chunking, encryption/auth/pairing handshake, timeouts/retries.
4 COMMANDS: per feature (time sync, battery, steps, heart rate, notifications, alarms, firmware info...) record
  command id, request layout, response layout, units. One `note` per confirmed fact.
5 SPEC: write PROTOCOL.md (tables of UUIDs, framing, commands, status: VERIFIED / UNVERIFIED) BEFORE the code.
6 SDK: transport (BLE), framing/checksum, command builders + response parsers (pure functions), small CLI example,
  README with license note, tests using hex vectors taken from the source. Keep protocol logic separate from I/O.
7 finish.

RULES
1. Before each call write 1-2 sentences: what you want to learn. Never invent code, addresses or output.
2. Obfuscated names (a.b.c, m123a) are normal: infer purpose from usage and call sites; mark guesses UNVERIFIED.
3. CLEAN-ROOM: record protocol FACTS (UUIDs, byte layouts, command ids, algorithms) and re-implement them in your own
   words. Do NOT copy decompiled code, vendor strings, images or resources into the SDK.
4. NEVER hardcode vendor secrets (API keys, tokens, signing keys, passwords). If the protocol needs a per-device or
   per-account key, document how it is derived/provisioned and make it a parameter. Mention such findings only as
   "secret present at file:line" without the value.
5. If the real logic is in a native .so (src/resources/lib/*), say so in notes and the report; do not guess.
6. Keep outputs small (use find with glob/path, read ranges <= 150 lines). If output was truncated, narrow the query.
7. Reply language: same as the user's language. Code, identifiers and PROTOCOL.md in English.
8. Never mark anything VERIFIED unless the exact line appeared in an OBSERVATION. Never repeat a call listed in
   ALREADY TRIED; the system blocks it. Start with ble_scan: it lists the real UUIDs and the files that use BLE APIs.
9. FORMAT: no markdown (no headings, bold, bullet lists) in your turns: one or two plain sentences, then exactly one
   <tool>{...}</tool> call, then STOP. Never write 'Observation' yourself; the system sends it.
"""

STD_SUFFIX = "-0000-1000-8000-00805f9b34fb"
UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
BLE_APIS = ["connectGatt", "BluetoothGattCallback", "onServicesDiscovered", "setCharacteristicNotification",
            "writeDescriptor", "writeCharacteristic", "onCharacteristicChanged", "onCharacteristicWrite",
            "onCharacteristicRead", "requestMtu", "createBond", "startLeScan", "BluetoothLeScanner",
            "createRfcommSocketToServiceRecord"]
URL_RE = re.compile(r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+")
NOISE = ("schemas.android.com", "w3.org", "apache.org", "xmlpull.org", "ns.adobe.com", "developer.android.com",
         "google.com/", "googleapis.com", "github.com", "example.com", "play.google")
NOISE_PKGS = ("androidx", "kotlin", "kotlinx", "android/support", "com/google", "okhttp3", "okio", "retrofit2",
              "org/jetbrains", "org/intellij", "com/squareup", "dagger", "javax", "org/apache", "org/slf4j",
              "coil", "io/reactivex", "org/bouncycastle")
RETRO_RE = re.compile(r'@(GET|POST|PUT|DELETE|PATCH|HEAD)\(\s*"([^"]+)"')


# --------------------------------------------------------------------------- #
# CRC / checksum araçları
# --------------------------------------------------------------------------- #
def _refl(v, w):
    r = 0
    for i in range(w):
        if (v >> i) & 1:
            r |= 1 << (w - 1 - i)
    return r


def crc_generic(data, w, poly, init, refin, refout, xorout):
    top, mask, crc = 1 << (w - 1), (1 << w) - 1, init
    for b in data:
        if refin:
            b = _refl(b, 8)
        crc ^= b << (w - 8)
        for _ in range(8):
            crc = ((crc << 1) ^ poly) if crc & top else (crc << 1)
            crc &= mask
    if refout:
        crc = _refl(crc, w)
    return crc ^ xorout


def _mk(w, poly, init, ri, ro, xo):
    return (w, lambda d: crc_generic(d, w, poly, init, ri, ro, xo))


ALGOS = {
    "crc8": _mk(8, 0x07, 0, False, False, 0),
    "crc8_maxim": _mk(8, 0x31, 0, True, True, 0),
    "crc16_ccitt_false": _mk(16, 0x1021, 0xFFFF, False, False, 0),
    "crc16_xmodem": _mk(16, 0x1021, 0, False, False, 0),
    "crc16_kermit": _mk(16, 0x1021, 0, True, True, 0),
    "crc16_x25": _mk(16, 0x1021, 0xFFFF, True, True, 0xFFFF),
    "crc16_aug_ccitt": _mk(16, 0x1021, 0x1D0F, False, False, 0),
    "crc16_modbus": _mk(16, 0x8005, 0xFFFF, True, True, 0),
    "crc16_arc": _mk(16, 0x8005, 0, True, True, 0),
    "crc32": (32, lambda d: zlib.crc32(bytes(d)) & 0xFFFFFFFF),
    "sum8": (8, lambda d: sum(d) & 0xFF),
    "sum8_neg": (8, lambda d: (-sum(d)) & 0xFF),
    "sum8_inv": (8, lambda d: (~sum(d)) & 0xFF),
    "xor8": (8, lambda d: __import__("functools").reduce(lambda x, y: x ^ y, d, 0)),
    "sum16": (16, lambda d: sum(d) & 0xFFFF),
}


def to_bytes(h):
    h = re.sub(r"(0x|[^0-9a-fA-F])", "", str(h))
    if len(h) % 2:
        raise ValueError("odd number of hex digits")
    return bytes.fromhex(h)


# --------------------------------------------------------------------------- #
# Ajan
# --------------------------------------------------------------------------- #
class ApkAgent(R.Agent):
    TOOLS = {"manifest", "ble_scan", "http_scan", "find", "read", "ls", "calc", "note", "write_file", "finish"}
    FEWSHOT = [
        {"role": "user", "content": "EXAMPLE of the required turn format. Task: list the app's permissions."},
        {"role": "assistant", "content": "I will read the manifest first.\n"
                                         "<tool>{\"name\": \"manifest\", \"args\": {}}</tool>"},
        {"role": "user", "content": "OBSERVATION:\npackage=com.example.app versionName=1.0 minSdk=21\n"
                                    "permissions: BLUETOOTH, INTERNET\n\n(End of example. The real task follows. "
                                    "Always answer with one short plain sentence and exactly one <tool> call, "
                                    "no markdown.)"},
        {"role": "assistant", "content": "Understood. One short sentence and one <tool> call per turn."},
    ]

    def __init__(self, llm, apk, src, out, ctx, max_steps, max_obs, lang):
        self.llm, self.path, self.r2 = llm, apk, None
        self.src, self.out = os.path.realpath(src), os.path.realpath(out)
        self.ctx, self.max_steps, self.max_obs, self.unsafe = ctx, max_steps, max_obs, False
        self.msgs, self.no_system, self.notes = [], False, []
        self.tried, self.seen = [], {}
        os.makedirs(self.out, exist_ok=True)
        self.notes_file = os.path.join(self.out, ".notes.json")
        try:
            with open(self.notes_file, encoding="utf-8") as f:
                self.notes = json.load(f)
            print(R.col(f"[*] {len(self.notes)} önceki not yüklendi (devam modu)", "33"))
        except (OSError, ValueError):
            pass
        nfiles = sum(1 for _ in self._iter_src())
        pkg = self._manifest_pkg() or "?"
        layout = self.t_ls({"path": ""})
        self.system_base = (SYSTEM.replace("@@APK@@", os.path.basename(apk)).replace("@@LANG@@", lang)
                            .replace("@@PKG@@", pkg).replace("@@NFILES@@", str(nfiles))
                            .replace("@@LAYOUT@@", "\n".join("    " + l for l in layout.splitlines()[:20])))

    # ---- yardımcılar ------------------------------------------------------- #
    def _res(self, where, p):
        root = self.out if where == "out" else self.src
        full = os.path.realpath(os.path.join(root, str(p or "").lstrip("/")))
        if full != root and not full.startswith(root + os.sep):
            raise ValueError("path escapes workspace")
        return full

    def _iter_src(self, exts=(".java", ".kt")):
        for dp, _, fns in os.walk(self.src):
            for fn in fns:
                if fn.endswith(exts):
                    fp = os.path.join(dp, fn)
                    try:
                        if os.path.getsize(fp) > 2_000_000:
                            continue
                        with open(fp, encoding="utf-8", errors="replace") as fh:
                            yield os.path.relpath(fp, self.src), fh.read()
                    except OSError:
                        continue

    def _find_manifest(self):
        for dp, _, fns in os.walk(self.src):
            if "AndroidManifest.xml" in fns:
                return os.path.join(dp, "AndroidManifest.xml")
        return None

    def _manifest_pkg(self):
        mp = self._find_manifest()
        try:
            return ET.parse(mp).getroot().get("package") if mp else None
        except ET.ParseError:
            return None

    def _save_notes(self):
        with open(self.notes_file, "w", encoding="utf-8") as f:
            json.dump(self.notes, f, ensure_ascii=False, indent=1)

    # ---- dispatcher ---------------------------------------------------------- #
    def tool(self, call):
        n, a = call["name"], call["args"]
        fn = getattr(self, "t_" + n, None) if re.fullmatch(r"[a-z_]+", n) else None
        if not fn:
            return ("error: unknown tool '%s'. Tools: manifest, ble_scan, http_scan, find, read, ls, calc, "
                    "note, write_file, finish." % n)
        try:
            return fn(a)
        except Exception as e:  # araç hatası ajana geri bildirilir, döngüyü düşürmez
            return f"error: {type(e).__name__}: {e}"

    # ---- araçlar -------------------------------------------------------------- #
    def t_note(self, a):
        t = str(a.get("text", "")).strip()
        tagged = False
        if t and not re.search(r"\.(java|kt|xml|so)\S*:\d+", t) and "UNVERIFIED" not in t.upper():
            t, tagged = "[UNVERIFIED: no file:line] " + t, True
        if t:
            self.notes.append(t)
            self._save_notes()
            print(R.col(f"  [note] {t}", "36"))
        return f"noted ({len(self.notes)} notes)" + (
            "; stored as UNVERIFIED because it had no file:line evidence. Re-note with file:line once you read it."
            if tagged else "")

    def t_manifest(self, a):
        mp = self._find_manifest()
        if not mp:
            return "error: AndroidManifest.xml not found in src"
        ns = "{http://schemas.android.com/apk/res/android}"
        try:
            root = ET.parse(mp).getroot()
        except ET.ParseError:
            with open(mp, encoding="utf-8", errors="replace") as f:
                return f.read(3000)
        sdk = root.find("uses-sdk")
        o = [f"package={root.get('package')} versionName={root.get(ns + 'versionName')} "
             f"minSdk={sdk.get(ns + 'minSdkVersion') if sdk is not None else '?'}"]
        perms = sorted(p.get(ns + "name", "").replace("android.permission.", "")
                       for p in root.findall("uses-permission"))
        o.append("permissions: " + ", ".join(perms[:60]))
        feats = [f.get(ns + "name", "") for f in root.findall("uses-feature")]
        if feats:
            o.append("features: " + ", ".join(feats[:20]))
        app = root.find("application")
        if app is not None:
            for kind in ("service", "receiver", "provider", "activity"):
                items = []
                for e in app.findall(kind):
                    nm = e.get(ns + "name", "")
                    acts = [x.get(ns + "name", "").split(".")[-1] for x in e.iter("action")]
                    items.append(nm + (f" [{','.join(acts[:3])}]" if acts else ""))
                if items:
                    o.append(f"{kind}s ({len(items)}): " + "; ".join(items[:30 if kind != 'activity' else 12]))
        return "\n".join(o)

    def t_ls(self, a):
        p = self._res(a.get("where", "src"), a.get("path", ""))
        if not os.path.isdir(p):
            return "error: not a directory"
        rows = []
        for name in sorted(os.listdir(p)):
            fp = os.path.join(p, name)
            if os.path.isdir(fp):
                rows.append(f"{name}/  ({sum(len(f) for _, _, f in os.walk(fp))} files)")
            else:
                rows.append(f"{name}  ({os.path.getsize(fp)} B)")
        extra = f"\n... +{len(rows) - 80} more" if len(rows) > 80 else ""
        return "\n".join(rows[:80]) + extra if rows else "(empty)"

    def t_read(self, a):
        p = self._res(a.get("where", "src"), a.get("file", ""))
        if not os.path.isfile(p):
            return "error: file not found (use find/ls)"
        start = max(1, int(a.get("start", 1)))
        end = min(int(a.get("end", start + 149)), start + 249)
        out, total = [], 0
        with open(p, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, 1):
                total = i
                if start <= i <= end:
                    out.append(f"{i}: {line.rstrip()[:300]}")
        return f"{os.path.relpath(p, self.src if a.get('where') != 'out' else self.out)} " \
               f"lines {start}-{min(end, total)} of {total}\n" + "\n".join(out)

    def _noisy(self, rel):
        r = "/" + rel.replace("\\", "/").strip("/") + "/"
        return any(f"/{p}/" in r for p in NOISE_PKGS)

    def _py_find(self, base, pat, ic, fo, glob, skip_noise=True):
        rx, lines = re.compile(pat, re.I if ic else 0), []
        for dp, dirs, fns in os.walk(base):
            if skip_noise:
                dirs[:] = [d for d in dirs if not self._noisy(os.path.relpath(os.path.join(dp, d), self.src))]
            for fn in fns:
                if glob and not fnmatch.fnmatch(fn, glob):
                    continue
                fp = os.path.join(dp, fn)
                try:
                    with open(fp, encoding="utf-8", errors="replace") as fh:
                        for i, l in enumerate(fh, 1):
                            if rx.search(l):
                                lines.append(fp if fo else f"{fp}:{i}:{l.strip()}")
                                if fo:
                                    break
                except OSError:
                    continue
            if len(lines) > 20000:
                break
        return lines

    def _fmt_find(self, lines, limit, fo, all_):
        pre = self.src + os.sep
        rows = [l.replace(pre, "") for l in lines]
        dropped = 0
        if not all_:
            keep = [r for r in rows if not self._noisy(os.path.dirname(r if fo else r.split(":", 1)[0]))]
            dropped, rows = len(rows) - len(keep), keep
        note = f" ({dropped} library-noise matches hidden; pass all=true to include them)" if dropped else ""
        if not rows:
            return "no matches" + note
        if fo:
            return f"{len(rows)} files{note}\n" + "\n".join(r[:200] for r in rows[:limit])
        by = {}
        for r in rows:
            m = re.match(r"^(.*?):(\d+):(.*)$", r)
            if m:
                by.setdefault(m.group(1), []).append((m.group(2), m.group(3).strip()))
        out, n = [], 0
        for f, hits in by.items():  # her dosyadan en fazla 3 satir: cesitlilik > tekrar
            if n >= limit:
                out.append(f"... {len(by) - sum(1 for l in out if not l.startswith('  '))} more files; "
                           "narrow with path/glob or a more specific pattern")
                break
            out.append(f"{f}  ({len(hits)} hits)")
            for ln, tx in hits[:3]:
                out.append(f"  {ln}: {tx[:170]}")
                n += 1
        return f"{len(rows)} matches in {len(by)} files{note}\n" + "\n".join(out)

    def t_find(self, a):
        pat = str(a.get("pattern", ""))
        if not pat:
            return "error: empty pattern"
        base = self._res("src", a.get("path", ""))
        ic, fo, glob = a.get("ignore_case", True), a.get("files_only", False), a.get("glob")
        all_ = bool(a.get("all", False))
        limit = min(int(a.get("limit", 40)), 100)
        if not shutil.which("rg") and not shutil.which("grep"):  # Windows: saf Python yedek arama
            return self._fmt_find(self._py_find(base, pat, ic, fo, glob, not all_), limit, fo, all_)
        if shutil.which("rg"):
            cmd = ["rg", "-n", "--no-heading", "-I"] + (["-i"] if ic else []) + (["-l"] if fo else []) \
                  + (["-g", glob] if glob else []) + ["-e", pat, base]
        else:
            cmd = ["grep", "-rnIE"] + (["-i"] if ic else []) + (["-l"] if fo else []) \
                  + ([f"--include={glob}"] if glob else []) + ["-e", pat, base]
        p = subprocess.run(cmd, capture_output=True, timeout=90)
        lines = p.stdout.decode("utf-8", "replace").splitlines()
        if p.returncode not in (0, 1):
            return "error: " + p.stderr.decode("utf-8", "replace")[:300]
        return self._fmt_find(lines, limit, fo, all_)

    def t_ble_scan(self, a):
        uuids, files = defaultdict(set), {}
        for rel, txt in self._iter_src():
            for u in UUID_RE.findall(txt):
                uuids[u.lower()].add(rel)
            hits = {k: txt.count(k) for k in BLE_APIS if k in txt}
            if hits:
                files[rel] = hits
        o = [f"128-bit UUIDs found: {len(uuids)}"]
        for u, fs in sorted(uuids.items(), key=lambda x: -len(x[1]))[:40]:
            tag = f" [SIG 0x{u[4:8]}]" if u.endswith(STD_SUFFIX) else ""
            o.append(f"  {u}{tag}  in {len(fs)} file(s): {', '.join(sorted(fs)[:3])}")
        o.append("\nFiles by BLE API usage:")
        rank = sorted(files.items(), key=lambda x: (-len(x[1]), -sum(x[1].values())))[:12]
        for rel, h in rank:
            o.append(f"  {rel}: " + " ".join(f"{k}={v}" for k, v in h.items()))
        if not files and not uuids:
            o.append("  nothing found - logic may be in native code, Kotlin/obfuscated strings, or a vendor SDK; "
                     "try find with 'Characteristic|Gatt|bluetooth'.")
        return "\n".join(o)

    def t_http_scan(self, a):
        urls, eps = defaultdict(set), defaultdict(set)
        for rel, txt in self._iter_src():
            for u in URL_RE.findall(txt):
                u = u.rstrip(".,;)\"'")
                if not any(n in u for n in NOISE):
                    urls[u].add(rel)
            for verb, path in RETRO_RE.findall(txt):
                eps[f"{verb} {path}"].add(rel)
        o = [f"URLs: {len(urls)}"]
        o += [f"  {u}  ({sorted(f)[0]})" for u, f in sorted(urls.items())[:40]]
        o.append(f"Retrofit-style endpoints: {len(eps)}")
        o += [f"  {e}  ({sorted(f)[0]})" for e, f in sorted(eps.items())[:60]]
        return "\n".join(o)

    def t_calc(self, a):
        op = a.get("op")
        if op == "jbytes":
            toks = [t for t in re.split(r"[,\s{}();]+", str(a.get("values", "")).replace("(byte)", "")) if t]
            return bytes(int(t, 0) & 0xFF for t in toks).hex()
        if op == "hex2jbytes":
            return ", ".join(str(b - 256 if b > 127 else b) for b in to_bytes(a.get("hex", "")))
        if op != "crc":
            return "error: op must be jbytes | hex2jbytes | crc"
        data = to_bytes(a.get("hex", ""))
        if not a.get("expect"):
            return "\n".join(f"{n:18s} {v:0{w // 4}x}" for n, (w, f) in ALGOS.items() for v in [f(data)])
        exp = to_bytes(a["expect"])
        width, val = len(exp) * 8, int.from_bytes(exp, "big")
        swapped = int.from_bytes(exp, "little")
        hits = []
        for name, (w, f) in ALGOS.items():
            if w != width:
                continue
            for i in range(len(data)):
                for j in range(i + 1, len(data) + 1):
                    r = f(data[i:j])
                    for endian, target in (("big", val), ("little", swapped)):
                        if r == target:
                            hits.append(f"{name} over bytes[{i}:{j}] ({endian}-endian in frame)")
                    if len(hits) >= 12:
                        break
        return "\n".join(dict.fromkeys(hits)) or "no match (try other byte ranges, seeds, or custom polynomial)"

    def t_write_file(self, a):
        rel = str(a.get("path", "")).strip()
        content = str(a.get("content", ""))
        if not rel or len(content) > 80_000:
            return "error: need path and content (<80KB)"
        p = self._res("out", rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a" if a.get("append") else "w", encoding="utf-8") as f:
            f.write(content)
        msg = f"wrote {len(content)} chars to out/{rel}"
        if rel.endswith(".py"):
            try:
                with open(p, encoding="utf-8") as f:
                    compile(f.read(), rel, "exec")
                msg += " (python syntax OK)"
            except SyntaxError as e:
                msg += f"\nSYNTAX ERROR line {e.lineno}: {e.msg} - fix it with write_file again"
        return msg


def _has_sources(d):
    for _, _, fns in os.walk(d):
        if any(f.endswith((".java", ".kt")) for f in fns):
            return True
    return False


def decompile(apk, work, extra):
    dst = os.path.join(work, "jadx")
    marker = os.path.join(work, ".jadx_ok")
    if os.path.isfile(marker) and _has_sources(dst):
        print(R.col(f"[*] mevcut decompile kullanılıyor: {dst}", "33"))
        return dst
    if os.path.isdir(dst):
        print(R.col("[!] önceki decompile yarım/bozuk, siliniyor...", "33"))
        shutil.rmtree(dst, ignore_errors=True)
    jadx = shutil.which("jadx")
    if not jadx:
        sys.exit("jadx bulunamadı. PATH'e ekle ya da başka yerde decompile edip --src-dir ver.")
    print(R.col("[*] jadx çalışıyor (büyük APK'larda dakikalar sürebilir)...", "33"))
    p = subprocess.Popen([jadx, "-d", dst, "--show-bad-code"] + shlex.split(extra or "") + [apk],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    log = []
    for line in p.stdout:  # hata kodu güvenilmez (jadx küçük hatalarda da 1 döner), çıktıya bakılır
        print(line, end="")
        log.append(line)
    p.wait()
    text = "".join(log).lower()
    crashed = any(k in text for k in ("outofmemoryerror", "insufficient memory", "hs_err_pid", "errno=1455"))
    if crashed or not _has_sources(dst):
        shutil.rmtree(dst, ignore_errors=True)
        sys.exit("\n[!] jadx başarısız (bellek yetersiz olabilir). llama-server'ı kapatıp tekrar dene; "
                 "gerekirse --jadx-args \"-j 1\" ver ve Windows sanal belleği (pagefile) artır.")
    open(marker, "w").close()
    return dst


def main():
    ap = argparse.ArgumentParser(description="JADX APK reverse-engineering agent (Ollama / llama.cpp)")
    ap.add_argument("apk")
    ap.add_argument("-g", "--goal")
    ap.add_argument("--backend", choices=("ollama", "llama"), default="ollama")
    ap.add_argument("-u", "--url", help="backend URL; Ollama default http://127.0.0.1:11434")
    ap.add_argument("-m", "--model", help="GGUF for llama backend")
    ap.add_argument("--server-bin")
    ap.add_argument("--server-args")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--ctx", type=int, default=32768)
    ap.add_argument("--api-key")
    ap.add_argument("--model-name", default="qwen3.5:latest")
    ap.add_argument("--temp", type=float, default=0.2)
    ap.add_argument("--max-tokens", type=int, default=1500)
    ap.add_argument("--max-steps", type=int, default=60)
    ap.add_argument("--max-obs", type=int, default=5000)
    ap.add_argument("--work")
    ap.add_argument("--out-dir")
    ap.add_argument("--src-dir", help="hazir JADX kaynak klasoru")
    ap.add_argument("--jadx-args", help='or: "-j 2"')
    ap.add_argument("--think", action="store_true", help="ollama: modelin thinking modunu ac (varsayilan kapali)")
    ap.add_argument("--decompile-only", action="store_true")
    ap.add_argument("--lang", default="Python 3 (bleak for BLE; minimal dependencies; pytest tests)")
    a = ap.parse_args()
    if not os.path.isfile(a.apk) and not a.src_dir: sys.exit(f"Dosya yok: {a.apk}")
    work = a.work or os.path.splitext(os.path.abspath(a.apk))[0] + "_work"
    os.makedirs(work, exist_ok=True)
    src = os.path.abspath(a.src_dir) if a.src_dir else decompile(os.path.abspath(a.apk), work, a.jadx_args)
    if not _has_sources(src): sys.exit(f"JADX kaynaklari bulunamadi: {src}")
    out = os.path.abspath(a.out_dir or os.path.join(work, "sdk_out"))
    if a.decompile_only:
        print(col(f"[*] decompile hazir: {src}", "32")); return
    if a.backend == "ollama":
        if a.model: sys.exit("--backend ollama ile -m/--model kullanilmaz; --model-name kullan.")
        url = (a.url or "http://127.0.0.1:11434").rstrip("/")
        model_name = a.model_name
        print(col(f"[*] Ollama: {url} model={model_name} ctx={a.ctx}", "33"))
    else:
        url = start_server(a) if a.model else (a.url or "http://127.0.0.1:8080")
        model_name = a.model_name or None
    llm = LLM(url, a.temp, a.max_tokens, a.api_key, model_name, a.backend, a.ctx)
    if a.backend == "ollama":
        llm.think = bool(a.think)
    ctx = llm.n_ctx() or a.ctx
    print(col(f"[*] source={src}", "33"))
    print(col(f"[*] context={ctx} max_tokens={a.max_tokens}", "33"))
    agent = ApkAgent(llm, a.apk, src, out, ctx, a.max_steps, a.max_obs, a.lang)
    default_goal = ("Analyze this APK. Recover the device communication protocol (BLE GATT and any HTTP API) and "
                    "build the documented open-source SDK in the out directory, following your phases.")
    def handle(goal):
        rep = agent.run_goal(goal)
        if rep:
            print(col("\n══════ RAPOR ══════", "35") + f"\n{rep}\n")
            with open(os.path.join(agent.out, "REPORT.md"), "a", encoding="utf-8") as f: f.write(f"## {goal}\n\n{rep}\n\n")
    if a.goal: handle(a.goal); return
    print(f"Bos satir/'go' = tam analiz. Komutlar: /notes /reset /files /find <regex> /ble /quit\nCikti: {agent.out}")
    while True:
        try: line = input(col("apk> ", "35")).strip()
        except (EOFError, KeyboardInterrupt): print(); break
        if line in ("/quit", "/q"): break
        if line == "/notes": print("\n".join(f"- {n}" for n in agent.notes) or "(bos)")
        elif line == "/reset": agent.msgs.clear(); print("konusma sifirlandi (notlar duruyor)")
        elif line == "/files": print(agent.t_ls({"where":"out"}))
        elif line == "/ble": print(agent.t_ble_scan({}))
        elif line.startswith("/find "): print(agent.t_find({"pattern":line[6:]}))
        else: handle(default_goal if line in ("", "go") else line)

if __name__ == "__main__": main()

