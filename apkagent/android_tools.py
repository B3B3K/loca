"""Android/JADX specific tools registered into a ToolRegistry."""
import os
import re
import shlex
import subprocess
import xml.etree.ElementTree as ET
from collections import defaultdict

from . import crc
from .tools import as_bool, as_int
from .workspace import _manifest_path

UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
STD_SUFFIX = "0000-1000-8000-00805f9b34fb"
BLE_APIS = ["BluetoothGattCallback", "writeCharacteristic", "setCharacteristicNotification",
            "onCharacteristicChanged", "onCharacteristicWrite", "discoverServices",
            "BluetoothLeScanner", "getCharacteristic", "BluetoothGatt", "requestMtu", "00002902"]
URL_RE = re.compile(r"https?://[^\s\"'<>\\)]+")
URL_NOISE = ("schemas.android.com", "w3.org", "apache.org", "xmlpull.org", "google.com/", "gstatic.com")
RETROFIT_RE = re.compile(r'@(GET|POST|PUT|DELETE|PATCH|HEAD)\(\s*"([^"]*)"')
SH_ALLOW = {"ls", "cat", "head", "tail", "grep", "wc", "file", "xxd", "strings"}


def register_android_tools(reg, ws):
    def _read(full):
        return full.read_text(encoding="utf-8", errors="replace")

    @reg.register()
    def manifest():
        p = _manifest_path(ws.src)
        if not p:
            return "error: AndroidManifest.xml not found"
        A = "{http://schemas.android.com/apk/res/android}"
        root = ET.parse(p).getroot()
        out = [f"package: {root.get('package')}"]
        out += [f"permission: {e.get(A + 'name')}" for e in root.iter("uses-permission")]
        out += [f"feature: {e.get(A + 'name')} required={e.get(A + 'required')}" for e in root.iter("uses-feature")]
        for tag in ("activity", "service", "receiver", "provider"):
            out += [f"{tag}: {e.get(A + 'name')}" for e in root.iter(tag)]
        return "\n".join(out)

    @reg.register()
    def ble_scan():
        uuids, score = defaultdict(set), {}
        for rel, full in ws.iter_files(exts={".java", ".kt"}):
            t = _read(full)
            for u in UUID_RE.findall(t):
                uuids[u.lower()].add(rel)
            s = sum(t.count(k) for k in BLE_APIS)
            if s:
                score[rel] = s
        rows = ["UUIDs (non-library code):"]
        for u, fs in sorted(uuids.items()):
            tag = " [std]" if u.endswith(STD_SUFFIX) else ""
            rows.append(f"  {u}{tag}  {', '.join(sorted(fs)[:3])}")
        rows.append("Files by BLE API usage:")
        rows += [f"  {n:4d} {r}" for r, n in sorted(score.items(), key=lambda x: -x[1])[:15]]
        return "\n".join(rows)

    @reg.register()
    def http_scan():
        urls, eps = defaultdict(set), defaultdict(set)
        for rel, full in ws.iter_files(exts={".java", ".kt"}):
            t = _read(full)
            for u in URL_RE.findall(t):
                if not any(n in u for n in URL_NOISE):
                    urls[u].add(rel)
            for verb, path in RETROFIT_RE.findall(t):
                eps[f"{verb} {path}"].add(rel)
        rows = ["URLs:"] + [f"  {u}  {sorted(f)[0]}" for u, f in sorted(urls.items())[:60]]
        rows += ["Retrofit endpoints:"] + [f"  {e}  {sorted(f)[0]}" for e, f in sorted(eps.items())[:60]]
        return "\n".join(rows)

    @reg.register()
    def find(pattern, path="", glob="*", ignore_case=True, files_only=False, limit=40, all=False):
        rx = re.compile(pattern, re.I if as_bool(ignore_case, True) else 0)
        limit, fo, hits, files = as_int(limit, 40), as_bool(files_only), [], []
        for rel, full in ws.iter_files(path, glob, include_lib=as_bool(all)):
            for n, line in enumerate(_read(full).splitlines(), 1):
                if rx.search(line):
                    if fo:
                        files.append(rel)
                        break
                    hits.append(f"{rel}:{n}: {line.strip()[:200]}")
                    if len(hits) >= limit:
                        return "\n".join(hits) + f"\n... (limit {limit} reached; narrow the query)"
            if fo and len(files) >= limit:
                break
        return "\n".join(files if fo else hits) or "(no matches)"

    @reg.register()
    def read(file, start=1, end=None, where="src"):
        full = ws.resolve(file, where)
        lines = _read(full).splitlines()
        s = max(1, as_int(start, 1))
        e = min(as_int(end, s + 249), s + 249, len(lines))
        return "\n".join(f"{i:5d}  {lines[i - 1]}" for i in range(s, e + 1)) or "(empty range)"

    @reg.register()
    def ls(path="", where="src"):
        base = ws.resolve(path, where)
        rows = []
        for e in sorted(base.iterdir(), key=lambda x: (not x.is_dir(), x.name)):
            if e.is_dir():
                n = sum(len(f) for _, _, f in os.walk(e))
                rows.append(f"{e.name}/  ({n} files)")
            else:
                rows.append(f"{e.name}  ({e.stat().st_size} B)")
        return "\n".join(rows) or "(empty)"

    @reg.register()
    def calc(op, values=None, hex=None, expect=None):
        if op == "jbytes":
            nums = [int(x.strip(), 0) for x in str(values).replace(";", ",").split(",") if x.strip()]
            return bytes(n & 0xFF for n in nums).hex()
        if op == "hex2jbytes":
            return ", ".join(str(b - 256 if b > 127 else b) for b in bytes.fromhex(str(hex)))
        if op == "crc":
            res = crc.brute(bytes.fromhex(str(hex)), expect)
            return "\n".join(res) if res else "no algorithm matched"
        return "error: op must be jbytes | hex2jbytes | crc"

    @reg.register()
    def write_file(path, content, append=False):
        full = ws.resolve(path, "out")
        full.parent.mkdir(parents=True, exist_ok=True)
        if full.suffix == ".py":
            try:
                compile(content, str(full), "exec")
            except SyntaxError as e:
                return f"error: syntax error not written: line {e.lineno}: {e.msg}"
        with open(full, "a" if as_bool(append) else "w", encoding="utf-8") as f:
            f.write(content)
        return f"wrote {full.relative_to(ws.out)} ({len(content)} chars)"


def register_shell_tool(reg, allow=SH_ALLOW, unsafe=False):
    @reg.register("sh")
    def sh(cmd):
        argv = shlex.split(str(cmd))
        if not argv or (argv[0] not in allow and not unsafe):
            return f"error: not allowed. Allowed: {', '.join(sorted(allow))}"
        p = subprocess.run(argv, capture_output=True, timeout=60, text=True, errors="replace")
        return (p.stdout + p.stderr).strip() or "(no output)"
