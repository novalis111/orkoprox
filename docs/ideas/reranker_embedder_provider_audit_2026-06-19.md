# Deep Audit — Reranker & Embedder bei OVH/Mistral (2026-06-19, live verifiziert)

**Anlass:** Owner-Frage „braucht orkoprox wirklich einen eigenen fetten Reranker-Sidecar, oder gibt es
einen bei OVHcloud/Mistral?" → Live-Audit gegen die echten APIs (nicht Doku).

## Ergebnis: KEIN externer Reranker bei EU-Providern. Embedder voll via OVH.

| Provider | Reranker | Embedder | Beleg |
|---|---|---|---|
| **OVH** (`oai.endpoints.kepler.ai.cloud.ovh.net/v1`) | ❌ **404** | ✅ `bge-m3`, `bge-multilingual-gemma2` (3584d), `Qwen3-Embedding-8B` | Live-curl: `/v1/rerank`, `/rerank`, `/v1/reranking` → `404 unknown_url` |
| **Mistral** (`api.mistral.ai/v1`) | ❌ **404** | ✅ `mistral-embed`, `codestral-embed` | Live-curl: `/v1/rerank`, `/rerank` → `404 no Route matched`; Katalog nur `*-embed`/`*-moderation` |

**OVH-Roadmap-Issue [#1140](https://github.com/ovh/public-cloud-roadmap/issues/1140)** „Add reranking
models to AI Endpoints": **STATE open** (erstellt 2026-04-22, aktualisiert 2026-06-01, kein Milestone).
Wörtlich: *„native support for reranking models … without relying on external providers"* — d.h. OVH hat
2026 **keinen** Reranker, nur eine offene Anfrage. Der HuggingFace-Treffer („OVH als Inference-Provider
für bge-reranker") ist HF-internes Routing, kein nativer OVH-AI-Endpoints-Reranker.

→ **Der orkoprox-Code-Kommentar `app/providers/reranker.py:9` („OVH + Mistral haben KEINEN
Rerank-Endpoint, verifiziert: 404") ist KORREKT — nicht veraltet.**

## Owner-Entscheidung 2026-06-19: Self-hosted Reranker-Sidecar BLEIBT

Begründung: Kein EU-Provider bietet Reranking; US-Provider (Cohere/Together) scheiden wegen
DSGVO/AI-Act aus (genau das, was der Cutover eliminieren soll). Der self-hosted TEI-Sidecar
(`bge-reranker-v2-m3-onnx`, CPU, ~2.3 GB) ist der **einzige compliance-konforme Weg**, `/v1/rerank`
anzubieten. Volle Parität zu tc-llm-proxy. Analog zum Whisper-Sidecar (auch self-hosted).

**Embedder dagegen:** läuft via OVH (`embed → ovh/bge-multilingual-gemma2`, 3584d) — kein Sidecar nötig,
schon korrekt konfiguriert.

**Re-Eval-Trigger:** Wenn OVH-Roadmap #1140 ausliefert → Reranker auf nativen OVH-Endpoint umstellen
(`reranker_base_url`), Sidecar entfernen. Bis dahin: Sidecar ist gewollt, nicht hinterfragen.
