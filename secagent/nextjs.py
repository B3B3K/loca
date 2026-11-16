"""Next.js-aware structural audit: route handlers, Server Actions, middleware, client leakage, headers, versions."""
import os
import re

from . import deps as depmod

APP_ROUTE = re.compile(r"(^|/)app/(.*/)?route\.(t|j)sx?$")
PAGES_API = re.compile(r"(^|/)pages/api/")
METHOD_RE = re.compile(r"export\s+(?:async\s+)?(?:function\s+|const\s+)(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b")
AUTH_RE = re.compile(
    r"(getServerSession|getSession\b|\bauth\s*\(|\bgetToken\b|currentUser|requireAuth|withAuth|authenticate|"
    r"verifyToken|jwtVerify|jwt\.verify|clerkClient|\bgetAuth\b|supabase\.auth|createServerClient|validateRequest|"
    r"isAuthenticated|(?i:authorization)|(?i:session\.user)|\bgetUser\s*\()")
VALID_RE = re.compile(r"(\bz\.|zod|safeParse|\.parse\(|\byup\b|\bJoi\b|valibot|superstruct|\bajv\b|class-validator|"
                      r"typebox|arktype|validate\w*\()")
BODY_RE = re.compile(r"(\.json\(\)|req\.body|formData\(\)|\.text\(\))")
SIG_RE = re.compile(r"(constructEvent|verifySignature|svix|createHmac|timingSafeEqual|signature)", re.I)
CRON_RE = re.compile(r"(CRON_SECRET|authorization)", re.I)
SENSITIVE_PATH = re.compile(r"(user|admin|account|profile|order|billing|invoice|internal|payment|me\b|settings)", re.I)
ENV_CLIENT = re.compile(r"process\.env\.(?!NEXT_PUBLIC_|NODE_ENV)\w+")
SERVER_IMPORT = re.compile(r"""from\s+["'](@prisma/client|fs|node:fs|child_process|server-only|pg|mysql2|mongoose|bcrypt|"""
                           r"""@/lib/(db|prisma|server)[^"']*|\.\./lib/(db|prisma))["']""")
HEADERS = {"Content-Security-Policy": r"Content-Security-Policy", "Strict-Transport-Security": r"Strict-Transport-Security",
           "X-Frame-Options/frame-ancestors": r"X-Frame-Options|frame-ancestors",
           "X-Content-Type-Options": r"X-Content-Type-Options", "Referrer-Policy": r"Referrer-Policy"}


def _line(lines, rx, default=1):
    for i, ln in enumerate(lines, 1):
        if rx.search(ln):
            return i
    return default


