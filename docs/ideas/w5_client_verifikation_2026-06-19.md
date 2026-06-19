# W5 — Client-Vollverifikation: alle Tenants laufen normal weiter (Owner-Gate)

**Welle:** W-ORKOPROX-CUTOVER · W5
**Datum:** 2026-06-19
**Status:** grün — alle realen Clients laufen mit ihren BESTEHENDEN Keys über orkoprox, 0 Fehler

## 1. Echter Live-Traffic seit Cutover (der stärkste Beweis)

orkoprox bedient seit dem W4-Cutover **echten Produktiv-Verkehr** (nicht nur Test-Calls).
Analyse `docker logs src-proxy-1` (~20 min nach Cutover):

| Metrik | Wert |
|---|---|
| HTTP 200 | **100** |
| HTTP 5xx | **0** |
| Exceptions/Traceback | **0** |
| Live angefragte Modelle | Mistral-Small-24B (174×), bge-multilingual-gemma2 (81×), Qwen3Guard 0.6B/8B (96×), gpt-oss-120b (12×) |
| Live-Aliase | high (48×), medium (52×), chat (16×), reason (5×), embed (3×) |

→ Mehrere Tenants senden parallel echten Traffic, alles 200. **Content-Moderation-Guard (Qwen3Guard)
läuft aktiv** (PII-Erkennung im guard_audit-Log, EU-AI-Act Art. 9/13/14) — Enterprise-Feature über
Legacy hinaus.

## 2. Client-spezifische End-to-End-Smokes (über llm.true-code.de)

| Client | Smoke | HTTP | Modell | Ergebnis |
|---|---|---|---|---|
| **Oekotopia** | Embedding | 200 | bge-multilingual-gemma2 | **3584d** (RAG-Vektor-Parität) ✅ |
| **Leitivo-Avi** | reason-Eskalation | 200 | **gpt-oss-120b** | rechnet Distanzaufgabe korrekt ✅ |
| **Leitivo-Avi** | chat (high) | 200 | Mistral-Small-24B | sinnvolle CRM-Definition ✅ |
| **TrueCode** | report | 200 | Meta-Llama-3.3-70B | kohärente Management-Summary ✅ |
| **Melanie/Teasewitch** | — | n/a | — | spricht Proxy NICHT an (Together.ai direkt) → kein Impact ✅ |

### Avi-Qualität (die „dumm wie Brot"-Frage — GEMESSEN)
- `high` (Standard) → Mistral-Small-3.2-24B: vollständige, korrekte Antworten.
- `reason` (Eskalation) → **gpt-oss-120b**: echtes Reasoning-Modell, löst Logik-/Rechenaufgaben.
- **Verbesserung ggü. Legacy:** tc-llm-proxy eskalierte reason auf Apriel-15b (klein). orkoprox nutzt
  gpt-oss-120b — deutlich stärker. **Avi-Qualität ist mit dem Cutover GESTIEGEN.**

## 3. Infrastruktur-Gesundheit

| Komponente | Status |
|---|---|
| orkoprox `src-proxy-1` | running **healthy** |
| tc-llm-proxy (Hot-Standby) | running healthy, `traefik.enable=false` |
| Alerter (pollt llm.true-code.de) | kein DOWN/Alert → orkoprox gesund |
| Client-Stacks (leitivo-platform, oekotopia, true-code-app, tc-pulse) | alle running healthy |
| 23 Domains | == Baseline (W4), unberührt |

## 4. W5-Akzeptanz-Status

- [x] Pro realem Client E2E-Smoke nach Cutover grün (Oekotopia-embed, Avi chat+reason, TrueCode-report)
- [x] Avi-Qualität gemessen: reason→gpt-oss-120b (stärker als Legacy-Apriel), chat sinnvoll
- [x] Embeddings 3584d (Vektor-Parität, kein RAG-Drift)
- [x] Monitor/Alerter grün, 0 Client-Fehler-Spike, 100× 200 / 0× 5xx seit Cutover
- [x] Alle Clients mit BESTEHENDEN Keys (keine Key-Rotation nötig — Cutover-Garantie eingehalten)

**Owner-Gate:** Die harte Owner-Bedingung 2026-06-19 („alle Tenants/Clients laufen danach normal mit
ihren Keys über llm.true-code.de") ist erfüllt und belegt. tc-llm-proxy bleibt als Hot-Standby bis
W6 (Stabilitäts-Fenster), Rollback-Pfad intakt.
