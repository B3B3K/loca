REACT NOTES
- JSX text is escaped. XSS needs dangerouslySetInnerHTML, innerHTML, href/src with javascript: URLs, srcDoc,
  raw-HTML markdown renderers (rehype-raw) or third-party widgets. Check for a sanitizer (DOMPurify) at the sink.
- Tokens in localStorage/sessionStorage are readable by any XSS; HttpOnly+Secure+SameSite cookies are safer.
- postMessage: wildcard targetOrigin and listeners that skip event.origin checks. Redirects from query params.
- Everything in the bundle is public (API keys, flags, source maps). Client-side checks are not authorization.
