PHASE 4C FRONTEND REVIEW (stack: @@STACK@@). Use sast_scan with category "xss", then find/read to check: every
dangerouslySetInnerHTML/innerHTML sink (is the input sanitised with DOMPurify or similar?), href/src built from
user data, markdown rendering, token storage, postMessage, client-side redirects from query params, TypeScript
casts on request/JSON data without a runtime schema, and mass assignment ({...body}). Call findings first to avoid
duplicates; report_finding for each confirmed issue. Finish with what was reviewed.