def register_nextjs_tools(reg, project, cands, rules_dir=None):
    @reg.register()
    def nextjs_audit():
        st = project.stack()
        if not st["next"]:
            return "not a Next.js project (no 'next' dependency in any package.json)"
        out = [f"next {st['next']} | react {st['react']} | typescript {st['typescript']}"]
        added = 0

        def cand(rule, title, sev, cwe, file, line):
            nonlocal added
            added += bool(cands.add(rule, title, sev, cwe, file, line, "nextjs_audit"))

        handlers, actions, clients, mws, cfg_text, config_files = [], [], 0, [], "", []
        for rel, full in project.iter_files(globs=("*.ts", "*.tsx", "*.js", "*.jsx", "*.mjs", "*.cjs")):
            norm = rel.replace(os.sep, "/")
            lines = project.lines(full)
            text = "\n".join(lines)
            head = "\n".join(lines[:8])
            base = full.name
            use_client = bool(re.search(r"^\s*[\"']use client[\"']", head, re.M))
            file_action = bool(re.search(r"^\s*[\"']use server[\"']", head, re.M))
            if base.startswith("next.config."):
                cfg_text += text
                config_files.append(rel)
            if base.startswith(("middleware.", "proxy.")) and norm.count("/") <= 1:
                cfg_text += text
                mws.append((rel, text, lines))
            auth, valid, body = bool(AUTH_RE.search(text)), bool(VALID_RE.search(text)), bool(BODY_RE.search(text))

            if APP_ROUTE.search(norm) or PAGES_API.search(norm):
                if APP_ROUTE.search(norm):
                    methods = sorted(set(METHOD_RE.findall(text)))
                else:
                    methods = sorted(set(re.findall(r"req\.method\s*===?\s*[\"'](\w+)[\"']", text))) or ["handler"]
                mutating = bool(set(methods) & {"POST", "PUT", "PATCH", "DELETE"}) or \
                    (methods == ["handler"] and body)
                ln = _line(lines, METHOD_RE if APP_ROUTE.search(norm) else re.compile(r"export\s+default"))
                handlers.append(f"{norm} [{','.join(methods) or '?'}] auth={'yes' if auth else 'NO'} "
                                f"validation={'yes' if valid else ('NO' if body else 'n/a')}")
                lower = norm.lower()
                if "webhook" in lower and not SIG_RE.search(text):
                    cand("NEXT-WEBHOOK-NO-SIG", "Webhook route without visible signature verification", "high", "CWE-345", rel, ln)
                if "/cron" in lower and not CRON_RE.search(text):
                    cand("NEXT-CRON-NO-SECRET", "Cron route without secret/authorization check", "medium", "CWE-306", rel, ln)
                if not auth and "webhook" not in lower and "/cron" not in lower:
                    if "admin" in lower:
                        cand("NEXT-ADMIN-NO-AUTH-HINT", "Admin route handler without visible authentication", "high", "CWE-306", rel, ln)
                    elif mutating or SENSITIVE_PATH.search(lower):
                        cand("NEXT-ROUTE-NO-AUTH-HINT", "Route handler without visible authentication (middleware may cover it)",
                             "medium", "CWE-306", rel, ln)
                if body and not valid:
                    cand("NEXT-NO-VALIDATION-HINT", "Request body used without visible schema validation", "low", "CWE-20", rel, ln)
                if re.search(r"revalidate(Path|Tag)", text) and not auth:
                    cand("NEXT-REVALIDATE-OPEN", "revalidate endpoint without visible auth/secret", "medium", "CWE-306", rel, ln)

            if file_action:
                names = re.findall(r"export\s+(?:async\s+function\s+(\w+)|const\s+(\w+)\s*=\s*async)", text)
                actions.append(f"{norm}: {', '.join(a or b for a, b in names) or '(actions)'} auth={'yes' if auth else 'NO'} "
                               f"validation={'yes' if valid else 'NO'}")
                ln = _line(lines, re.compile(r"[\"']use server[\"']"))
                if not auth:
                    cand("NEXT-ACTION-NO-AUTH-HINT", "Server Actions file without visible authentication/authorization",
                         "medium", "CWE-862", rel, ln)
                if re.search(r"(formData|FormData)", text) and not valid:
                    cand("NEXT-ACTION-NO-VALIDATION", "Server Action input used without visible schema validation",
                         "low", "CWE-20", rel, ln)
            elif use_client:
                clients += 1
                m = ENV_CLIENT.search(text)
                if m:
                    cand("NEXT-CLIENT-SECRET-REF", f"Client component reads {m.group(0)} (server-only env var)",
                         "medium", "CWE-200", rel, _line(lines, ENV_CLIENT))
                if SERVER_IMPORT.search(text):
                    cand("NEXT-CLIENT-IMPORTS-SERVER", "Client component imports server-side module (code/secrets in bundle)",
                         "medium", "CWE-200", rel, _line(lines, SERVER_IMPORT))

        out += [f"route handlers: {len(handlers)}"] + [f"  {h}" for h in handlers[:40]]
        out += [f"server action files: {len(actions)}"] + [f"  {a}" for a in actions[:25]]
        out.append(f"client components: {clients}")

        for rel, text, lines in mws:
            m = re.search(r"matcher\s*:\s*(\[[^\]]*\]|[\"'][^\"']*[\"'])", text)
            matcher = " ".join(m.group(1).split())[:160] if m else "(no matcher: runs on every request)"
            out.append(f"middleware/proxy {rel}: matcher={matcher} auth_logic={'yes' if AUTH_RE.search(text) else 'NO'}")
            if m and re.search(r"\?!\s*api|\?!api", m.group(1)):
                cand("NEXT-MIDDLEWARE-API-EXCLUDED", "Middleware matcher excludes /api routes (they bypass middleware auth)",
                     "medium", "CWE-284", rel, _line(lines, re.compile(r"matcher")))
        if not mws:
            out.append("middleware/proxy: none found (authorization must be enforced in handlers/actions)")

        missing = [k for k, rx in HEADERS.items() if not re.search(rx, cfg_text, re.I)]
        if missing:
            out.append("security headers not set in next.config/middleware: " + ", ".join(missing) +
                       " (ignore if set by the CDN/reverse proxy)")
            target = config_files[0] if config_files else (mws[0][0] if mws else None)
            if target:
                cand("NEXT-MISSING-HEADERS", "Security headers missing in next.config/middleware: " + ", ".join(missing),
                     "low" if len(missing) < 3 else "medium", "CWE-693", target, 1)

        gi = project.root / ".gitignore"
        if gi.is_file():
            if not re.search(r"^\s*/?\.env", gi.read_text(errors="replace"), re.M):
                cand("NEXT-ENV-NOT-IGNORED", ".gitignore does not exclude .env files", "high", "CWE-540", ".gitignore", 1)
                out.append(".gitignore: .env files are NOT ignored")
        else:
            out.append("no .gitignore at project root")
        envs = [r for r, f in project.iter_files(globs=(".env", ".env.*"))
                if not re.search(r"\.(example|sample|template)$", f.name)]
        if envs:
            out.append("env files present (committed?): " + ", ".join(envs[:10]))

        hints = depmod.local_advisories(project, rules_dir)
        for n, v, ex, rel, e in hints:
            out.append(f"advisory hint: {n}@{v}{'' if ex else ' (declared)'} -> fixed {', '.join(e['fixed'])} | {e['title'][:90]}")
            cands.add("ADV-" + n, f"{n}@{v} below patched releases: {e['title'][:100]} (fixed {', '.join(e['fixed'])})",
                      e["severity"], "CWE-1104", rel, 0, "advisory-hints")
        cands.save()
        out.append(f"{added} new candidates queued (use next_candidates).")
        return "\n".join(out)
