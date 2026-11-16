PHASE 4 MANUAL REVIEW. Scanners miss logic flaws. Using NOTES and entrypoints, review: authentication and session
handling, authorization on every sensitive route (missing checks, IDOR), input validation reaching SQL/command/
template/file sinks, file upload and path handling, SSRF and redirects, deserialization, password storage and
crypto, CORS/CSRF, secrets in logs or error messages, rate limiting, business-logic abuse. Call findings first so
you do not duplicate. Use find and read; call report_finding for each confirmed issue. Finish with areas reviewed.
