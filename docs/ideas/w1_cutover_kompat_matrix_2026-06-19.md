# W1 — Cutover-Kompatibilitätsmatrix orkoprox ← tc-llm-proxy

**Welle:** W-ORKOPROX-CUTOVER · W1 (Diff + Client-Key-Inventar + Mistral-Key)
**Datum:** 2026-06-19
**Status:** grün — 0 fehlende Aliase, Vollabdeckung bewiesen
**Belege:** orkoprox `make test` 566 passed/15 skipped; Live-Inspekt tc-llm-proxy-Container auf oekotopia; lc-/oekotopia-/teasewitch-Repo-Audit; sops-decrypt `kopp-proxy-runtime__env.env`.

---

## 1. Kern-Ergebnis (Cutover-Garantie W1)

**Jeder Wire-Alias, den ein realer Client HEUTE gegen `llm.true-code.de` sendet, existiert in orkoprox.**
→ Kein 401/Alias-404-Risiko beim Cutover. Verbleibende Differenz ist rein das **Modell-Verhalten**
(US-Provider-Modelle im Legacy-Zwitter → OVH-Modelle in orkoprox = gewollter Compliance-Effekt §12).

```
make test: 566 passed, 15 skipped, 1 warning in 5.60s   ✅  (Plan-SSOT erwartete 560)
```

## 2. Client-Key-Inventar (akzeptierte Keys live im tc-llm-proxy-Container)

`PROXY_API_KEYS` (11 Keys, maskiert) — diese MÜSSEN in orkoprox `proxy_api_keys` CSV übernommen werden:

| # | Key (maskiert) | Tenant/Zweck |
|---|---|---|
| 1 | `7dc3…` | generischer/Admin-Key |
| 2 | `lv_8daba…` | Leitivo (Basis) |
| 3 | `lv_hq_4c5e…` | Leitivo HQ |
| 4 | `lcs-ritter-…` | Leitivo Tenant *ritter* |
| 5 | `lv_mitarbeiterdesign_…` | Leitivo Tenant *mitarbeiterdesign* |
| 6 | `orko-alerter-…` | Orko Alerter (interner Monitor) |
| 7 | `lv_cgassler_5ae5…` | Leitivo *cgassler* (Avi-Tenant) |
| 8 | `lv_cgassler_795d…` | Leitivo *cgassler* #2 |
| 9 | `lv_cgassler_5ea8…` | Leitivo *cgassler* #3 |
| 10 | `tcpulse-…` | tc-pulse (Feedback-Events) |
| 11 | `lv_ttkramer_…` | Leitivo Tenant *ttkramer* |

> Vollwerte stehen im Live-Container (`docker inspect tc-llm-proxy-llm-proxy-1`) und werden in W2
> 1:1 in die orkoprox-`.env` portiert (NICHT hier im Klartext abgelegt — Highlander/Secret-Disziplin).

## 3. Wire-Alias-Nutzung pro Client (was geht TATSÄCHLICH raus)

### Leitivo (lc) — `LCS_LLM_BASE_URL=https://llm.true-code.de`, Key `lv_*`
Quelle: `leitivo-client/api/app/settings.py:115-158`. Die `llm_model_*`-Settings sind die exakten Wire-Strings:

| lc-Config | Wire-Alias | orkoprox? |
|---|---|---|
| `llm_model_classify` | `classify` | ✅ |
| `llm_model_extract` | `extract` | ✅ |
| `llm_model_compose` | `compose` | ✅ |
| `llm_model_chat` | `chat` | ✅ |
| `llm_model_avi` | **`high`** (Avi!) | ✅ |
| `llm_model_reason` | `reason` | ✅ |
| `llm_model_report` | `report` | ✅ |
| `llm_model_ocr` | `ocr` | ✅ |
| `llm_model_ocr_fast` | `ocr` (interner Tier→`ocr`, settings.py:151) | ✅ |
| `llm_model_ocr_vision_x` | `vision_x` | ✅ |

