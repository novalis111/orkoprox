---
plan_id: w-orkoprox-responses-api
repo: orkoprox
created: '2026-06-21'
updated: '2026-06-21'
status: completed
owner: info@ingo-terpelle.de
external_blockers: []
waves:
  - wave_id: 1
    name: /v1/responses-Route + Tool-Calls + Streaming + Live-Compat-Matrix gegen orkoprox umstellen
    inventur: >-
      orkoprox hatte /v1/responses NICHT (app/main.py: nur chat/completions,
      embeddings, rerank, audio, images, vision). Archivierter tc-llm-proxy
      hatte einen Minimal-Endpoint (main.py:280-393), der aber Tool-Calls
      droppte (nur delta.content / message.content). tests/test_live_compat_matrix.py
      verlangt die VOLLE Responses-API (function_call-Items, SSE-Event-Sequenz,
      previous_response_id, store, retrieval, list/delete, tool-loop).
    dry_anker:
      mode: neu
      audit_datum: '2026-06-21'
      audit_beleg: >-
        Kein Duplikat der Guard-/Eskalations-/Cache-/Hook-Pipeline. Neuer
        Endpoint adaptiert Responses<->ChatCompletions und ruft die bestehende
        chat_completions()-Funktion auf (app/main.py). Format-/Stream-/Store-
        Logik isoliert in app/responses_api.py.
    aufwand_pt: 1
    akzeptanz:
      - 'POST /v1/responses (non-stream + stream) liefert OpenAI-Responses-Format (object=response, output[], output_text).'
      - 'Tool-Calls durchgereicht: function_call-Items (non-stream) + response.function_call_arguments.delta-SSE (stream).'
      - 'Server-State: store-Flag, previous_response_id-Verkettung, GET /{id}, GET /{id}/input_items, GET list, DELETE /{id}.'
      - 'Guard/Eskalation/Cache/Hooks via Wiederverwendung von chat_completions (DRY) — nicht dupliziert.'
      - 'test_live_compat_matrix.py auf orkoprox (Default :8091) umgestellt, Modelle via LIVE_COMPAT_MODELS konfigurierbar.'
      - 'Gates gruen: pytest 582 passed / 15 skipped, ruff clean, pyright 0 errors.'
    smoke_skript:
      - 'make test  # 582 passed, 15 skipped'
      - 'make lint   # ruff: All checks passed'
      - 'pyright app # 0 errors'
      - 'Live (echter uvicorn :8099, mock-Provider): 12/12 HTTP-Checks PASS (non-stream, stream-tool-call, store, prev_id, list/delete, tool-loop, auth).'
    doctrine_fit: >-
      Enterprise-Feature, no shortcuts, sane (echter Durchstich zum Backend ueber
      chat_completions, kein Stub). DRY (keine Pipeline-Duplikation). Live-Proxy-
      Verifikation MANDATORY erfuellt (echter Server, kein Mock-Client).
---

# OpenAI /v1/responses-Endpoint in orkoprox (Tool-Calls + Streaming + Server-State)

<!-- Plan-SSOT: w-orkoprox-responses-api -->
<!-- Frontmatter via scripts/plan_frontmatter.py pflegen. -->

## Kontext

orkoprox ist der LIVE-SSOT-LLM-Proxy (laeuft auf oekotopia.com; alle Clients
lc/tc/oc/Avi/Kalia routen darueber). Der OpenAI-Responses-API-Endpoint
(`/v1/responses`) fehlte — bis zum Cutover lief die Live-Compat-Matrix gegen
den inzwischen archivierten tc-llm-proxy. Dessen Endpoint war minimal und
droppte Tool-Calls. Diese Welle implementiert die volle Responses-API in
orkoprox und stellt die Matrix auf orkoprox um.

## Architektur (DRY)

- **app/responses_api.py** (neu): `ResponsesRequest`-Schema; Uebersetzung
  Responses-Input (`instructions`, `input` als String/Item-Liste,
  `function_call_output`, `previous_response_id`) → Chat-Messages; Uebersetzung
  Chat-Completion → Responses-Output (`message`/`function_call`-Items,
  `output_text`); SSE-Stream-Translator (chat-chunks → `response.*`-Events mit
  Tool-Call-Argument-Akkumulation); `ResponseStore` auf dem vorhandenen
  `KeyValueStore`-Protokoll (Redis/Memory) fuer store/retrieval/list/delete/
  Verkettung.
- **app/main.py**: Routen `POST /v1/responses` (+ GET list, GET /{id},
  GET /{id}/input_items, DELETE /{id}). Der POST ruft die bestehende
  `chat_completions()`-Funktion auf → Pre-/Post-Guard, F8-Eskalationskaskade,
  Semantic-Cache, F6-Hooks und Tool-Call-Sanitizing werden NICHT dupliziert.

## Wellen

W1 (completed) — Details im Frontmatter `waves`.
