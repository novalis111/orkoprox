# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial open-source release of **orkoprox** — an OpenAI-compatible,
  self-hosted LLM gateway.
- OpenAI-compatible endpoints: `/v1/chat/completions` (JSON + SSE streaming,
  tool calls, vision), `/v1/embeddings`, `/v1/audio/transcriptions`,
  `/v1/images/generations`, `/v1/rerank`, `/v1/models`.
- Tier/task alias routing (`chat`, `reason`, `vision`, `xhigh`, …) with
  fallback chains and provider cooldown.
- Per-key quotas: daily and monthly budgets, token weighting, EUR/USD cost
  tracking, and quota-status response headers.
- Content moderation guard (configurable pre/post filter, fail-open option).
- Provider quirk repair (reasoning→content lifting, empty-content handling,
  retry/backoff).
- Prometheus `/metrics`, health/readiness probes, deep health check, and an
  optional Telegram alerter.
- Configurable response-header namespace via `BRAND_HEADER_PREFIX`
  (default `X-Orkoprox`).
- Optional production provider allowlist via `PRODUCTION_ALLOWED_PROVIDERS`.

## [0.1.0] - TBD

- First tagged public release.
