# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.2]

Custom providers + dashboard smoke test (see entries below).

### Added
- **Custom OpenAI-compatible providers** via `CUSTOM_PROVIDERS` (JSON): register
  any number of additional backends — Baseten, Groq, Together, … — without a code
  change. Usable in `PRIMARY_PROVIDER`, `FALLBACK_PROVIDERS`, and as a model-alias
  prefix (`baseten/<model>`), exactly like the built-in providers.
- **Dashboard smoke test**: a one-click "ask the switchboard" panel in `/admin`
  that fires one real completion through routing → provider → model and shows the
  answer, provider, model, latency and token usage. Empty prompt → the model tells
  a one-liner about being a proxied LLM. Admin-plane only.

## [0.1.1]

Hardening release — no breaking changes.

### Security
- Data-plane API-key validation now uses a constant-time comparison
  (`hmac.compare_digest`) instead of plain set membership, matching the admin
  plane and removing a key-timing side channel.
- Added a request body-size limit (`MAX_REQUEST_BODY_BYTES`, default 10 MiB)
  returning `413` before oversized payloads are buffered.

### Changed
- All GitHub Actions are now pinned to commit SHAs (with version comments) in
  both CI and release workflows, closing a supply-chain path to the signing /
  registry tokens.
- The Docker base image is pinned to a digest for reproducible builds.
- CI now runs a Trivy filesystem + config + secret scan in addition to the
  image scan.

### Docs
- SECURITY.md: guidance to use file-mounted secrets over plain env vars for
  provider keys, plus notes on the body-size cap and cosign verification.

## [0.1.0]

Initial public release of **orkoprox** — an OpenAI-compatible, self-hosted LLM
gateway. Secure by default, one container.

### Added
- OpenAI-compatible endpoints: `/v1/chat/completions` (JSON + SSE streaming,
  tool calls, vision), `/v1/embeddings`, `/v1/audio/transcriptions`,
  `/v1/images/generations`, `/v1/rerank`, `/v1/models`.
- Tier/task alias routing (`chat`, `reason`, `vision`, `xhigh`, …) with
  fallback chains and provider cooldown.
- Per-key quotas: daily and monthly budgets, token weighting, EUR/USD cost
  tracking, and quota-status response headers.
- **Admin / data-plane separation** (`ADMIN_API_KEYS`): admin endpoints are
  strictly separate from data-plane keys; secure by default (disabled if unset).
- **Per-key rate + concurrency limiting** and an **append-only audit log**
  (key prefixes only, never prompt content).
- **Zero-config mode**: metering/quotas work without Redis (in-process fallback).
- **Declarative TOML policy** (`POLICY_FILE`) with hot-reload via the admin API.
- **Budget guardrails with graceful degrade** and a **server-side escalation
  cascade** (`model="auto"`).
- **Drop-in compatibility endpoints**: Anthropic (`/v1/messages`) and Ollama
  (`/api/chat`).
- **Semantic response cache** (optional, embedding-keyed, off by default).
- **Pluggable guard hooks** (PII redaction, EU-AI-Act tagging, custom hooks).
- Content-moderation guard (configurable pre/post filter, fail-open option).
- Provider quirk repair (reasoning→content lifting, empty-content handling,
  retry/backoff).
- **Built-in admin dashboard** at `/admin` (no Grafana required).
- Prometheus `/metrics`, health/readiness probes, deep health check, optional
  Telegram alerter.
- Configurable response-header namespace via `BRAND_HEADER_PREFIX`.
- Optional production provider allowlist via `PRODUCTION_ALLOWED_PROVIDERS`.
- Public CI (ruff + pyright + pytest + docker build + Trivy scan), multi-arch
  release images (amd64 + arm64) to GHCR with cosign signing and an SBOM.
