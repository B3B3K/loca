PHASE 4B NEXT.JS REVIEW (stack: @@STACK@@). Run nextjs_audit, then triage what it queued with next_candidates.
Then review manually: (1) read the middleware/proxy and its matcher: is it the only auth check, which routes does it
skip? (2) for every route handler and Server Action: authentication, ownership check on the resource id (IDOR) and
schema validation of the input; (3) data passed from server to client components; (4) caching of per-user data;
(5) next.config rewrites, redirects, headers and images; (6) auth library config (Auth.js/NextAuth, Clerk,
Supabase): callbacks, session and token handling. Call findings first to avoid duplicates; report_finding for each
confirmed issue. Finish with what was reviewed.
