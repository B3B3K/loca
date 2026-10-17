#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agent_core.py — Ortak altyapi: LLM istemcisi, parse_call, BaseAgent, start_server.

Tum agent scriptleri bu modulu import eder:
    from agent_core import LLM, LLMError, BaseAgent, parse_call, BLOCK_RE, col, TTY, start_server
"""
import atexit, difflib, fnmatch, json, os, re, shlex, shutil, subprocess, sys, tempfile, threading, time
import urllib.error, urllib.request

# --------------------------------------------------------------------------- #
# Terminal renk yardimcilari
# --------------------------------------------------------------------------- #
if os.name == "nt":
    os.system("")

TTY = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
STOPS = ["OBSERVATION", "Observation:", "**Observation"]


def col(s, code):
    return f"\033[{code}m{s}\033[0m" if TTY else s


# --------------------------------------------------------------------------- #
# LLM istemcisi  (Ollama + llama.cpp, thinking destekli)
# --------------------------------------------------------------------------- #
class LLMError(Exception):
    def __init__(self, code, body):
        super().__init__(f"HTTP {code}: {body[:300]}")
        self.code, self.body = code, body


class LLM:
    def __init__(self, url, temperature, max_tokens,
                 api_key=None, model=None, backend="ollama", ctx=32768):
        self.url = url.rstrip("/")
        self.temperature, self.max_tokens = temperature, max_tokens
        self.api_key, self.model, self.backend, self.ctx = api_key, model, backend, ctx
        self.think     = None   # True/False: thinking ac/kapat; None: parametre gonderme
        self.last_done = ""     # "length" => token butcesi doldu
        self.last_think = 0     # son cevapta thinking karakter sayisi

    @property
    def endpoint(self):
        return self.url + ("/api/chat" if self.backend == "ollama" else "/v1/chat/completions")

    def _headers(self):
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def n_ctx(self):
        """Aktif context boyutunu sorgula. Ollama'da yapilandirilan deger doner."""
        if self.backend == "ollama":
            return self.ctx
        try:
            req = urllib.request.Request(self.url + "/props", headers=self._headers())
            with urllib.request.urlopen(req, timeout=5) as r:
                return int(json.load(r)["default_generation_settings"]["n_ctx"])
        except Exception:
            return None

    def chat(self, messages, stop, on_token):
        """Streaming chat. on_token(text, is_thinking). Yalnizca cevap metnini dondurur."""
        if self.backend == "ollama":
            body = {
                "model": self.model, "messages": messages, "stream": True,
                "keep_alive": "30m",
                "options": {
                    "temperature": self.temperature,
                    "num_predict": self.max_tokens,
                    "num_ctx": self.ctx,
                    "stop": stop,
                },
            }
            if self.think is not None:
                body["think"] = self.think
        else:
            body = {
                "messages": messages, "stream": True,
                "temperature": self.temperature, "max_tokens": self.max_tokens,
                "stop": stop, "cache_prompt": True,
            }
            if self.model:
                body["model"] = self.model
            if self.think is not None:
                body["chat_template_kwargs"] = {"enable_thinking": self.think}

        req = urllib.request.Request(self.endpoint, json.dumps(body).encode(), self._headers())
        try:
            resp = urllib.request.urlopen(req, timeout=900)
        except urllib.error.HTTPError as e:
            raise LLMError(e.code, e.read().decode("utf-8", "replace"))

        parts, self.last_done, self.last_think = [], "", 0
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            if self.backend == "ollama":
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                m = d.get("message") or {}
                th  = m.get("thinking") or ""
                tok = m.get("content")  or ""
                if d.get("done"):
                    self.last_done = d.get("done_reason") or "stop"
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
                delta = ch[0].get("delta") or {}
                th  = delta.get("reasoning_content") or ""
                tok = delta.get("content") or ""
                if ch[0].get("finish_reason"):
                    self.last_done = ch[0]["finish_reason"]
            if th:
                self.last_think += len(th)
                on_token(th, True)
            if tok:
                parts.append(tok)
                on_token(tok, False)
            if self.backend == "ollama" and self.last_done:
                break
        return "".join(parts)


