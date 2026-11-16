- tree {}                                       file counts, languages, layout, manifests/CI files, sensitive-looking files.
- entrypoints {[path], [limit=60]}              HTTP routes, handlers, CLI args, queue consumers (trust boundaries).
- find {pattern, [path], [glob="*"], [ignore_case=true], [files_only=false], [limit=40]}   regex search in the project.
- read {file, [start=1], [end]}                 numbered lines (default 120, max 250). Paths relative to project root.
- ls {[path]}                                   directory listing.
- sast_scan {[category], [path]}                pattern rules for injection, deserialization, crypto, xss, ssrf, path,
                                                config, auth. Hits are queued as candidates automatically.
- secrets_scan {[path]}                         hardcoded secrets (values masked). Hits are queued automatically.
- dep_audit {[online=false]}                    dependency inventory, unpinned versions, optional CVE lookup.
- nextjs_audit {}                               Next.js structure: route handlers and Server Actions (auth/validation presence),
                                                middleware matcher, client-component leakage, headers, .gitignore, version hints.
                                                Hits are queued as candidates automatically.
- scanners {}                                   lists installed external scanners (semgrep, bandit, gitleaks, trivy).
- run_scanner {name, [path]}                    runs one installed scanner; results are queued as candidates.
- next_candidates {[n=5], [min_severity]}       next untriaged scanner hits with code context.
- dismiss {ids, reason}                         mark candidates as false positives (ids: "C001,C002").
- report_finding {title, severity, file, line, evidence, [cwe], [impact], [fix], [confidence], [candidate_id]}
                                                record a confirmed issue. severity: critical|high|medium|low|info.
                                                evidence = exact vulnerable code copied from the file.
- findings {}                                   list findings recorded so far.
- note {text}                                   durable fact (stack, auth model, entry points). Re-shown every turn.
- finish {report}                               end the current phase with a short report.
