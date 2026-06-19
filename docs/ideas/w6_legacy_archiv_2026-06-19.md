# W6 — Legacy-Archivierung tc-llm-proxy + llm-unified-proxy

**Welle:** W-ORKOPROX-CUTOVER · W6
**Datum:** 2026-06-19
**Status:** W6a grün (jetzt); W6b terminiert (nach 24-48h-Stabilitätsfenster, ab 2026-06-21)

## Split-Entscheidung (Doktrin-konform)

Plan-SSOT W6 verlangt explizit: *"Erst NACH Stabilitäts-Fenster (mind. 24-48h orkoprox grün):
tc-llm-proxy-Container gestoppt … Rollback-Pfad nicht voreilig zerstören."* Der Cutover war heute
(2026-06-19, ~30 min vor W6). Daher Split:

- **W6a (JETZT, sicher):** alles ohne Rollback-Risiko — Registry, Doku, Symlink-Cleanup, Runbook.
- **W6b (ab 2026-06-21, terminiert):** tc-llm-proxy-Container stoppen + Stack archivieren. Das ist
  der Schritt, der den Hot-Standby-Rollback-Pfad zerstört → braucht das Stabilitäts-Fenster.

## W6a — erledigt

| Aktion | Status |
|---|---|
| `llm-unified-proxy` aufgeräumt | ✅ war nur Symlink auf tc-llm-proxy (kein eigener Stack/Container), entfernt |
| Registry `tc_llm_proxy.json` → `status: archived` | ✅ + `successor: orkoprox`, archive_note, archived_at |
| Registry `orkoprox.json` → `status: ssot-live` | ✅ + `live_since`, `live_url`, veraltete Notes korrigiert |
| CHANGELOG.md aktualisiert | ✅ Unreleased: model_alias_map, compose-profiles, RERANKER_ENABLED=false, /v1/models 22 Aliase |
| W6b-Archiv-Runbook | ✅ `deploy/oekotopia/W6b_ARCHIV_RUNBOOK.md` (terminiert, mit Vorbedingungs-Gates) |

## W6b — terminiert (NICHT vor 2026-06-21)

Runbook: `deploy/oekotopia/W6b_ARCHIV_RUNBOOK.md`. Vorbedingung: orkoprox 24h healthy + 0 5xx.
Dann: tc-llm-proxy-Stack `down`, Ordner → `tc-llm-proxy_archived_20260621` (umbenennen, nicht löschen).

## Ist-Stand nach W6a

- `llm.true-code.de` → orkoprox (live, healthy, HTTP 200).
- tc-llm-proxy: 5 Container **running** als Hot-Standby (`traefik.enable=false`) — Rollback <1min.
- Kein Client zeigt auf tc-llm-proxy (alle über llm.true-code.de → orkoprox). Self-Contained §0 gewahrt.
- llm-unified-proxy: weg (war Symlink).

## W6-Akzeptanz-Status

- [x] (W6a) llm-unified-proxy-Reste entfernt (Symlink weg)
- [x] (W6a) Registry: tc_llm_proxy.json=archived (Verweis orkoprox), orkoprox.json=ssot-live
- [x] (W6a) Doku-Drift nachgezogen (CHANGELOG, Registry-Notes)
- [x] (W6a) Kein Client zeigt mehr auf tc-llm-proxy (alle über llm.true-code.de→orkoprox)
- [ ] (W6b, terminiert ab 2026-06-21) tc-llm-proxy-Container gestoppt + Stack archiviert — Runbook bereit

**Begründung der Terminierung:** Voreiliges Stoppen würde den getesteten Rollback-Pfad (Hot-Standby)
zerstören, während orkoprox erst ~30 min live ist. Die Doktrin ist hier eindeutig — Stabilität vor
endgültigem Abriss. Alles Übrige ist erledigt.
