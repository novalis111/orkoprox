# Security

orkoprox holds your provider keys, so its security model is structural, not an
afterthought.

## Admin / data-plane separation

The `/v1/admin/*` endpoints (key management, tenant usage, policy reload) and
the `/admin` dashboard are guarded by a dedicated key set, `ADMIN_API_KEYS`,
that is **strictly separate** from the data plane:

- A data-plane key (one in `PROXY_API_KEYS` or a metered key) can **never**
  authenticate on the admin plane — a leaked client key cannot mint or revoke
  keys, or read tenant usage.
- An admin key is **never** accepted on the data plane.
- **Secure by default:** with no admin key configured, the admin plane is
  *disabled* (HTTP 403), not open.
- Configuration validation rejects any overlap between admin and data keys and
  enforces a minimum key length in production.

## Key handling

- Provider keys come from the environment and are **never logged** and never
  stored in plaintext in a database.
- The optional audit log records key **prefixes** and metadata only — never the
  full key, and never prompt or response content (privacy by default).

## Rate limiting

Per-key request-rate and concurrency limits (`RATE_LIMIT_*`) sit alongside the
token budgets to blunt bursts and abuse — token quotas alone don't stop a key
from hammering the gateway.

## Reporting a vulnerability

Please report security issues privately rather than opening a public issue.
See [SECURITY.md](https://github.com/truecode-org/orkoprox/blob/main/SECURITY.md)
in the repository for the disclosure policy and contact.
