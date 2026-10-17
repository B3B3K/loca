#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""apk_analyzer.py — JADX + Ollama/llama.cpp Android APK reverse-engineering agent.

Kullanim:
    python apk_analyzer.py <apk_dosyasi> [-g "gorev"]
"""
import argparse, fnmatch, json, os, re, shlex, shutil, subprocess, sys, zlib
import xml.etree.ElementTree as ET
from collections import defaultdict

from agent_core import (
    BaseAgent, col, add_common_args, build_llm_from_args,
)

# --------------------------------------------------------------------------- #
# APK / BLE / HTTP sabitler
# --------------------------------------------------------------------------- #
STD_SUFFIX = "-0000-1000-8000-00805f9b34fb"
UUID_RE    = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
BLE_APIS   = [
    "connectGatt", "BluetoothGattCallback", "onServicesDiscovered",
    "setCharacteristicNotification", "writeDescriptor", "writeCharacteristic",
    "onCharacteristicChanged", "onCharacteristicWrite", "onCharacteristicRead",
    "requestMtu", "createBond", "startLeScan", "BluetoothLeScanner",
    "createRfcommSocketToServiceRecord",
]
URL_RE  = re.compile(r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+")
NOISE   = ("schemas.android.com", "w3.org", "apache.org", "xmlpull.org",
           "ns.adobe.com", "developer.android.com", "google.com/", "googleapis.com",
           "github.com", "example.com", "play.google")
NOISE_PKGS = ("androidx", "kotlin", "kotlinx", "android/support", "com/google",
              "okhttp3", "okio", "retrofit2", "org/jetbrains", "org/intellij",
              "com/squareup", "dagger", "javax", "org/apache", "org/slf4j",
              "coil", "io/reactivex", "org/bouncycastle")
RETRO_RE = re.compile(r'@(GET|POST|PUT|DELETE|PATCH|HEAD)\(\s*"([^"]+)"')

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
- find {pattern, [path], [glob], [ignore_case=true], [files_only=false], [limit=40], [all=false]}
- read {file, [start=1], [end], [where="src"|"out"]}   numbered lines (max 250 per call).
- ls {[path], [where]}                          directory listing.
- calc {op, ...}                                op="jbytes"|"hex2jbytes"|"crc".
- note {text}                                   durable finding WITH evidence (file:line).
- write_file {path, content, [append=false]}    write into out. Keep each file under ~120 lines.
- finish {report}                               final report.

PHASES
1 RECON: manifest, ble_scan, http_scan. Note main package, BLE manager/callback classes, candidate UUIDs.
2 MAP: find BluetoothGattCallback; trace connect -> service discovery -> enable notifications -> write path.
3 FRAMING: header/magic, length, command id, payload, checksum/CRC (verify with calc crc), endianness, MTU.
4 COMMANDS: per feature record command id, request layout, response layout, units.
5 SPEC: write PROTOCOL.md BEFORE the code.
6 SDK: transport, framing/checksum, command builders + response parsers, CLI example, README, tests.
7 finish.

RULES
1. Before each call write 1-2 sentences. Never invent code, addresses or output.
2. Obfuscated names (a.b.c, m123a) are normal: infer purpose from usage; mark guesses UNVERIFIED.
3. CLEAN-ROOM: record protocol FACTS and re-implement in your own words. Do NOT copy decompiled code.
4. NEVER hardcode vendor secrets. Document how keys are derived; pass them as parameters.
5. Keep outputs small. If output was truncated, narrow the query.
6. Reply language: same as the user's. Code, identifiers and PROTOCOL.md in English.
7. FORMAT: no markdown; one or two plain sentences, exactly one <tool> call, then STOP.
"""

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


# --------------------------------------------------------------------------- #
# CRC / checksum yardimcilari
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
    "crc8":              _mk(8,  0x07, 0,      False, False, 0),
    "crc8_maxim":        _mk(8,  0x31, 0,      True,  True,  0),
    "crc16_ccitt_false": _mk(16, 0x1021, 0xFFFF, False, False, 0),
    "crc16_xmodem":      _mk(16, 0x1021, 0,      False, False, 0),
    "crc16_kermit":      _mk(16, 0x1021, 0,      True,  True,  0),
    "crc16_x25":         _mk(16, 0x1021, 0xFFFF, True,  True,  0xFFFF),
    "crc16_modbus":      _mk(16, 0x8005, 0xFFFF, True,  True,  0),
    "crc16_arc":         _mk(16, 0x8005, 0,      True,  True,  0),
    "crc32":             (32, lambda d: zlib.crc32(bytes(d)) & 0xFFFFFFFF),
    "sum8":              (8,  lambda d: sum(d) & 0xFF),
    "sum8_neg":          (8,  lambda d: (-sum(d)) & 0xFF),
    "sum8_inv":          (8,  lambda d: (~sum(d)) & 0xFF),
    "xor8":              (8,  lambda d: __import__("functools").reduce(lambda x, y: x ^ y, d, 0)),
    "sum16":             (16, lambda d: sum(d) & 0xFFFF),
}