# --------------------------------------------------------------------------- #
# Tool call ayrıstırma:  <tool>{json}</tool>  +  <old>/<new>/<content> bloklari
# --------------------------------------------------------------------------- #
BLOCK_RE = re.compile(
    r"<(old|new|content)>[ \t]*\r?\n?(.*?)(?:\r?\n?[ \t]*</\1>|\Z)", re.S
)


def parse_call(text, names=None):
    """LLM cevabindan ilk gecerli arac cagrisini ayristir."""
    dec = json.JSONDecoder(strict=False)
    m = re.search(r"<tool>", text)
    cands = ([(m.end(), True)] if m else []) + [(x.start(), False) for x in re.finditer(r"\{", text)]
    for s, trusted in cands:
        i = text.find("{", s)
        if i < 0:
            continue
        try:
            obj, end = dec.raw_decode(text[i:])
        except ValueError:
            continue
        if not (isinstance(obj, dict) and "name" in obj):
            continue
        if not trusted and names and obj["name"] not in names:
            continue
        args = obj.get("args") or obj.get("arguments") or obj.get("parameters") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        args = dict(args) if isinstance(args, dict) else {}
        pos = i + end
        close = re.match(r"\s*</tool>", text[pos:])
        if close:
            pos += close.end()
        for bm in BLOCK_RE.finditer(text, pos):
            args.setdefault(bm.group(1), bm.group(2))
            pos = max(pos, bm.end())
        return {"name": str(obj["name"]), "args": args, "end": pos, "tagged": m is not None}
    # Zayif modeller `tool {json}` seklinde backtick icerisinde yazar
    if names:
        for mm in re.finditer(r"`\s*([a-z_0-9]+)\s*(\{.*?\})?\s*`", text, re.S):
            if mm.group(1) in names:
                try:
                    args = json.loads(mm.group(2), strict=False) if mm.group(2) else {}
                except ValueError:
                    continue
                if isinstance(args, dict):
                    return {"name": mm.group(1), "args": args, "end": mm.end(), "tagged": False}
    return None


# --------------------------------------------------------------------------- #
# llama.cpp sunucusu (opsiyonel)
# --------------------------------------------------------------------------- #
def start_server(a):
    """argparse Namespace'inden llama-server baslatir ve URL dondurur."""
    exe = a.server_bin or shutil.which("llama-server")
    if not exe:
        sys.exit("llama-server bulunamadi: PATH'e ekle veya --server-bin ver.")
    cmd = [exe, "-m", a.model, "--port", str(a.port), "-c", str(a.ctx)] + shlex.split(a.server_args or "")
    log = tempfile.NamedTemporaryFile("w", suffix=".log", delete=False)
    print(col(f"[*] llama-server baslatiliyor (log: {log.name})", "33"))
    proc = subprocess.Popen(cmd, stdout=log, stderr=log)
    atexit.register(proc.terminate)
    url = f"http://127.0.0.1:{a.port}"
    for _ in range(600):
        if proc.poll() is not None:
            sys.exit(f"llama-server cikti, logu incele: {log.name}")
        try:
            with urllib.request.urlopen(url + "/health", timeout=2) as r:
                if r.status == 200:
                    return url
        except Exception:
            pass
        time.sleep(1)
    sys.exit("llama-server zamaninda hazir olmadi.")


