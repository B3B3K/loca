"""JADX wrapper."""
import shutil
import subprocess
from pathlib import Path


def decompile(apk, out_dir, jadx_bin=None, extra_args=(), timeout=3600, force=False):
    out = Path(out_dir)
    if out.exists() and any(out.rglob("*.java")) and not force:
        return out
    exe = jadx_bin or shutil.which("jadx")
    if not exe:
        raise FileNotFoundError("jadx not found: put it on PATH or pass jadx_bin")
    out.mkdir(parents=True, exist_ok=True)
    p = subprocess.run([exe, "-d", str(out), "--show-bad-code", *extra_args, str(apk)],
                       capture_output=True, text=True, timeout=timeout)
    if not any(out.rglob("*.java")):  # jadx exits non-zero on partial errors; sources are what matter
        raise RuntimeError(f"jadx produced no sources:\n{(p.stderr or p.stdout)[-500:]}")
    return out