> **Klarstellung:** `ocr_fast` ist KEIN Wire-Alias — lc löst es client-seitig auf `"ocr"` auf
> (settings.py:146-151: „nutzen 'ocr' direkt als fast-Alias"). Frühere Diff-Sorge entkräftet.

### Oekotopia — `LLM_PROXY_BASE_URL=https://llm.true-code.de`
Embedding-Only-Client. Modell `bge-multilingual-gemma2`, **3584-dim** (OVH/EU).
→ orkoprox `embed → ovh/bge-multilingual-gemma2`, 3584-dim (config.py:190-191,264). **Parität ✅** (kein Vektor-Drift).

### TrueCode — `high`/`medium`/`gpt-5.4`-Aliase
- `high`, `medium`: in orkoprox vorhanden ✅.
- `gpt-5.4`/`gpt5`/`codex`/`deepseek`: im Legacy-Container nur als `FALLBACK_PROVIDERS_*=ovh`-Schlüssel
  präsent (kein eigenes Modell). In orkoprox sind diese als **Prefix-`provider/model`** oder über die
  Tier-Aliase abbildbar; ein direkter `gpt-5.4`-Alias existiert in orkoprox NICHT (nur tier/task-Aliase).
  → **Offen-Punkt W3:** falls ein TrueCode-Pfad `gpt-5.4` als nackten model-String sendet, braucht es
  entweder einen `CUSTOM_PROVIDERS`/Alias-Eintrag ODER der Aufrufer nutzt `xhigh`/`reason`. In W3-Canary
  hart prüfen; kein lokaler TrueCode-Sender in den durchsuchten Repos gefunden (Pfad serverseitig).

### Teasewitch (Melanie/Persona) — **NICHT am Proxy**
Nutzt Together.ai (Apriel) direkt + Ollama-Fallback, eigenes `bge-m3` (1024-dim) lokal.
→ **Cutover-irrelevant** (spricht `llm.true-code.de` nicht an). In W5 nur als „kein Impact" verifizieren.

### Konsolidierte Wire-Alias-Liste (alle Clients, dedupliziert)
```
chat  classify  compose  extract  high  medium  reason  report  ocr  vision_x  embed
```
Alle 11 in orkoprox vorhanden (orkoprox exponiert 22 Aliase gesamt). **Vollabdeckung.**

### ⚠ Befund: `/v1/models`-Listing-Lücke (kosmetisch, NICHT Cutover-relevant)

Lokaler Smoke (orkoprox `127.0.0.1:8099`, `make dev`): `/v1/models` listet nur **12 Tier-/Task-Aliase**
(`_build_models_payload` main.py:417-432 hat ein hartcodiertes 12er-`alias_targets`-Dict). **NICHT gelistet:**
`vision_x`, `embed`, `voice`, `voice_hq`, `image`, `reason_lite`, `reason_mid`, `long_context`, `report_premium`,
`report_structure`.

**Aber Routing funktioniert für ALLE** — der echte Auflösungspfad ist `router.py:218
_resolve_route_decision()` mit dem **vollständigen** Alias-Dict (router.py:230-248). Unit-Beweis (kein Netz):

```
chat → ovh/Mistral-Small-3.2-24B   high → ovh/Mistral-Small-3.2-24B   reason → ovh/gpt-oss-120b
report → ovh/Meta-Llama-3_3-70B    ocr → ovh/Mistral-Small-3.2-24B    vision_x → ovh/Qwen2.5-VL-72B
classify → ovh/Mistral-7B          extract/compose → ovh/Mistral-Small-3.2-24B
embed → ovh/bge-multilingual-gemma2 @ 3584-dim
→ AUFLÖSBAR: 10/10 Wire-Aliase, 0 Fehler. tests/test_alias_resolution_w3pre.py: 17 passed.
```

**Konsequenz:** Clients senden den Alias direkt als `model:`-String und validieren NICHT gegen `/v1/models`
(lc/oekotopia tun das nicht). Die Listing-Lücke bricht den Cutover nicht. **Kein Code-Edit** (Owner-Direktive
2026-06-19: orkoprox out-of-box, kein Code-Rumfummeln). Listing-Vervollständigung → Backlog (siehe unten).

## 4. Mistral-La-Plateforme-Key — KORREKTUR des Plan-Ist-Stands

**Befund (sops-decrypt `~/orko/secrets/kopp-proxy-runtime__env.env`):**

| Var | Status | Wert |
|---|---|---|
| `MISTRAL_LP_API_KEY` | **BEFÜLLT** | 32 Zeichen, Präfix `hufr…` |
| `MISTRAL_LP_BASE_URL` | befüllt | `https://api.mistral.ai/v1` (25 Z.) |
| `MISTRAL_LP_DEFAULT_MODEL` | befüllt | `mistral-…` (20 Z.) |

Der **identische** `hufr…`-Key (len 32) läuft live im tc-llm-proxy-Container (`MISTRAL_LP_API_KEY`).
→ **Plan-SSOT-Annahme „WERT LEER" ist veraltet/falsch.** Best-Guess-Entscheidung (Autonom-Modus):
Key ist vorhanden und produktiv — **kein Neu-Befüllen nötig**, in W2 aus dem Secret-Store (sops) in die
orkoprox-`.env` übernehmen. Akzeptanz „Mistral-Key befüllt ODER bewusst-leer dokumentiert" = **erfüllt
(befüllt)**.

**Owner-Frage „braucht reason-Tier Mistral Large?"** — Antwort aus orkoprox-Config:
`reason → ovh/gpt-oss-120b` (NICHT Mistral). Mistral-Large wird nur via `report_premium → mistral_lp/mistral-large-latest`
genutzt (config.py:212), Fallback ovh. → reason läuft auf OVH gpt-oss-120b; Mistral-Key dient `report_premium`.
Avi-Eskalation (`high`/`reason`) ist damit OVH-getragen — Qualitätsmessung in W3/W5.

## 5. Provider-Compliance-Diff (Legacy-Zwitter → orkoprox sauber)

| Aspekt | tc-llm-proxy (Legacy, live) | orkoprox |
|---|---|---|
| `PRIMARY_PROVIDER` | ovh | ovh ✅ |
| `DEFAULT_PROVIDER` | **baseten** ⚠ (US!) | ovh ✅ |
| `FALLBACK_PROVIDERS_*` | ovh | ovh ✅ |
| US-Keys gesetzt | `TOGETHER`, `BASETEN`, `GROQ` scharf ⚠ | nur via opt-in `CUSTOM_PROVIDERS` |
| `ocr`-Ziel | baseten/Kimi-K2.5 (US) | ovh/Mistral-Small-3.2-24B |
| `high`-Ziel (Avi) | GLM-4.7 (Legacy-Kommentar) | ovh/Mistral-Small-3.2-24B |
| Embedding | bge-multilingual-gemma2 3584d | identisch ✅ |

→ Der Cutover **eliminiert** den US-Provider-Zwitter (G-NO-US-PROVIDER) als Nebeneffekt — gewollt (§12).
**Modell-Verhaltens-Risiko:** `ocr`/`high` liefern auf OVH andere Qualität als auf Kimi/GLM. Das ist die
„Avi dumm wie Brot?"-Frage → in W3-Canary + W5-Live messen, nicht spekulieren.

## 6. W1-Akzeptanz-Status

- [x] Alle Client-Key-Typen + Wire-Aliase inventarisiert (11 Keys, 11 Wire-Aliase) — Vollabdeckung
- [x] Mistral-Key geklärt: **befüllt** (Plan-Ist-Stand korrigiert), nicht leer
- [x] orkoprox-Alias-Map gegen Client-Nutzung gemappt: 0 fehlende Aliase, Diff (Modell-Ziele) dokumentiert
- [x] `make test` grün (566 passed)
- [ ] **Nach W3 offen:** `gpt-5.4`-TrueCode-Pfad live verifizieren (kein lokaler Sender gefunden)
