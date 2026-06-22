# Multi-Provider-Failover (OVH → konfigurierbarer Sekundär-Provider)

> Status: implementiert auf `feature/orkoprox-multi-provider-failover` (2026-06-22).
> Owner-Direktive (KRITISCH, 2026-06-22): OVHcloud hat Server-Outages. Ein
> OVH-Ausfall legt aktuell Avi + Kalia + JEDE LLM-Funktion für ALLE Tenants
> lahm. Wir brauchen einen konfigurierbaren Auto-Failover auf einen anderen
> Provider + Model, der Kosten/Token korrekt mitabrechnet.

## TL;DR — was geändert wurde

orkoprox **hatte schon** die komplette Failover-Maschinerie (Fallback-Chain pro
Route, Circuit-Breaker/Cooldown, Retry+Backoff, provider-spezifisches Pricing).
Was **fehlte**, war:

1. Die Default-Config zeigte alle Fallback-Chains auf **OVH selbst**
   (`FALLBACK_PROVIDERS=ovh`, `FALLBACK_PROVIDERS_*=ovh`) → **kein** echter
   Cross-Provider-Failover. Ein OVH-Ausfall hatte kein Ausweichziel.
2. Bei einem Cross-Provider-Fallback wählte `_model_for_candidate()` stumpf den
   **Provider-Default** des Sekundärs (`mistral_lp_default_model`,
   "mistral-small-latest"). Es gab **kein Tier→Fallback-Model-Mapping** — d.h.
   `reason` (gpt-oss-120b) wäre auf Mistral-Small ausgewichen statt auf das vom
   Owner gewünschte **Mistral-Large**.

Beides ist jetzt behoben — additiv, ohne Breaking Change an API/Response-Form.

## Bestand (NICHT neu gebaut — bereits vorhanden, verifiziert)

| Baustein | Ort | Status |
|---|---|---|
| Provider-Abstraktion (OpenAI-kompatibel, erweiterbar) | `app/providers/openai_compatible.py`, `CUSTOM_PROVIDERS` | ✓ |
| Fallback-Chain pro Route | `router._fallback_chain_for_route`, `config.fallback_provider_list_for_route` | ✓ |
| Circuit-Breaker / Cooldown (scope- + error-typ-differenziert) | `router._start_cooldown`, `_is_cooling_down`, `_cooldown_seconds_for` | ✓ |
| Retry + exponentielles Backoff (mit Cap) | `router._run_with_retry`, `provider_retry_backoff_*` | ✓ |
| Capability-Matching (Vision/Tools/Stream) | `router._provider_missing_capabilities`, `_model_missing_capabilities` | ✓ |
| Provider-spezifisches Pricing + Token-Weighting | `app/providers/ovh_pricing.py`, `app/providers/mistral_lp_pricing.py` | ✓ |
| Abrechnung pro **tatsächlich genutztem** Provider/Model | `token_metering.record_usage` (wählt OVH- vs Mistral-Pricing) | ✓ |
| Router gibt tatsächlich genutztes `provider.name` + `model` zurück | `router.chat_completions` / `chat_completions_stream` / `embeddings` / `rerank` | ✓ |

**Schlüssel-Erkenntnis Abrechnung:** Der Router liefert an `main.py`
`(provider_name, resolved_model, data, route_debug)` — und zwar den *real
verwendeten* Provider + Modell (nach Failover). `main.py` reicht exakt diese an
`metering_service.record_usage(model=resolved_model, provider=provider_name)`
durch. `record_usage` entscheidet anhand `provider == "mistral_lp"` bzw.
`mistral_lp_is_priced(model)` selbst, ob Mistral- oder OVH-Pricing +
-Token-Weighting greift. **Damit läuft die Kosten-/Token-Abrechnung beim
Failover automatisch provider-korrekt** — Voraussetzung war nur, dass das
Fallback-Modell ein in der Mistral-Pricing-Map bekanntes Modell ist (z.B.
`mistral-large-latest`, `mistral-small-latest`). Das stellt das neue
Tier→Model-Mapping sicher.

## Lücke → Lösung

### 1. Cross-Provider-Fallback aktivierbar machen

Per ENV / Policy-TOML wird die Fallback-Chain pro Tier auf einen Sekundär-
Provider erweitert, z.B.:

```
FALLBACK_PROVIDERS_CHAT=ovh,mistral_lp
FALLBACK_PROVIDERS_REASON=ovh,mistral_lp
```

Die Chain-Mechanik (`fallback_provider_list_for_route`) existierte bereits — sie
musste nur ein echtes zweites Ziel bekommen. Default bleibt konservativ
(`ovh`), damit Deployments ohne gesetzten Mistral-Key sich nicht ändern.

### 2. Tier→Fallback-Model-Mapping (`MODEL_ALIAS_FALLBACK_<TIER>`)