def to_bytes(h):
    h = re.sub(r"(0x|[^0-9a-fA-F])", "", str(h))
    if len(h) % 2:
        raise ValueError("odd number of hex digits")
    return bytes.fromhex(h)


# --------------------------------------------------------------------------- #
# Agent
# --------------------------------------------------------------------------- #
class ApkAgent(BaseAgent):
    TOOLS = {"manifest", "ble_scan", "http_scan", "find", "read", "ls",
             "calc", "note", "write_file", "finish"}
    FEWSHOT = FEWSHOT
    SKIP_DIRS = {".git", "__pycache__"}
    TEXT_EXTS = (".java", ".kt", ".xml", ".json", ".md", ".txt")
    BIN_EXTS  = (".apk", ".zip", ".so", ".dex", ".class", ".bin")
    MUTATORS  = {"write_file"}

    def __init__(self, llm, apk, src, out, ctx, max_steps, max_obs, lang):
        # BaseAgent.__init__ root=src olarak kullanir; out ayri tutulur
        super().__init__(llm, src, out, ctx, max_steps, max_obs)
        self.apk_path = apk
        self.src = self.root   # okunabilirlik icin alias
        self.out = os.path.realpath(out)
        os.makedirs(self.out, exist_ok=True)
        self.notes_file = os.path.join(self.out, ".notes.json")
        # notes zaten load edildi; burada out-based dosyaya yonlendir
        try:
            with open(self.notes_file, encoding="utf-8") as f:
                self.notes = json.load(f)
        except (OSError, ValueError):
            pass
        nfiles = sum(1 for _ in self._iter_src())
        pkg    = self._manifest_pkg() or "?"
        layout = self._ls_src("")
        self.system_base = (
            SYSTEM.replace("@@APK@@", os.path.basename(apk))
                  .replace("@@LANG@@", lang)
                  .replace("@@PKG@@", pkg)
                  .replace("@@NFILES@@", str(nfiles))
                  .replace("@@LAYOUT@@", "\n".join("    " + l for l in layout.splitlines()[:20]))
        )

    # ---- kaynak gezici yardimcilari --------------------------------------- #
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

    def _ws_res(self, where, p):
        root = self.out if where == "out" else self.src
        full = os.path.realpath(os.path.join(root, str(p or "").lstrip("/")))
        if full != root and not full.startswith(root + os.sep):
            raise ValueError("path escapes workspace")
        return full

    def _noisy(self, rel):
        r = "/" + rel.replace("\\", "/").strip("/") + "/"
        return any(f"/{p}/" in r for p in NOISE_PKGS)

    # ---- ls/find overrides  (src/out iki koklu) ---------------------------- #
    def _ls_src(self, p):
        root = self._ws_res("src", p)
        if not os.path.isdir(root):
            return "error: not a directory"
        rows = []
        for name in sorted(os.listdir(root)):
            fp = os.path.join(root, name)
            rows.append(
                f"{name}/  ({sum(len(f) for _, _, f in os.walk(fp))} files)"
                if os.path.isdir(fp) else f"{name}  ({os.path.getsize(fp)} B)"
            )
        return ("\n".join(rows[:80]) + (f"\n... +{len(rows) - 80} more" if len(rows) > 80 else "")) if rows else "(empty)"

    def t_ls(self, a):
        return self._ls_src(a.get("path", "")) if a.get("where", "src") == "src" else \
               self._ls_src_out("out", a.get("path", ""))

    def _ls_src_out(self, where, p):
        root = self._ws_res(where, p)
        if not os.path.isdir(root):
            return "error: not a directory"
        rows = []
        for name in sorted(os.listdir(root)):
            fp = os.path.join(root, name)
            rows.append(f"{name}/  ({sum(len(f) for _, _, f in os.walk(fp))} files)"
                        if os.path.isdir(fp) else f"{name}  ({os.path.getsize(fp)} B)")
        return "\n".join(rows[:80]) if rows else "(empty)"

    def t_read(self, a):
        p = self._ws_res(a.get("where", "src"), a.get("file", ""))
        if not os.path.isfile(p):
            return "error: file not found (use find/ls)"
        base = self.src if a.get("where") != "out" else self.out
        start = max(1, int(a.get("start", 1)))
        end   = min(int(a.get("end", start + 149)), start + 249)
        out, total = [], 0
        with open(p, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, 1):
                total = i
                if start <= i <= end:
                    out.append(f"{i}: {line.rstrip()[:300]}")
        return f"{os.path.relpath(p, base)} lines {start}-{min(end, total)} of {total}\n" + "\n".join(out)

    def t_find(self, a):
        pat = str(a.get("pattern", ""))
        if not pat:
            return "error: empty pattern"
        base = self._ws_res("src", a.get("path", ""))
        ic   = a.get("ignore_case", True)
        fo   = a.get("files_only", False)
        glob = a.get("glob")
        all_ = bool(a.get("all", False))
        limit = min(int(a.get("limit", 40)), 100)
        # rg varsa kullan
        if shutil.which("rg"):
            cmd = (["rg", "-n", "--no-heading", "-I"] + (["-i"] if ic else [])
                   + (["-l"] if fo else []) + (["-g", glob] if glob else []) + ["-e", pat, str(base)])
            p = subprocess.run(cmd, capture_output=True, timeout=90)
            lines = p.stdout.decode("utf-8", "replace").splitlines()
        else:
            lines = self._py_find(base, pat, ic, fo, glob, not all_)
        return self._fmt_find(lines, limit, fo, all_)

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
        return lines

    def _fmt_find(self, lines, limit, fo, all_):
        pre  = self.src + os.sep
        rows = [l.replace(pre, "") for l in lines]
        dropped = 0
        if not all_:
            keep    = [r for r in rows if not self._noisy(os.path.dirname(r if fo else r.split(":", 1)[0]))]
            dropped, rows = len(rows) - len(keep), keep
        note = f" ({dropped} library-noise matches hidden; pass all=true to include)" if dropped else ""
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
        for f, hits in by.items():
            if n >= limit:
                out.append("... more files; narrow query")
                break
            out.append(f"{f}  ({len(hits)} hits)")
            for ln, tx in hits[:3]:
                out.append(f"  {ln}: {tx[:170]}")
                n += 1
        return f"{len(rows)} matches in {len(by)} files{note}\n" + "\n".join(out)

    # ---- Araclar ------------------------------------------------------------ #
    def t_note(self, a):
        t = str(a.get("text", "")).strip()
        tagged = False
        if t and not re.search(r"\.(java|kt|xml|so)\S*:\d+", t) and "UNVERIFIED" not in t.upper():
            t, tagged = "[UNVERIFIED: no file:line] " + t, True
        if t:
            self.notes.append(t)
            self._save_notes()
            print(col(f"  [note] {t}", "36"))
        return f"noted ({len(self.notes)} notes)" + (
            "; stored as UNVERIFIED. Re-note with file:line once you read it." if tagged else "")

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
        sdk  = root.find("uses-sdk")
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
                    nm   = e.get(ns + "name", "")
                    acts = [x.get(ns + "name", "").split(".")[-1] for x in e.iter("action")]
                    items.append(nm + (f" [{','.join(acts[:3])}]" if acts else ""))
                if items:
                    o.append(f"{kind}s ({len(items)}): " + "; ".join(items[:12]))
        return "\n".join(o)

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
            o.append("  nothing found — logic may be in native code or a vendor SDK.")
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
        return "\n".join(dict.fromkeys(hits)) or "no match (try other byte ranges or custom polynomial)"

    def t_write_file(self, a):
        rel     = str(a.get("path", "")).strip()
        content = str(a.get("content", ""))
        if not rel or len(content) > 80_000:
            return "error: need path and content (<80KB)"
        p = self._ws_res("out", rel)
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
                msg += f"\nSYNTAX ERROR line {e.lineno}: {e.msg} — fix with write_file again"
        return msg


