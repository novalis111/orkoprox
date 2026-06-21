# HANDOFF — w-orkoprox-responses-api

**Loop:** orkoprox-/v1/responses-API (FULL FORCE, Enterprise-Feature)
**Branch:** feature/w-orkoprox-responses-api → gemerged/gepusht auf `main`
**Worktree:** orkoprox-wt-responses
**Datum:** 2026-06-21
**Status:** ✅ FERTIG — Endpoint live-faehig, alle Gates gruen, Live-Proxy-Verifikation bestanden.

---

## Was gebaut wurde

OpenAI-`/v1/responses`-Endpoint in orkoprox (vorher: nicht vorhanden). Voller
Durchstich zum Backend ueber Wiederverwendung von `chat_completions()` — keine
Stub-Antwort, keine Pipeline-Duplikation.

### Neu / geaendert
- **app/responses_api.py** (neu, ~600 Z): `ResponsesRequest`-Schema,
  Request/Response-/SSE-Uebersetzung Responses↔ChatCompletions, `ResponseStore`
  (Server-State auf `KeyValueStore`: Redis wenn konfiguriert, sonst Memory).
- **app/main.py**: `POST /v1/responses` + `GET /v1/responses` (list) +
  `GET /v1/responses/{id}` + `GET /v1/responses/{id}/input_items` +
  `DELETE /v1/responses/{id}`. POST delegiert an `chat_completions()` →
  Guard (pre/post), F8-Eskalationskaskade, Semantic-Cache, F6-Hooks und
  Tool-Call-Sanitizing werden geerbt, nicht kopiert.
- **tests/test_responses_endpoint.py** (neu, 14 Tests gegen mock-Provider).
- **tests/test_live_compat_matrix.py**: auf orkoprox umgestellt (Default
  `:8091`); Modell-Matrix via `LIVE_COMPAT_MODELS` konfigurierbar; Doku-Kommentar
  zum Cutover.
- **docs/ideas/2026-06-21_w_orkoprox_responses_api.md**: Frontmatter ausgefuellt,
  status → completed.

### Feature-Abdeckung (entspricht Live-Compat-Matrix)
- Non-stream + Streaming
- Tool-Calls: `function_call`-Items (non-stream) + `response.function_call_arguments.delta`/
  `.done` + `response.output_item.added`/`.done` (stream), Argument-Akkumulation
- `instructions` → System-Message, `input` als String oder Item-Liste
- `function_call_output` Tool-Loop (Tool-Ergebnis zurueck in den Input)
- Server-State: `store`-Flag, `previous_response_id`-Verkettung, Retrieval,
  `input_items`, List, Delete; `store=false` → nicht abrufbar (404)

## Gate-Belege
- `pytest -q`: **582 passed, 15 skipped** (15 = Live-Compat-Matrix ohne `LIVE_COMPAT_API_KEY`)
- `ruff check app tests`: **All checks passed**
- `pyright app`: **0 errors, 0 warnings** (CI-Gate)
- **Live-Proxy-Verifikation (§12)**: echter uvicorn-Server (Port 8099, mock-Provider,
  KEIN TestClient), 12/12 HTTP-Checks PASS — non-stream, stream-tool-call,
  store/retrieve, store=false→404, previous_response_id, input_items, list/delete,
  function_call_output-Loop, auth.

---

## NÄCHSTER SCHRITT (Hub)

1. **CI gruen abwarten** auf `main` (GitHub Actions `ci.yml`: ruff + pyright + pytest).
   Repo-Remote: `git@github.com:novalis111/orkoprox.git`. Lokal sind alle drei
   Gates gruen vorverifiziert.
2. **Live-Smoke gegen Prod-orkoprox** (oekotopia.com :8081) wenn deployed:
   `LIVE_COMPAT_API_KEY=<key> LIVE_COMPAT_BASE_URL=https://llm.true-code.de pytest tests/test_live_compat_matrix.py -q`
   — verifiziert `/v1/responses` Tool-Calls/Streaming/Server-State gegen ECHTE
   Provider (OVH/Mistral) mit den Tier-Aliasen `high` + Wire-Alias `gpt-5.4`.
   (Lokal geskippt, da kein Provider-Key im Worktree.)
3. **Deploy** des neuen Endpoints auf den Prod-orkoprox-Container (gleicher
   Deploy-Pfad wie Cutover-W2; Endpoint ist additiv, kein Breaking Change an
   bestehenden Routen).
4. **Clients** die bisher `/v1/responses` am archivierten tc-llm-proxy nutzten
   (falls noch welche): auf orkoprox umhaengen — Endpoint ist jetzt SSOT-seitig
   verfuegbar.

**Risiko / Hinweise:**
- `ResponseStore` nutzt in-memory wenn kein `REDIS_URL` gesetzt → Server-State
  (previous_response_id/Retrieval) resettet bei Container-Restart. Fuer Prod
  mit `REDIS_URL` betreiben, sonst sind verkettete Konversationen nach Restart
  weg. (Gleiche Trade-off-Doktrin wie beim Metering-Store.)
- Endpoint ist additiv; bestehende Routen unveraendert.
