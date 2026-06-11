# Configuration

All configuration is via environment variables. Copy `.env.example` to `.env`
and edit, or pass `-e` flags to `docker run`. Only the variables you set
override the built-in defaults.

## Server

| Variable | Default | Description |
|---|---|---|
| `HOST` | `0.0.0.0` | Listen address |
| `PORT` | `8091` | Listen port |
| `LOG_LEVEL` | `INFO` | Log level |
| `LOG_JSON` | `true` | Structured JSON logging |
| `ENVIRONMENT` | `development` | `production` enables boot validators |

## Authentication & headers

| Variable | Default | Description |
|---|---|---|
| `PROXY_AUTH_REQUIRED` | `true` | Require an API key on every request |
| `PROXY_API_KEYS` | _(required)_ | Comma-separated gateway keys (≥40 chars in prod) |
| `ADMIN_API_KEYS` | _(optional)_ | Admin-plane keys (`/v1/admin/*`, `/admin`). Empty = admin disabled |
| `BRAND_HEADER_PREFIX` | `X-Orkoprox` | Response-header namespace |

## Providers & routing

| Variable | Default | Description |
|---|---|---|
| `PRIMARY_PROVIDER` | `ovh` | Primary upstream provider |
| `OVH_API_KEY` | _(required)_ | API key for the primary provider |
| `OVH_BASE_URL` | _(provider URL)_ | OpenAI-compatible base URL of the provider |
| `FALLBACK_PROVIDERS` | `ovh` | Comma-separated fallback chain |
| `PRODUCTION_ALLOWED_PROVIDERS` | _(optional)_ | If set, only these providers may be primary in production |
| `MODEL_ALIAS_*` | _(see .env.example)_ | Map a tier/task alias to a `provider/model` |

## Limits, budgets & guardrails

| Variable | Default | Description |
|---|---|---|
| `RATE_LIMIT_PER_MINUTE` | `0` | Per-key request rate limit (0 = off) |
| `RATE_LIMIT_BURST` | `0` | Burst allowance on top of the rate |
| `RATE_LIMIT_CONCURRENCY` | `0` | Per-key in-flight request limit (0 = off) |
| `BUDGET_DEGRADE_ALIAS` | _(optional)_ | On budget exhaustion, downgrade to this alias instead of 429 |
| `ESCALATION_TRIGGER_MODEL` | `auto` | Model name that triggers the escalation cascade |
| `ESCALATION_CASCADE` | _(optional)_ | Comma-separated tiers to walk (e.g. `low,chat,xhigh`) |

## Cache, hooks & guard

| Variable | Default | Description |
|---|---|---|
| `SEMANTIC_CACHE_ENABLED` | `false` | Embedding-keyed response cache |
| `SEMANTIC_CACHE_THRESHOLD` | `0.95` | Cosine threshold for a cache hit |
| `SEMANTIC_CACHE_MAX_ENTRIES` | `1000` | LRU cap |
| `SEMANTIC_CACHE_TTL_SECONDS` | `3600` | Entry TTL (0 = no expiry) |
| `GUARD_HOOKS` | _(optional)_ | Pre/post hooks, e.g. `pii_redact,ai_act_tag` |
| `GUARD_ENABLED` | `true` | Content-moderation pre/post filter |

## Storage & observability

| Variable | Default | Description |
|---|---|---|
| `REDIS_URL` | _(optional)_ | Redis for shared metering. Empty = in-process fallback |
| `POLICY_FILE` | _(optional)_ | TOML policy file (aliases + limits + quotas), hot-reloadable |
| `AUDIT_LOG_ENABLED` | `false` | Append-only audit log (key prefixes only) |
| `METRICS_ENABLED` | `true` | Expose `/metrics` (Prometheus) |
| `ALERT_TELEGRAM_BOT_TOKEN` | _(optional)_ | Telegram alerting |

See `.env.example` in the repository for the complete list.
