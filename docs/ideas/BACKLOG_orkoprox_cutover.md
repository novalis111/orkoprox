# Backlog — W-ORKOPROX-CUTOVER (Scope-Wachstum, NICHT in W1-W6)

Einträge, die während des autonomen Cutover-Laufs auftauchten, aber außerhalb des Wellen-Scopes
liegen (Owner-Direktive 2026-06-19: orkoprox out-of-box, KEIN Code-Edit). Hier geparkt, nicht jetzt gebaut.

## B1 — `/v1/models` listet nicht alle auflösbaren Aliase (kosmetisch)
**Gefunden:** W1, 2026-06-19.
`app/main.py:_build_models_payload()` (Zeile 417-432) hat ein hartcodiertes 12er-`alias_targets`-Dict
(`xhigh/high/medium/low/classify/extract/compose/chat/reason/report/ocr/vision`). Der Router
(`router.py:230-248 _resolve_route_decision`) kennt zusätzlich `vision_x, image, voice, voice_hq,
reason_lite, reason_mid, long_context, report_premium, report_structure` und löst sie korrekt auf —
aber `/v1/models` zeigt sie nicht.

**Impact:** rein kosmetisch. Routing funktioniert (10/10 Wire-Aliase auflösbar, Beleg W1-Report).
Kein Client validiert gegen `/v1/models`. Kein Cutover-Risiko.

**Fix (später, eigenes PR im OSS-Repo):** `alias_targets` in `_build_models_payload` aus dem
gemeinsamen Router-Alias-Dict speisen statt hartcodieren — Single-Source-of-Truth. ~0.5 PT.
Upstream-fähig (novalis111/orkoprox).

## B2 — `gpt-5.4`/`codex`/`deepseek` TrueCode-Pfad nicht lokal belegbar
**Gefunden:** W1, 2026-06-19.
Im tc-llm-proxy-Container existieren `FALLBACK_PROVIDERS_{GPT5,CODEX,DEEPSEEK}=ovh`, aber kein
lokaler Sender dieser model-Strings in den durchsuchten Repos (lc/oekotopia/teasewitch). orkoprox
hat keinen `gpt-5.4`-Alias (nur tier/task). Falls ein serverseitiger TrueCode-Pfad `gpt-5.4` als
nackten model-String sendet → 404 möglich.

**Aktion:** in W3-Canary live gegen orkoprox-Parallelport prüfen (model=`gpt-5.4` mit TrueCode-Key).
Falls real genutzt: `CUSTOM_PROVIDERS`-Alias ODER Aufrufer auf `xhigh`/`reason` umstellen (Infra/Config,
kein orkoprox-Code-Edit). Bis dahin: Risiko niedrig (kein Beleg für aktive Nutzung).
