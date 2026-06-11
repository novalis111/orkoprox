# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