# --------------------------------------------------------------------------- #
# CLI arguman yardimcisi  (her agent kendi parser'ini kurar, ortak flag'leri
# add_common_args() ile ekler)
# --------------------------------------------------------------------------- #
def add_common_args(ap, default_temp=0.4, default_max_tokens=8192, default_max_steps=40):
    """Her agent'in argparse.ArgumentParser'ina ortak flag'leri ekler."""
    ap.add_argument("--backend",    choices=("ollama", "llama"), default="ollama")
    ap.add_argument("-u", "--url",  help="backend URL; Ollama default http://127.0.0.1:11434")
    ap.add_argument("-m", "--model", help="GGUF yolu (llama backend)")
    ap.add_argument("--server-bin")
    ap.add_argument("--server-args")
    ap.add_argument("--port",       type=int, default=8080)
    ap.add_argument("--ctx",        type=int, default=32768)
    ap.add_argument("--api-key")
    ap.add_argument("--model-name", default="qwen3.5:latest")
    ap.add_argument("--temp",       type=float, default=default_temp)
    ap.add_argument("--max-tokens", type=int,   default=default_max_tokens)
    ap.add_argument("--max-steps",  type=int,   default=default_max_steps)
    ap.add_argument("--max-obs",    type=int,   default=5000)
    ap.add_argument("--no-think",   action="store_true", help="thinking kapali")
    return ap


def build_llm_from_args(a):
    """Parsed args'tan (LLM, url, model_name) uclusu olusturur."""
    if a.backend == "ollama":
        if getattr(a, "model", None):
            sys.exit("--backend ollama ile -m kullanilmaz; --model-name kullan.")
        url, model_name = (a.url or "http://127.0.0.1:11434").rstrip("/"), a.model_name
    else:
        url = start_server(a) if getattr(a, "model", None) else (a.url or "http://127.0.0.1:8080")
        model_name = a.model_name or None
    llm = LLM(url, a.temp, a.max_tokens, a.api_key, model_name, a.backend, a.ctx)
    llm.think = not a.no_think
    return llm, url, model_name


# --------------------------------------------------------------------------- #
# BaseAgent  —  her agent icin ortak dongu + dosya islem altyapisi
# --------------------------------------------------------------------------- #
# Bu sabitler subclass'lar tarafindan override edilir
_DEFAULT_MUTATORS = {"edit_file", "write_file", "delete_file", "undo"}


