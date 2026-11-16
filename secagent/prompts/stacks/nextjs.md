NEXT.JS NOTES
- Server Actions and Route Handlers are public HTTP endpoints. Each needs its own authentication, authorization
  (ownership check on the resource id, i.e. IDOR) and schema validation. Hidden buttons and UI checks protect nothing.
- Middleware/proxy is not a safe sole authorization boundary: bypass advisories keep appearing and matchers can
  exclude routes (api, _next, static). Authorize again next to the data access.
- NEXT_PUBLIC_* values and next.config env{} are inlined into the browser bundle: no secrets there.
  A server-only process.env.X read inside a "use client" file is a bug or a leak.
- Server code must not be imported into client components. Props passed from server to client components are
  serialized to the browser: never pass whole DB user rows (hashes, tokens, internal flags).
- Caching (fetch cache, unstable_cache, "use cache", ISR/revalidate) of per-user or authenticated data can leak
  across users. revalidate endpoints, webhooks and cron routes need a secret or signature check.
- SSRF: fetch() to a URL from params/searchParams/body, wildcard next/image remotePatterns, rewrites to user hosts.
  Open redirects: redirect()/NextResponse.redirect() with callbackUrl/returnUrl; allow-list or same-origin only.
- Keep next, react and react-server-dom-* on patched releases (2025-2026 advisories include RCE, middleware bypass,
  SSRF and DoS; 13.x/14.x have no patched line). dep_audit hints are curated: verify with npm audit.
- Not a finding by itself: JSX text escaping (safe by default), Prisma tagged-template $queryRaw`...`, input that
  is parsed with zod/valibot before use.