# --------------------------------------------------------------------------- #
# JADX decompiler
# --------------------------------------------------------------------------- #
def _has_sources(d):
    for _, _, fns in os.walk(d):
        if any(f.endswith((".java", ".kt")) for f in fns):
            return True
    return False


def decompile(apk, work, extra):
    dst    = os.path.join(work, "jadx")
    marker = os.path.join(work, ".jadx_ok")
    if os.path.isfile(marker) and _has_sources(dst):
        print(col(f"[*] mevcut decompile kullaniliyor: {dst}", "33"))
        return dst
    if os.path.isdir(dst):
        print(col("[!] onceki decompile yarım/bozuk, siliniyor...", "33"))
        shutil.rmtree(dst, ignore_errors=True)
    jadx = shutil.which("jadx")
    if not jadx:
        sys.exit("jadx bulunamaıdi. PATH'e ekle ya da --src-dir ver.")
    print(col("[*] jadx calisiyor (buyuk APK'larda dakikalar surebilir)...", "33"))
    p = subprocess.Popen(
        [jadx, "-d", dst, "--show-bad-code"] + shlex.split(extra or "") + [apk],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace"
    )
    log = []
    for line in p.stdout:
        print(line, end="")
        log.append(line)
    p.wait()
    text = "".join(log).lower()
    crashed = any(k in text for k in ("outofmemoryerror", "insufficient memory", "hs_err_pid", "errno=1455"))
    if crashed or not _has_sources(dst):
        shutil.rmtree(dst, ignore_errors=True)
        sys.exit("[!] jadx basarisiz (bellek yetersiz olabilir). --jadx-args \"-j 1\" dene.")
    open(marker, "w").close()
    return dst


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="JADX APK reverse-engineering agent (Ollama / llama.cpp)")
    ap.add_argument("apk")
    ap.add_argument("-g", "--goal")
    ap.add_argument("--work")
    ap.add_argument("--out-dir")
    ap.add_argument("--src-dir",       help="hazir JADX kaynak klasoru")
    ap.add_argument("--jadx-args",     help='ornek: "-j 2"')
    ap.add_argument("--decompile-only", action="store_true")
    ap.add_argument("--lang", default="Python 3 (bleak for BLE; minimal dependencies; pytest tests)")
    add_common_args(ap, default_temp=0.2, default_max_tokens=1500, default_max_steps=60)
    a = ap.parse_args()

    if not os.path.isfile(a.apk) and not a.src_dir:
        sys.exit(f"Dosya yok: {a.apk}")
    work = a.work or os.path.splitext(os.path.abspath(a.apk))[0] + "_work"
    os.makedirs(work, exist_ok=True)
    src = os.path.abspath(a.src_dir) if a.src_dir else decompile(os.path.abspath(a.apk), work, a.jadx_args)
    if not _has_sources(src):
        sys.exit(f"JADX kaynaklari bulunamadi: {src}")
    out = os.path.abspath(a.out_dir or os.path.join(work, "sdk_out"))
    if a.decompile_only:
        print(col(f"[*] decompile hazir: {src}", "32"))
        return

    llm, url, model_name = build_llm_from_args(a)
    ctx = llm.n_ctx() or a.ctx
    print(col(f"[*] {a.backend} {url} model={model_name} ctx={ctx} max_tokens={a.max_tokens}", "33"))
    print(col(f"[*] source={src}", "33"))

    agent = ApkAgent(llm, a.apk, src, out, ctx, a.max_steps, a.max_obs, a.lang)
    default_goal = (
        "Analyze this APK. Recover the device communication protocol (BLE GATT and any HTTP API) "
        "and build the documented open-source SDK in the out directory, following your phases."
    )

    def handle(goal):
        rep = agent.run_goal(goal)
        if rep:
            print(col("\n══════ RAPOR ══════", "35") + f"\n{rep}\n")
            with open(os.path.join(agent.out, "REPORT.md"), "a", encoding="utf-8") as f:
                f.write(f"## {goal}\n\n{rep}\n\n")

    if a.goal:
        handle(a.goal)
        return
    print(f"Bos satir/'go' = tam analiz. Komutlar: /notes /reset /files /find <regex> /ble /quit\nCikti: {agent.out}")
    while True:
        try:
            line = input(col("apk> ", "35")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if line in ("/quit", "/q"):
            break
        elif line == "/notes":
            print("\n".join(f"- {n}" for n in agent.notes) or "(bos)")
        elif line == "/reset":
            agent.msgs.clear()
            print("konusma sifirlandi (notlar duruyor)")
        elif line == "/files":
            print(agent._ls_src_out("out", ""))
        elif line == "/ble":
            print(agent.t_ble_scan({}))
        elif line.startswith("/find "):
            print(agent.t_find({"pattern": line[6:]}))
        else:
            handle(default_goal if line in ("", "go") else line)


if __name__ == "__main__":
    main()