class BaseAgent:
    """
    Ortak: mesaj yonetimi, prune, generate, run_goal, dosya CRUD (edit/write/delete/undo/diff),
           t_note, t_ls, t_read, t_find.

    Her subclass tanimlamali:
        TOOLS: set[str]
        SYSTEM: str  (veya system_base'i __init__'te kendiniz set edin)
        FEWSHOT: list[dict]
        SKIP_DIRS: set[str]
        TEXT_EXTS: tuple[str]
        BIN_EXTS: tuple[str]
        NO_WRITE_NAMES: set[str]
        MUTATORS: set[str]   (default: edit_file/write_file/delete_file/undo)
    """
    TOOLS:          set   = set()
    FEWSHOT:        list  = []
    SKIP_DIRS:      set   = {".git", ".agent", "node_modules", "__pycache__"}
    TEXT_EXTS:      tuple = (".txt", ".md", ".json", ".yaml", ".yml", ".sh")
    BIN_EXTS:       tuple = (".bin", ".zip", ".tar", ".gz")
    NO_WRITE_NAMES: set   = set()
    MUTATORS:       set   = frozenset(_DEFAULT_MUTATORS)

    def __init__(self, llm, root, state, ctx, max_steps, max_obs):
        self.llm  = llm
        self.root = os.path.realpath(root)
        self.state = os.path.realpath(state)
        self.ctx, self.max_steps, self.max_obs = ctx, max_steps, max_obs
        self.msgs, self.no_system, self.notes = [], False, []
        self.tried, self.seen, self.originals = [], {}, {}
        self.dirty        = False
        self.finish_warned = False
        self.system_base  = ""   # subclass __init__'te set edilir
        os.makedirs(self.state, exist_ok=True)
        self.backup_dir  = os.path.join(self.state, "backups", time.strftime("%Y%m%d-%H%M%S"))
        self.notes_file  = os.path.join(self.state, "notes.json")
        self._git_exclude()
        try:
            with open(self.notes_file, encoding="utf-8") as f:
                self.notes = json.load(f)
            print(col(f"[*] {len(self.notes)} onceki not yuklendi (devam modu)", "33"))
        except (OSError, ValueError):
            pass

    # ------------------------------------------------------------------ #
    # Git / yol yardimcilari
    # ------------------------------------------------------------------ #
    def _git_exclude(self):
        ex = os.path.join(self.root, ".git", "info", "exclude")
        try:
            if os.path.isdir(os.path.dirname(ex)):
                cur = open(ex, encoding="utf-8").read() if os.path.exists(ex) else ""
                if ".agent/" not in cur:
                    with open(ex, "a", encoding="utf-8") as f:
                        f.write("\n.agent/\n")
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
            if any(x in self.SKIP_DIRS for x in parts):
                raise ValueError(f"writing to '{rel}' is not allowed (generated/internal dir)")
            if parts[-1] in self.NO_WRITE_NAMES or rel.lower().endswith(self.BIN_EXTS):
                raise ValueError(f"'{rel}' is protected or binary; not writable")
        return full

    def _walk(self, base, glob=None):
        if os.path.isfile(base):
            yield base
            return
        for dp, dirs, fns in os.walk(base):
            dirs[:] = sorted(d for d in dirs if d not in self.SKIP_DIRS)
            for fn in sorted(fns):
                if glob:
                    if not fnmatch.fnmatch(fn, glob):
                        continue
                elif not fn.lower().endswith(self.TEXT_EXTS) and fn.lower() not in self.TEXT_EXTS:
                    continue
                fp = os.path.join(dp, fn)
                try:
                    if os.path.getsize(fp) <= 1_500_000:
                        yield fp
                except OSError:
                    continue

    def _file(self, rel, write=False):
        """Yol coz; yoksa tekil dosya adiyla eslestir."""
        p = self._res(rel, write)
        if os.path.isfile(p):
            return p
        base = os.path.basename(str(rel).replace("\\", "/"))
        hits = [f for f in self._walk(self.root, base) if os.path.basename(f) == base] if base else []
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise ValueError("ambiguous file name, use one of: " + ", ".join(self._rel(h) for h in hits[:6]))
        raise ValueError(f"file not found: {rel} (use find with files_only=true)")

    def _rel(self, full):
        return os.path.relpath(full, self.root).replace(os.sep, "/")

    def _save_notes(self):
        with open(self.notes_file, "w", encoding="utf-8") as f:
            json.dump(self.notes, f, ensure_ascii=False, indent=1)

    def _backup(self, full):
        rel = self._rel(full)
        if rel in self.originals:
            return
        if os.path.exists(full):
            dst = os.path.join(self.backup_dir, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(full, dst)
            self.originals[rel] = dst
        else:
            self.originals[rel] = None

    # ------------------------------------------------------------------ #
    # Mesaj yonetimi
    # ------------------------------------------------------------------ #
    def add_user(self, text):
        if self.msgs and self.msgs[-1]["role"] == "user":
            self.msgs[-1]["content"] += "\n\n" + text
        else:
            self.msgs.append({"role": "user", "content": text})

    def system_text(self):
        s = self.system_base
        if self.notes:
            s += "\nNOTES SO FAR:\n" + "\n".join(f"- {n}" for n in self.notes[-40:])
        if self.originals:
            s += "\nFILES CHANGED THIS SESSION: " + ", ".join(sorted(self.originals))
        if self.tried:
            s += "\nALREADY TRIED (never repeat):\n" + "\n".join(self.tried[-20:])
        return s

    def log_try(self, step, call, obs):
        a = json.dumps(
            {k: (v if len(str(v)) < 60 else "...") for k, v in call["args"].items()},
            ensure_ascii=False,
        )
        self.tried.append(f"s{step} {call['name']} {a[:80]} -> {(obs.splitlines() or [''])[0][:60]}")

    def fallback_report(self):
        return (
            "(otomatik: ajan finish cagirmadi)\nDegisen dosyalar: "
            + (", ".join(sorted(self.originals)) or "yok")
            + "\nNotlar:\n"
            + ("\n".join(f"- {n}" for n in self.notes) or "(yok)")
        )

    def build_msgs(self):
        sys_t = self.system_text()
        shots = [dict(m) for m in self.FEWSHOT]
        if not self.no_system:
            return [{"role": "system", "content": sys_t}] + shots + self.msgs
        shots[0]["content"] = sys_t + "\n\n---\n\n" + shots[0]["content"]
        return shots + self.msgs

    def est(self):
        return (len(self.system_text()) + sum(len(m["content"]) for m in self.FEWSHOT + self.msgs)) // 2.5

    def prune(self):
        budget = max(self.ctx // 2, self.ctx - self.llm.max_tokens - 300)
        while self.est() > budget:
            for m in self.msgs[:-6]:
                if (m["role"] == "assistant" and "[code elided]" not in m["content"]
                        and BLOCK_RE.search(m["content"])):
                    m["content"] = BLOCK_RE.sub(
                        lambda x: f"<{x.group(1)}>[code elided]</{x.group(1)}>", m["content"]
                    )
                    break
            else:
                for m in self.msgs[1:]:
                    if (m["role"] == "user" and m["content"].startswith("OBSERVATION")
                            and "elided" not in m["content"][:80]):
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
        return s[:h] + f"\n...[truncated {len(s) - self.max_obs} chars; narrow the query]...\n" + s[-(self.max_obs - h):]

    # ------------------------------------------------------------------ #
    # LLM cagrisi
    # ------------------------------------------------------------------ #
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
                    print(col("\n[!] Model 'think' parametresini desteklemiyor, kapatildi.", "33"))
                    self.llm.think = None
                elif e.code == 400 and not self.no_system and ("system" in b or "role" in b or "template" in b):
                    print(col("\n[!] system rolu desteklenmiyor, user mesajina gomuluyor.", "33"))
                    self.no_system = True
                elif any(k in b for k in ("context", "exceed", "too long", "n_ctx")):
                    self.ctx = int(self.ctx * 0.7)
                    print(col(f"\n[!] Context tasti, butce {self.ctx} token'a dusuruldu.", "33"))
                else:
                    raise
        raise RuntimeError("LLM istegi tekrar tekrar basarisiz")

    # ------------------------------------------------------------------ #
    # Tool dispatcher
    # ------------------------------------------------------------------ #
    def tool(self, call):
        n, a = call["name"], call["args"]
        fn = getattr(self, "t_" + n, None) if re.fullmatch(r"[a-z_]+", n) and n in self.TOOLS else None
        if not fn:
            return f"error: unknown tool '{n}'. Tools: {', '.join(sorted(self.TOOLS))}."
        try:
            return fn(a)
        except Exception as e:
            return f"error: {type(e).__name__}: {e}"

    @staticmethod
    def _brief(args):
        return json.dumps(
            {k: (v if len(str(v)) < 90 else f"<{len(str(v))} chars>") for k, v in args.items()},
            ensure_ascii=False,
        )

    # ------------------------------------------------------------------ #
    # Ana dongu
    # ------------------------------------------------------------------ #
    def run_goal(self, goal):
        self.add_user(goal)
        repeats = nocall = 0
        try:
            for step in range(1, self.max_steps + 1):
                print(col(f"\n== adim {step}/{self.max_steps} ==", "34"))
                raw  = self.generate()
                text = re.sub(r"<think>.*?(?:</think>|\Z)", "", raw, flags=re.S).strip()
                call = parse_call(text, self.TOOLS)
                if call:
                    stored = text[:call["end"]] if call["tagged"] else (
                        "<tool>" + json.dumps({"name": call["name"], "args": call["args"]}, ensure_ascii=False) + "</tool>"
                    )
                    if call["tagged"] and "</tool>" not in stored:
                        stored = (
                            re.sub(r"(\})\s*$", r"\1</tool>", stored, count=1)
                            if not BLOCK_RE.search(stored) else stored
                        )
                else:
                    stored = text or "(empty)"
                self.msgs.append({"role": "assistant", "content": stored})
                print()

                if not call:
                    nocall += 1
                    if nocall >= 3:
                        return self.fallback_report()
                    if self.llm.last_done == "length":
                        print(col("[!] max-tokens doldu: --max-tokens artir.", "31"))
                        self.add_user(
                            "Your answer was cut off. Think much less and reply NOW with one short sentence "
                            "and exactly one <tool> call."
                        )
                    else:
                        self.add_user(
                            "FORMAT ERROR: no <tool> call found. Reply with one short plain sentence and "
                            f"exactly one call, e.g. <tool>{{\"name\": \"read\", \"args\": {{\"file\": \"...\"}}}}.</tool>. "
                            "Use finish when done. Never write an OBSERVATION yourself."
                        )
                    continue
                nocall = 0

                if call["name"] == "finish":
                    return str(call["args"].get("report", text))

                print(col(f"  > {call['name']} {self._brief(call['args'])}", "32"))
                sig = json.dumps({k: v for k, v in call.items() if k in ("name", "args")}, sort_keys=True)
                if call["name"] not in ({"note"} | self.MUTATORS) and sig in self.seen:
                    obs = (
                        f"BLOCKED (not executed): identical call already ran at step {self.seen[sig]}. "
                        "Read ALREADY TRIED and NOTES, then use a different tool or pattern."
                    )
                    repeats += 1
                    if repeats >= 5:
                        print(col("[!] ajan dongude, rapor notlardan uretiliyor", "31"))
                        return self.fallback_report()
                else:
                    obs = self.trunc(self.tool(call))
                    self.seen[sig] = step
                    self.log_try(step, call, obs)
                    repeats = 0
                    if call["name"] in self.MUTATORS and not obs.startswith("error"):
                        self.seen.clear()

                shown = obs.splitlines()
                print(col(
                    "\n".join("    " + l for l in shown[:15])
                    + (f"\n    ... (+{len(shown) - 15} satir)" if len(shown) > 15 else ""),
                    "37",
                ))
                warn = (
                    "\n\n(4 steps left: note confirmed facts, then call finish.)"
                    if step == self.max_steps - 4 else ""
                )
                self.add_user(f"OBSERVATION:\n{obs}{warn}")

            self.add_user("Step limit reached. Call finish now.")
            call = parse_call(self.generate(), self.TOOLS)
            return str(call["args"].get("report", "")) if call and call["name"] == "finish" else self.fallback_report()
        except KeyboardInterrupt:
            print(col("\n[kesildi]", "31"))
            self.add_user("[user interrupted the previous run]")
            return None

    # ------------------------------------------------------------------ #
    # Ortak araclar  (her agent tarafindan kullanilir)
    # ------------------------------------------------------------------ #
    def t_note(self, a):
        t = str(a.get("text", "")).strip()
        if t:
            self.notes.append(t)
            self._save_notes()
            print(col(f"  [note] {t}", "36"))
        return f"noted ({len(self.notes)} notes)"

    def t_ls(self, a):
        p = self._res(a.get("path", ""))
        if not os.path.isdir(p):
            return "error: not a directory"
        rows = []
        for name in sorted(os.listdir(p)):
            if name in self.SKIP_DIRS:
                continue
            fp = os.path.join(p, name)
            rows.append(
                f"{name}/  ({sum(1 for _ in self._walk(fp, '*'))} files)"
                if os.path.isdir(fp) else f"{name}  ({os.path.getsize(fp)} B)"
            )
        return ("\n".join(rows[:80]) + (f"\n... +{len(rows) - 80} more" if len(rows) > 80 else "")) if rows else "(empty)"

    def t_read(self, a):
        p = self._file(a.get("file") or a.get("path", ""))
        start = max(1, int(a.get("start", 1)))
        end   = min(int(a.get("end", start + 149)), start + 249)
        out, total = [], 0
        with open(p, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, 1):
                total = i
                if start <= i <= end:
                    out.append(f"{i}: {line.rstrip()[:300]}")
        return f"{self._rel(p)} lines {start}-{min(end, total)} of {total}\n" + "\n".join(out)

    def t_find(self, a):
        from collections import defaultdict
        pat = str(a.get("pattern", ""))
        if not pat:
            return "error: empty pattern"
        try:
            rx = re.compile(pat, re.I if a.get("ignore_case", True) else 0)
        except re.error as e:
            return f"error: bad regex: {e}"
        base = self._res(a.get("path", ""))
        glob = a.get("glob")
        fo   = bool(a.get("files_only", False))
        limit, hits, total = min(int(a.get("limit", 40)), 100), defaultdict(list), 0
        for fp in self._walk(base, glob):
            try:
                with open(fp, encoding="utf-8", errors="replace") as fh:
                    for i, l in enumerate(fh, 1):
                        if rx.search(l):
                            hits[self._rel(fp)].append((i, l.strip()))
                            total += 1
                            if fo:
                                break
            except OSError:
                continue
        if not hits:
            return "no matches"
        if fo:
            return f"{len(hits)} files\n" + "\n".join(list(hits)[:limit])
        out, n = [], 0
        for f, hs in hits.items():
            if n >= limit:
                out.append("... more files; narrow with path/glob or a more specific pattern")
                break
            out.append(f"{f}  ({len(hs)} hits)")
            for ln, tx in hs[:3]:
                out.append(f"  {ln}: {tx[:170]}")
                n += 1
        return f"{total} matches in {len(hits)} files\n" + "\n".join(out)

    # ---- Dosya degistirme araclari ---------------------------------------- #
    @staticmethod
    def _strip_ln(s):
        ls = s.split("\n")
        ne = [l for l in ls if l.strip()]
        if ne and all(re.match(r"^\d+: ?", l) for l in ne):
            return "\n".join(re.sub(r"^\d+: ?", "", l) for l in ls)
        return s

    @staticmethod
    def _indent(l):
        return len(l) - len(l.lstrip(" "))

    def t_edit_file(self, a):
        old, new = a.get("old"), a.get("new")
        if not old or new is None:
            return "error: put the exact existing lines in <old>...</old> and the replacement in <new>...</new> after the call"
        p   = self._file(a.get("path") or a.get("file", ""), write=True)
        raw = open(p, encoding="utf-8", errors="replace", newline="").read()
        crlf = "\r\n" in raw
        text = raw.replace("\r\n", "\n")
        old, new = str(old).replace("\r\n", "\n"), str(new).replace("\r\n", "\n")
        if self._placeholder_re().search(new):
            return "error: <new> contains a placeholder ('...' / 'rest of code'); write real code"
        note, cnt = "", text.count(old)
        if cnt == 0 and self._strip_ln(old) != old:
            old, new = self._strip_ln(old), self._strip_ln(new)
            cnt = text.count(old)
        if cnt == 0:
            tl, ol = text.split("\n"), old.strip("\n").split("\n")
            ost = [x.strip() for x in ol]
            idx = [i for i in range(len(tl) - len(ol) + 1)
                   if [x.strip() for x in tl[i:i + len(ol)]] == ost]
            if len(idx) == 1:
                i = idx[0]
                d  = self._indent(tl[i]) - self._indent(ol[0])
                nl = new.strip("\n").split("\n")
                nl = [(" " * d + l if d > 0 else l[min(-d, self._indent(l)):]) if l.strip() else l for l in nl]
                text2 = "\n".join(tl[:i] + nl + tl[i + len(ol):])
                return self._commit(p, raw, text, text2, crlf, 1, " (whitespace-tolerant match)")
            if len(idx) > 1:
                return f"error: whitespace-insensitive match is ambiguous ({len(idx)} places); add more context lines"
            first = next((l.strip() for l in old.split("\n") if l.strip()), "")
            close = difflib.get_close_matches(first, [l.strip() for l in tl], n=1, cutoff=0.6)
            where = next((i + 1 for i, l in enumerate(tl) if close and l.strip() == close[0]), None)
            return (
                "error: <old> not found in file."
                + (f" Closest line {where}: {tl[where - 1].strip()[:120]}. "
                   f"read lines {max(1, where - 3)}-{where + 8} and copy exactly." if where
                   else " Use read to copy exact text.")
            )
        if cnt > 1 and not a.get("replace_all"):
            lines = [text[:m.start()].count("\n") + 1 for m in re.finditer(re.escape(old), text)][:6]
            return f"error: <old> matches {cnt} times (lines {lines}); add surrounding lines or pass replace_all=true"
        return self._commit(p, raw, text, text.replace(old, new), crlf, cnt, note)

    def _commit(self, p, raw, before, after, crlf, n, note=""):
        """Dosyaya yaz ve isteğe bagli check uygula."""
        if after == before:
            return "error: edit changes nothing"
        extra = self._post_edit_check(self._rel(p), before, after)
        self._backup(p)
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(after.replace("\n", "\r\n") if crlf else after)
        self.dirty = True
        return f"edited {self._rel(p)} ({n} replacement{'s' if n != 1 else ''}){note}{extra}"

    def _post_edit_check(self, rel, before, after):
        """Subclass'lar override ederek ek kontrol (bracket balance, XML, vs.) ekler."""
        return ""

    def _placeholder_re(self):
        """Subclass'lar override edebilir; ortak placeholder deseni."""
        return re.compile(
            r"(?im)^\s*(?://|/\*|#|<!--)[^\n]*\b(rest of (the )?(code|file)|existing code|"
            r"remaining code|same as before|unchanged)\b|^\s*\.\.\.\s*$"
        )

    def t_write_file(self, a):
        rel, content = str(a.get("path", "")).strip(), a.get("content")
        if not rel or content is None or len(str(content)) > 60_000:
            return "error: need path and a <content>...</content> block (<60KB) after the call"
        content = str(content)
        if self._placeholder_re().search(content):
            return "error: content has a placeholder ('...' / 'rest of code'); write real code"
        p = self._res(rel, write=True)
        existed = os.path.exists(p)
        if existed and not a.get("overwrite"):
            return f"error: {rel} already exists; use edit_file (or overwrite=true to replace it entirely)"
        before = open(p, encoding="utf-8", errors="replace").read() if existed else ""
        self._backup(p)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        content = content.rstrip("\n") + "\n"
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        self.dirty = True
        return f"{'overwrote' if existed else 'created'} {rel} ({content.count(chr(10))} lines)"

    def t_delete_file(self, a):
        p = self._file(a.get("path") or a.get("file", ""), write=True)
        self._backup(p)
        os.remove(p)
        self.dirty = True
        return f"deleted {self._rel(p)} (restore with undo)"

    def t_undo(self, a):
        rel = self._rel(self._res(a.get("path") or a.get("file", "")))
        if rel not in self.originals:
            return f"error: {rel} was not changed in this session"
        src, dst = self.originals.pop(rel), os.path.join(self.root, rel)
        if src is None:
            if os.path.exists(dst):
                os.remove(dst)
            return f"removed new file {rel}"
        shutil.copy2(src, dst)
        self.dirty = True
        return f"restored {rel} to session-start state"

    def t_diff(self, a):
        want = self._rel(self._res(a["path"])) if a.get("path") else None
        out  = []
        for rel, bak in sorted(self.originals.items()):
            if want and rel != want:
                continue
            cur = os.path.join(self.root, rel)
            old = open(bak, encoding="utf-8", errors="replace").read().splitlines() if bak else []
            new = open(cur, encoding="utf-8", errors="replace").read().splitlines() if os.path.exists(cur) else []
            if not bak:
                out.append(f"+++ new file {rel} ({len(new)} lines)")
                continue
            d = list(difflib.unified_diff(old, new, f"a/{rel}", f"b/{rel}", n=2, lineterm=""))
            out += d or [f"(no net change in {rel})"]
        return "\n".join(out) or "(no changes in this session)"
