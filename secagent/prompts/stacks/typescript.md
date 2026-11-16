TYPESCRIPT NOTES
- Types are erased at runtime: `as T`, `: any` and @ts-ignore on external data (request body, JSON.parse, fetch
  results, env) validate nothing. Require a runtime schema (zod, valibot, yup, ajv) at every trust boundary.
- Mass assignment: spreading a request body into a DB write ({...body}, data: body) lets clients set role,
  isAdmin, price or ownerId. Prefer an explicit allow-list of fields.
- Optional chaining and non-null assertions are not authorization checks.