Neu: pro Tier/Task-Alias ein **explizites Fallback-Ziel** in der Form
`provider/model`. `_model_for_candidate()` konsultiert dieses Mapping, bevor es
auf den Provider-Default zurückfällt.

```
MODEL_ALIAS_FALLBACK_CHAT=mistral_lp/mistral-small-latest
MODEL_ALIAS_FALLBACK_REASON=mistral_lp/mistral-large-latest
MODEL_ALIAS_FALLBACK_XHIGH=mistral_lp/mistral-large-latest
```

Auflösungs-Reihenfolge in `_model_for_candidate` (Cross-Provider-Fall):

1. **`MODEL_ALIAS_FALLBACK_<route_key>`** — wenn gesetzt UND der Provider-Prefix
   zum aktuellen Candidate-Provider passt → dieses Modell.
2. sonst: bisheriges Verhalten (`_default_model_for_provider`, prefer_fast /
   is_vision wie gehabt).

Das Mapping folgt exakt der bestehenden `model_alias_*`-Konvention, ist also
automatisch über die Policy-TOML (`[aliases] fallback_reason = "..."`)
hot-reloadbar — kein Sonderpfad.

### 3. Cooldown-Tuning bei echtem Failover

`provider_cooldown_seconds` (Default 5s) war explizit auf Single-Provider
("no failover target") getuned. Bei echtem Cross-Provider-Failover ist ein
längerer Primär-Cooldown sinnvoll, damit ein flapping-OVH nicht ständig
zurückgezogen wird, während Traffic sauber auf dem Sekundär läuft (Recovery
nach Ablauf des Cooldowns automatisch — der nächste Request probiert OVH wieder,
weil OVH als preferred Provider an Index 0 der Chain steht). Default bleibt
unverändert (Backward-Compat); für Failover-Deployments wird der empfohlene Wert
in `.env.example` dokumentiert.

## Recovery (automatisch)

Der Cooldown ist zeitbasiert (`cooldown_until`). Nach Ablauf ist OVH (Index 0
der Chain) wieder der erste Candidate jedes Requests. Ein erfolgreicher
OVH-Call beendet den Failover-Zustand implizit — es gibt keinen klebrigen
"wir-sind-jetzt-auf-Mistral"-Modus. Das ist die gewünschte Self-Healing-
Semantik: sobald OVH gesund ist, fließt Traffic ohne Eingriff zurück.

## Was NICHT geändert wurde (Stabilität)

- API-/Response-Form: unverändert. Clients sehen weiter OpenAI-kompatible
  Responses; das genutzte Modell steht wie immer in `data["model"]` /
  `route_debug`.
- Default-Provider bleibt OVH. Ohne gesetzten `MISTRAL_LP_API_KEY` +
  `FALLBACK_PROVIDERS_*`-Override ist das Verhalten **bitidentisch** zu vorher.
- Kein neuer Pflicht-Dependency, kein DB-Schema-Change.

## Konfiguration — Quickstart (Owner-Default)

```bash
# 1. Sekundär-Provider-Key setzen (über bestehende Secret-Mechanik / .env):
MISTRAL_LP_API_KEY=...

# 2. Cross-Provider-Fallback pro Tier aktivieren:
FALLBACK_PROVIDERS_CHAT=ovh,mistral_lp
FALLBACK_PROVIDERS_REASON=ovh,mistral_lp
FALLBACK_PROVIDERS_XHIGH=ovh,mistral_lp
FALLBACK_PROVIDERS_LONG_CONTEXT=ovh,mistral_lp

# 3. Fallback-Zielmodelle pro Tier (sonst Provider-Default):
MODEL_ALIAS_FALLBACK_CHAT=mistral_lp/mistral-small-latest
MODEL_ALIAS_FALLBACK_REASON=mistral_lp/mistral-large-latest
MODEL_ALIAS_FALLBACK_XHIGH=mistral_lp/mistral-large-latest
MODEL_ALIAS_FALLBACK_LONG_CONTEXT=mistral_lp/mistral-large-latest

# 4. (Optional, empfohlen für Failover) längerer Primär-Cooldown:
PROVIDER_COOLDOWN_SECONDS=30
```

Beliebige Provider sind möglich (nicht hart auf Mistral): jeder via
`CUSTOM_PROVIDERS` registrierte OpenAI-kompatible Backend kann als
`FALLBACK_PROVIDERS_*`-Ziel und `MODEL_ALIAS_FALLBACK_*`-Prefix dienen. Für
korrekte Abrechnung muss das Ziel-Modell entweder in einer Pricing-Map stehen
(OVH/Mistral) oder über `CUSTOM_PROVIDERS` eingebracht werden — sonst wird der
Call mit 0-Kosten getrackt (Token-Count läuft trotzdem mit).
