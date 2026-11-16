"""Read-only view of the project under review."""
import json
import os
from fnmatch import fnmatch
from pathlib import Path

IGNORE_DIRS = {".git", "node_modules", "vendor", "venv", ".venv", "env", "__pycache__", "dist", "build",
               "target", ".idea", ".vscode", ".next", ".nuxt", ".tox", "site-packages", ".gradle", "Pods",
               "coverage", ".mypy_cache", ".pytest_cache", ".terraform"}
SKIP_FILES = ("*.min.js", "*.min.css", "*.map", "*.svg", "*.lock", "package-lock.json", "yarn.lock",
              "pnpm-lock.yaml", "*.pb.go", "*.snap")
LANGS = {".py": "Python", ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
         ".ts": "TypeScript", ".tsx": "TypeScript", ".java": "Java", ".kt": "Kotlin", ".go": "Go",
         ".rb": "Ruby", ".php": "PHP", ".rs": "Rust", ".c": "C", ".h": "C/C++", ".cc": "C++", ".cpp": "C++",
         ".cs": "C#", ".swift": "Swift", ".sh": "Shell", ".tf": "Terraform", ".sql": "SQL"}
MANIFESTS = {"requirements.txt", "pyproject.toml", "Pipfile", "package.json", "package-lock.json", "go.mod",
             "Cargo.toml", "Cargo.lock", "pom.xml", "build.gradle", "build.gradle.kts", "Gemfile",
             "Gemfile.lock", "composer.json", "composer.lock", "Dockerfile", "docker-compose.yml",
             "docker-compose.yaml", ".gitlab-ci.yml", "Jenkinsfile"}
SENSITIVE_NAMES = (".env", ".env.*", "*.pem", "*.p12", "*.pfx", "*.jks", "*.keystore", "id_rsa", "id_dsa",
                   "id_ecdsa", "id_ed25519", "*.tfstate", ".htpasswd", "credentials", ".npmrc", ".pypirc",
                   "*.kdbx", "secrets.*", "*.sqlite", "*.db")


class Project:
    def __init__(self, root, excludes=(), max_size=1_000_000):
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise NotADirectoryError(root)
        self.excludes = IGNORE_DIRS | set(excludes)
        self.max_size = max_size

    def resolve(self, rel=""):
        p = (self.root / str(rel or "").lstrip("/\\")).resolve()
        if p != self.root and self.root not in p.parents:
            raise ValueError("path escapes project root")
        return p

    def rel(self, p):
        return str(Path(p).resolve().relative_to(self.root))

    def _binary(self, full):
        try:
            with open(full, "rb") as f:
                return b"\0" in f.read(2048)
        except OSError:
            return True

    def iter_files(self, path="", globs=("*",), include_all=False):
        base = self.resolve(path)
        if base.is_file():
            yield self.rel(base), base
            return
        for dp, dns, fns in os.walk(base):
            dns[:] = [d for d in sorted(dns) if d not in self.excludes]
            for fn in sorted(fns):
                if not any(fnmatch(fn, g) for g in globs):
                    continue
                if not include_all and any(fnmatch(fn, s) for s in SKIP_FILES):
                    continue
                full = Path(dp) / fn
                try:
                    if full.stat().st_size > self.max_size or full.is_symlink():
                        continue
                except OSError:
                    continue
                if self._binary(full):
                    continue
                yield self.rel(full), full

    @staticmethod
    def lines(full):
        return Path(full).read_text(encoding="utf-8", errors="replace").splitlines()

    def languages(self):
        cnt = {}
        for _, full in self.iter_files():
            lang = LANGS.get(full.suffix.lower())
            if lang:
                cnt[lang] = cnt.get(lang, 0) + 1
        return dict(sorted(cnt.items(), key=lambda x: -x[1]))

    def stack(self):
        """Detect JS/TS stack from every package.json (name -> declared version) and tsconfig."""
        if getattr(self, "_stack", None):
            return self._stack
        pk = {}
        for rel, full in self.iter_files(globs=("package.json",), include_all=True):
            try:
                d = json.loads(full.read_text(encoding="utf-8", errors="replace"))
            except ValueError:
                continue
            for sect in ("dependencies", "devDependencies", "peerDependencies"):
                for k, v in (d.get(sect) or {}).items():
                    pk.setdefault(k, (str(v), rel))
        ts = "typescript" in pk or any(True for _ in self.iter_files(globs=("tsconfig*.json",), include_all=True))
        self._stack = {"packages": pk, "next": pk.get("next", (None,))[0], "react": pk.get("react", (None,))[0],
                       "typescript": bool(ts)}
        return self._stack

    def stack_label(self):
        st, parts = self.stack(), []
        if st["next"]:
            parts.append(f"Next.js {st['next']}")
        if st["react"]:
            parts.append(f"React {st['react']}")
        if st["typescript"]:
            parts.append("TypeScript")
        return ", ".join(parts) or "no JS/TS framework detected"

    def count_files(self):
        return sum(1 for _ in self.iter_files())

    def layout(self, depth=2, limit=20):
        rows = []
        for dp, dns, _ in os.walk(self.root):
            dns[:] = [d for d in sorted(dns) if d not in self.excludes and not d.startswith(".")]
            rel = Path(dp).relative_to(self.root)
            if len(rel.parts) == depth or (len(rel.parts) < depth and not dns and rel.parts):
                n = sum(len(f) for _, _, f in os.walk(dp))
                rows.append((n, str(rel)))
            if len(rel.parts) >= depth:
                dns[:] = []
        rows.sort(reverse=True)
        return "\n".join(f"  {r}/ ({n} files)" for n, r in rows[:limit]) or "  (flat project)"
