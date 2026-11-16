PHASE 2 AUTOMATED SCAN. Run sast_scan, secrets_scan and dep_audit (online=true only if online lookups are allowed).
If the stack is Next.js also run nextjs_audit. Call scanners, then run_scanner for each installed scanner. Candidates are queued automatically; do not triage yet.
Finish with the counts and the three rule groups that look most serious.
