# orkoprox

**The LLM gateway you can hand your provider keys to — secure by default,
self-hosted, one container.**

Point any OpenAI-compatible SDK at orkoprox instead of a cloud endpoint. Your
keys stay on your infrastructure, your prompts don't touch a third-party logging
pipeline, and per-key budgets keep a runaway script from running up a bill.

## Quickstart

```bash
docker run --rm -p 8091:8091 \
  -e PROXY_API_KEYS=your-gateway-key-min-40-chars \
  -e OVH_API_KEY=YOUR_PROVIDER_API_KEY \
  -e OVH_BASE_URL=https://your-provider.example.com/v1 \
  ghcr.io/novalis111/orkoprox:latest
```

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8091/v1", api_key="your-gateway-key-min-40-chars")
print(client.chat.completions.create(
    model="chat",  # tier/task alias — orkoprox routes to the configured model
    messages=[{"role": "user", "content": "Hello from orkoprox!"}],
).choices[0].message.content)
```

## What's inside

- **OpenAI-compatible API** — chat (JSON + SSE streaming, tools, vision),
  embeddings, audio, images, rerank.
- **Tier/task routing** with fallback chains and provider cooldown.
- **Per-key budgets** — daily + monthly, token weighting, cost tracking.
- **Budget guardrails** — graceful degrade to a cheaper tier instead of a hard 429.
- **Escalation cascade** — `model="auto"` walks a configured tier list.
- **Drop-in compatibility** — Anthropic (`/v1/messages`) and Ollama (`/api/chat`).
- **Semantic cache** — optional embedding-keyed response cache.
- **Pluggable guard hooks** — PII redaction, content policy, EU-AI-Act tagging.
- **Admin/data-plane separation**, per-key rate limits, append-only audit log.
- **Built-in admin dashboard** at `/admin` — no Grafana required.
- **Prometheus `/metrics`**, health/readiness probes, optional Telegram alerter.

## Documentation

- [Configuration](configuration.md) — every environment variable.
- [Security](security.md) — admin plane, key handling, disclosure policy.

## License

MIT. See the [repository](https://github.com/novalis111/orkoprox).
