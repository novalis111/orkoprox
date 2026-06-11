# orkoprox

<!-- mood image: see docs/assets/PROMPT.md -->
![orkoprox](docs/assets/orkoprox-hero.png)

**The LLM gateway you can hand your provider keys to — secure by default, self-hosted, one container.**

Point any OpenAI-compatible SDK at orkoprox instead of a cloud endpoint. Your keys stay on your infrastructure. Your prompts don't touch a third-party logging pipeline. Budget guardrails mean no surprise bills.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED.svg?logo=docker&logoColor=white)](Dockerfile)
[![CI](https://img.shields.io/badge/CI-passing-brightgreen.svg)](#)

---

## Why orkoprox

- **Secure by default** — your provider keys never leave your infrastructure; no phone-home telemetry, no third-party logging of your prompts.
- **One container** — `docker run` and you're live. Redis is optional; quota features activate when it's present.
- **True drop-in** — any OpenAI-compatible SDK or tool works unmodified. Just change the `base_url`.
- **Budget guardrails** — per-key daily and monthly limits with cost tracking so a runaway script can't rack up a surprise bill.
- **EU-friendly** — designed for self-hosted deployments in your own data center or cloud region. No data residency surprises.

---

## Quickstart

### One-liner

```bash
docker run --rm \
  -p 8091:8091 \
  -e PROXY_API_KEYS=your-gateway-key-min-40-chars \
  -e OVH_API_KEY=YOUR_PROVIDER_API_KEY \
  -e OVH_BASE_URL=https://your-provider.example.com/v1 \
  ghcr.io/truecode-org/orkoprox:latest
```

### Call it like OpenAI

```bash
curl http://localhost:8091/v1/chat/completions \
  -H "Authorization: Bearer your-gateway-key-min-40-chars" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "chat",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

### Use the OpenAI Python SDK — zero code changes

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8091/v1",
    api_key="your-gateway-key-min-40-chars",
)

response = client.chat.completions.create(
    model="chat",          # tier/task alias — orkoprox routes to the configured model
    messages=[{"role": "user", "content": "Hello from orkoprox!"}],
)
print(response.choices[0].message.content)
```

The `model` field accepts both raw provider model names and **tier/task aliases** (see [How routing works](#how-routing-works)).

---

## Features

### Core (MIT — free for all uses)

- **OpenAI-compatible API** — `/v1/chat/completions` (JSON + SSE streaming, tool calls, vision), `/v1/embeddings`, `/v1/audio/transcriptions`, `/v1/images/generations`, `/v1/rerank`
- **Tier/task alias routing** — send `model="chat"` or `model="reason"` and orkoprox resolves it to the right provider/model. No SDK changes needed when you swap backends.
- **Fallback chain + provider cooldown** — if the primary backend is unhealthy, traffic shifts to the fallback automatically. Cooldown windows prevent thundering-herd retries.
- **Per-key quotas** — daily and monthly budget limits per API key. Token weighting makes expensive models consume more "virtual tokens" so you control costs accurately.
- **Cost tracking** — EUR/USD cost attribution per request and per key. Quota-status headers (`X-Orkoprox-Quota-Status: ok|warn|critical|exceeded`) on every response.
- **Content moderation guard** — pluggable pre/post filter (fail-open configurable). Built for EU AI Act compliance.
- **Provider quirk repair** — handles `reasoning→content` lifting, empty-content edge cases, and configurable retry/backoff so your code doesn't need to.
- **Prometheus `/metrics`** — latency histograms, token counts, error rates, quota hits. Drop it into any existing monitoring stack.
- **Health probes** — `/health`, `/ready`, `/v1/healthz` (deep health check with backend connectivity).
- **Optional Telegram alerter** — notifies you when a backend goes down or latency spikes. Zero config if you don't want it.
- **Brandable response headers** — `BRAND_HEADER_PREFIX` lets you run orkoprox behind your own product name. Default: `X-Orkoprox-*`.
- **Key management API** — `/v1/admin/metering/keys` for quota inspection and key lifecycle.

---

## Configuration

Copy `.env.example` to `.env` and edit. All settings are environment variables.

| Variable | Default | Description |
|---|---|---|
| `PORT` | `8091` | Port orkoprox listens on |
| `PROXY_AUTH_REQUIRED` | `true` | Require API key on every request |
| `PROXY_API_KEYS` | _(required)_ | Comma-separated gateway keys (≥40 chars in production) |
| `PRIMARY_PROVIDER` | `ovh` | Primary upstream provider |
| `OVH_API_KEY` | _(required)_ | API key for your primary provider |
| `OVH_BASE_URL` | _(required)_ | Base URL of your primary provider's OpenAI-compatible API |
| `FALLBACK_PROVIDERS` | `ovh` | Comma-separated fallback chain (the `stub` provider returns 503, useful for tests) |
| `REDIS_URL` | _(optional)_ | Redis connection string — enables per-key quotas and health caching |
| `GUARD_ENABLED` | `true` | Enable content moderation pre/post filter |
| `GUARD_BYPASS_KEYS` | _(optional)_ | Keys that skip the guard (internal service accounts) |
| `METRICS_ENABLED` | `true` | Expose `/metrics` Prometheus endpoint |
| `BRAND_HEADER_PREFIX` | `X-Orkoprox` | Response header prefix — rename for white-label deployments |
| `PRODUCTION_ALLOWED_PROVIDERS` | _(optional)_ | Allowlist of providers permitted in production environments |
| `ALERT_TELEGRAM_BOT_TOKEN` | _(optional)_ | Telegram bot token for alerting |
| `ALERT_TELEGRAM_CHAT_ID` | _(optional)_ | Telegram chat/channel ID for alerts |

See `.env.example` for the full list including all `MODEL_ALIAS_*` and reranker settings.

---

## How routing works

Clients send a `model` value. orkoprox resolves it through three layers:

1. **Exact match** — if `model` matches a known provider model name, it routes directly.
2. **Task alias** — human-readable task names like `chat`, `reason`, `vision`, `embed`, `voice`. Each maps to a provider/model configured via `MODEL_ALIAS_*` env vars.
3. **Tier alias** — quality tiers `xhigh`, `high`, `medium`, `low` for when you want quality-based routing without naming specific models.

```
client: model="reason"
   → MODEL_ALIAS_REASON=your-provider/your-model
   → POST https://your-provider.example.com/v1/chat/completions
       Authorization: Bearer $OVH_API_KEY
```

If the primary provider fails, orkoprox checks the **fallback chain** (`FALLBACK_PROVIDERS`). Failed providers enter a **cooldown window** (`PROVIDER_COOLDOWN_SECONDS`) before being retried.

Built-in aliases: `xhigh`, `high`, `medium`, `low`, `reason_lite`, `reason_mid`, `long_context`, `classify`, `extract`, `compose`, `chat`, `reason`, `report`, `ocr`, `vision`, `vision_x`, `image`, `voice`, `voice_hq`, `embed`.

---

## Per-key budgets & quotas

When Redis is configured, orkoprox tracks spend per API key.

- **Daily and monthly limits** — set via the key management API or via env-level defaults.
- **Token weighting** — a `MODEL_ALIAS_XHIGH` model can be configured to consume more "virtual tokens" per request, so your quota reflects real cost rather than raw token count.
- **Quota status headers** — every response carries `X-Orkoprox-Quota-Status` (`ok` / `warn` / `critical` / `exceeded`), `X-Orkoprox-Usage-Pct`, and cost/usage breakdowns for daily and monthly windows.
- **Hard stop on exceeded** — requests from a key with an exhausted budget get a `429` with a clear message. No partial charges.

---

## Roadmap

These features are planned and in progress — not yet in the current release:

- **Redis-optional mode** — SQLite or in-memory fallback so Redis is never a hard dependency.
- **Declarative YAML/TOML policy** — routing rules, quotas, and guard config as files with hot-reload.
- **Budget graceful degrade** — instead of a hard 429, downgrade to a cheaper model tier before rejecting.
- **Server-side escalation cascade** — automatic quality-tier escalation with cost cap.
- **Semantic cache** — deduplicate semantically equivalent requests to cut costs.
- **Anthropic / Ollama compat shapes** — first-class support for non-OpenAI provider wire formats.
- **Built-in admin dashboard** — lightweight web UI for quota inspection and key management.

---

## Open Core

orkoprox is **MIT-licensed** — the full core including budget guardrails, content guard, routing engine, and (upcoming) admin dashboard is free for personal and commercial use.

An **Enterprise module** (separate, commercial) adds: SSO/SAML, multi-tenant cost-center accounting, multi-region HA configuration, and a compliance pack with signed audit exports and SLA support.

The MIT core is not crippled. You can run it in production at scale without the Enterprise module.

---

## Security

This gateway holds your provider keys. Please read [SECURITY.md](SECURITY.md) before deploying.

Responsible disclosure matters — if you find a vulnerability (especially in key handling or the admin/data-plane boundary), please report it privately rather than opening a public issue. Details in [SECURITY.md](SECURITY.md).

---

## Contributing

Contributions welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup, coding style, and the PR process.

---

## Code of Conduct

This project follows the [Contributor Covenant v2.1](CODE_OF_CONDUCT.md).

---

## License

[MIT](LICENSE) © 2026 TrueCode
