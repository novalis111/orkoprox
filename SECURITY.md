# Security Policy

orkoprox sits between your applications and your LLM provider keys. That makes key handling, the admin/data-plane boundary, and injection surface areas especially sensitive. Please take this policy seriously.

---

## Supported Versions

| Version | Supported |
|---------|-----------|
| `main` (latest) | ✅ |
| older tags | security fixes on request |

---

## Reporting a Vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

### Preferred: GitHub Security Advisories

Use the private disclosure flow built into GitHub:

1. Go to the repository's **Security** tab.
2. Click **"Report a vulnerability"**.
3. Fill in the advisory draft — include reproduction steps, impact assessment, and affected versions.

We will acknowledge within **48 hours** and aim to release a fix within **14 days** for critical issues.

### Alternative: Email

Send details to **security@example.com** (PGP available on request).

---

## Response Timeline

| Severity | Acknowledgement | Target fix |
|---|---|---|
| Critical (key exposure, auth bypass) | 24 h | 7 days |
| High (data plane escape, injection) | 48 h | 14 days |
| Medium / Low | 72 h | 30 days |

We will coordinate a CVE assignment and disclosure date with you before publishing.

---

## Scope

Issues we consider in scope:

- **Key handling** — provider API keys stored in ENV, forwarded in headers, or written to logs. Any path where a key could be exposed to an unauthorized party.
- **Authentication bypass** — circumventing `PROXY_AUTH_REQUIRED` or the key-validation logic to reach upstream providers without a valid gateway key.
- **Admin / data-plane boundary** — the `/v1/admin/*` endpoints should be inaccessible without a privileged key. Any bypass counts.
- **Prompt injection through the guard layer** — content that causes the moderation guard to produce a false-negative on clearly harmful input.
- **Redis key namespace collision** — quota or health data from one tenant/key visible to or writable by another.
- **Dependency vulnerabilities** — known CVEs in `fastapi`, `uvicorn`, `httpx`, `pydantic`, `redis` that affect orkoprox's attack surface.

Out of scope:

- Denial-of-service via extremely large payloads (use your own rate-limiting layer in front of the gateway).
- Issues that require physical access to the host.
- Social engineering.

---

## Key Handling Commitment

- Provider keys are **never logged**, not even at DEBUG level.
- Provider keys are **never included in response bodies or error messages**.
- The `PROXY_API_KEYS` gateway keys are hashed before comparison — the raw value is not retained in memory beyond the initial validation.
- orkoprox has **no phone-home telemetry** — no usage data is sent to any external service.

---

## Deployment Hardening Notes

- Run behind a TLS-terminating reverse proxy. Do not expose port 8091 directly to the internet.
- Use `PROXY_AUTH_REQUIRED=true` (the default). Disable only in isolated local dev environments.
- For cross-host Redis, use `rediss://` (TLS) — see `.env.example`.
- Rotate `PROXY_API_KEYS` if a key is suspected compromised. Redis quota state is keyed by the hash — old hash entries will expire naturally.
