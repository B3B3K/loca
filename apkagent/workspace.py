"""Workspace: read-only decompiled sources (src) + writable output dir (out)."""
import os
import re
from pathlib import Path

LIB_PREFIXES = ("androidx/", "android/support/", "kotlin/", "kotlinx/", "com/google/", "okhttp3/",
                "okio/", "retrofit2/", "org/jetbrains/", "org/intellij/", "com/squareup/",
                "io/reactivex/", "dagger/", "javax/", "org/apache/", "com/bumptech/", "com/facebook/")
TEXT_EXT = {".java", ".kt", ".xml", ".smali", ".json", ".txt", ".properties", ".gradle", ".md"}


class Workspace:
    def __init__(self, src, out):
        self.src, self.out = Path(src).resolve(), Path(out).resolve()
        self.out.mkdir(parents=True, exist_ok=True)

    def root(self, where="src"):
        return self.out if where == "out" else self.src

    def resolve(self, rel, where="src"):
        root = self.root(where)
        p = (root / str(rel or "").lstrip("/\\")).resolve()
        if p != root and root not in p.parents:
            raise ValueError("path escapes workspace")
        return p

    def is_lib(self, relpath):
        r = relpath.replace(os.sep, "/")
        r = r.split("sources/", 1)[1] if "sources/" in r else r
        return r.startswith(LIB_PREFIXES)

    def iter_files(self, path="", glob="*", include_lib=False, exts=TEXT_EXT):
        import fnmatch
        base = self.resolve(path)
        for dp, _, fns in os.walk(base):
            for fn in sorted(fns):
                if Path(fn).suffix.lower() not in exts or not fnmatch.fnmatch(fn, glob):
                    continue
                full = Path(dp) / fn
                rel = str(full.relative_to(self.src))
                if include_lib or not self.is_lib(rel):
                    yield rel, full


def _manifest_path(src):
    for c in ("resources/AndroidManifest.xml", "AndroidManifest.xml"):
        if (Path(src) / c).is_file():
            return Path(src) / c
    return None


def detect_package(src):
    import xml.etree.ElementTree as ET
    p = _manifest_path(src)
    if not p:
        return None
    try:
        return ET.parse(p).getroot().get("package")
    except Exception:
        return None


def count_sources(src):
    return sum(1 for _, _, fs in os.walk(src) for f in fs if f.endswith((".java", ".kt")))


def layout_text(src, depth=2, limit=25):
    src, rows = Path(src), []
    for dp, dns, fns in os.walk(src):
        rel = Path(dp).relative_to(src)
        if len(rel.parts) == depth:
            n = sum(1 for _, _, f in os.walk(dp) for x in f if x.endswith((".java", ".kt")))
            if n:
                rows.append((n, str(rel)))
            dns[:] = []
    rows.sort(reverse=True)
    return "\n".join(f"  {r}/ ({n} files)" for n, r in rows[:limit]) or "  (empty)"
