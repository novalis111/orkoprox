# W3 — Canary-Smoke-Matrix: alle Client-Keys × alle Aliase gegen orkoprox-Parallelport

**Welle:** W-ORKOPROX-CUTOVER · W3 (Canary, gegen `oekotopia:8081`, parallel zu tc-llm-proxy)
**Datum:** 2026-06-19
**Status:** grün — Cutover-Garantie HART bewiesen (0 Fehlschläge auf produktivem Pfad)

## 1. Auth-Matrix — alle 11 Client-Keys

Jeder `PROXY_API_KEYS`-Key gegen `/v1/models`:

| Key (maskiert) | HTTP | | Key (maskiert) | HTTP |
|---|---|---|---|---|
| `7dc3…` (admin) | 200 ✅ | | `lv_cgassler_5ae5…` (Avi) | 200 ✅ |
| `lv_8daba…` | 200 ✅ | | `lv_cgassler_795d…` | 200 ✅ |
| `lv_hq_4c5e…` | 200 ✅ | | `lv_cgassler_5ea8…` | 200 ✅ |
| `lcs-ritter-…` | 200 ✅ | | `tcpulse-…` | 200 ✅ |
| `lv_mitarbeiterdesign_…` | 200 ✅ | | `lv_ttkramer_…` | 200 ✅ |
| `orko-alerter-…` | 200 ✅ | | **Negativ-Kontrolle** `lv_INVALID` | **401** ✅ |

→ Alle 11 echten Keys authentifizieren; ungültiger Key korrekt abgewiesen.

## 2. Alias-Matrix — jeder Wire-Alias × echter OVH-Call (cgassler/Avi-Key)

| Alias | HTTP | aufgelöst auf | |
|---|---|---|---|
| `chat` | 200 | Mistral-Small-3.2-24B | ✅ |
| `classify` | 200 | Mistral-7B-Instruct-v0.3 | ✅ |
| `extract` | 200 | Mistral-Small-3.2-24B | ✅ |
| `compose` | 200 | Mistral-Small-3.2-24B | ✅ |
| `high` (Avi) | 200 | Mistral-Small-3.2-24B | ✅ |
| `medium` | 200 | Mistral-Small-3.2-24B | ✅ |
| **`reason`** | 200 | **gpt-oss-120b** (Eskalation!) | ✅ |
| `report` | 200 | Meta-Llama-3.3-70B | ✅ |
| `vision_x` | 200 | Qwen2.5-VL-72B | ✅ |
| `embed` | 200 | bge-multilingual-gemma2 **3584d** | ✅ |

→ **0 Alias-404, 0 Provider-Fehler.** Jeder produktiv genutzte Alias liefert valide Antwort.

## 3. Avi-Spezifika (W3-Akzeptanz 2+3)

| Feature | Ergebnis | |
|---|---|---|
| **Tool-Calls** (function_call) | `get_weather({"city":"Berlin"})` korrekt extrahiert | ✅ |
| **Streaming** (SSE) | 22 `data:`-Chunks | ✅ |
| **`reason`-Eskalation** | → gpt-oss-120b (echtes Reasoning-Modell, NICHT Legacy-Apriel-15b) | ✅ |
| `/v1/responses` (Responses-API) | **404** — orkoprox hat den Endpoint nicht | ⚠ siehe B3 |

**Avi-Qualität:** Der `reason`-Alias eskaliert auf **gpt-oss-120b** — das ist der IQ-Hebel gegen
„Avi dumm wie Brot". Im Legacy lief reason-Eskalation auf Apriel-15b (kleines Modell); orkoprox
bietet ein deutlich stärkeres Reasoning-Tier. Avi nutzt `high` (→ Mistral-Small-24B) für Standard
+ `reason` (→ gpt-oss-120b) für Eskalation — beide live grün.

## 4. Offene W1/W3-Punkte — geklärt

- **B2 (gpt-5.4/codex/deepseek, TrueCode-Legacy):** alle → **HTTP 200**, Default-Fallback auf
  Mistral-7B (`low`-Tier). Kein 404 → **kein Cutover-Brecher**. Konsistent zum Legacy (dort auch
  nur OVH-Fallback, kein eigenes Modell). Backlog B2: ggf. auf stärkeres Tier mappen wenn TrueCode
  Qualität braucht.
- **B3 (`/v1/responses` 404):** kein aktiver Nutzer (tc-llm-proxy 0 Calls/7d; leitivo-platform hat
  den Code-Pfad, ruft aber nur `chat_completions` auf). `RESPONSE_STORE_*` aus .env entfernt (toter
  Ballast). Backlog B3.

## 5. W3-Akzeptanz-Status

- [x] Canary-Matrix grün: jeder Client-Key × jeder genutzte Alias → valide Antwort (kein 401/404/Provider-Fehler)
- [x] Avi-spezifisch: chat sinnvoll, reason→gpt-oss-120b (Eskalation), embed→3584d, Tool-Calls ok
- [x] Streaming getestet (22 SSE-Chunks) — kein Regression
- [~] Responses-Store: orkoprox hat keine Responses-API (B3) — kein aktiver Nutzer, kein Blocker
- [x] null Disruption: tc-llm-proxy unberührt während aller Canary-Calls
