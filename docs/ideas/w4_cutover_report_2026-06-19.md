# W4 — Traefik-Cutover llm.true-code.de → orkoprox (ERFOLGREICH)

**Welle:** W-ORKOPROX-CUTOVER · W4 (autonom, Owner-Gate aufgehoben 2026-06-19)
**Datum:** 2026-06-19
**Status:** grün — Zero-Downtime-Cutover, 23/23 Domains == Baseline, Rollback-Pfad steht

## Was passierte

`llm.true-code.de` wurde per Traefik-Label-Swap von `tc-llm-proxy-llm-proxy-1` auf den orkoprox-
Container `src-proxy-1` umgehängt. **Nur dieses eine Host-Label wanderte** — die anderen 22 Domains
behielten ihre Labels an ihren eigenen Containern.

### Mechanik (Label-Swap, Traefik `providers.docker`)
1. orkoprox `src-proxy-1` bekam die Traefik-Labels (`docker-compose.cutover.yml`): Router `orkoprox`
   (+`orkoprox-http`-Redirect), `Host(\`llm.true-code.de\`)`, certresolver `le`, Backend-Port 8091.
2. tc-llm-proxy `llm-proxy` bekam `traefik.enable=false` (`/tmp/tc-disable-traefik.yml`-Override) —
   Container bleibt **running + healthy** als Hot-Standby.

### Stolperstein (gelöst)
Erster Disable-Versuch schlug fehl (`TAG must be set in .env.deploy`) → kurzzeitig beanspruchten
beide Container `Host(llm.true-code.de)`. `/v1/healthz` blieb durchgehend 200. Fix: `--env-file
.env.deploy` mitgegeben (`TAG=tc-llm-proxy-20260610T1100-fd9d8b22`), Disable sauber durchgezogen.

## Verifikation (alle grün)

| Check | Ergebnis |
|---|---|
| **23/23 Domains == Baseline** (HTTP+TLS) | ✅ 0 Drift |
| `llm.true-code.de` bedient orkoprox | ✅ (B1-Marker `reason_lite`+`report_premium` gelistet, 40 IDs) |
| `/v1/healthz` über public domain | ✅ 200 |
| Avi chat / reason / embed (cgassler) | ✅ Mistral-Small-24B / **gpt-oss-120b** / 3584d |
| **11/11 Client-Keys** über public domain | ✅ alle 200 |
| Tenant-chat cgassler/ttkramer/ritter | ✅ alle 200 |
| tc-llm-proxy Hot-Standby | ✅ running+healthy, `traefik.enable=false` |

## Baseline-Referenz (für künftige Vergleiche)

Erwartete Status (manche 404/403 sind NORMAL = API-Roots ohne `/`-Handler / MinIO-Auth):
- 200: platform/leitivo/leitivo.de/app.true-code/api.true-code/true-code/oekotopia/
  mitarbeiterdesign/melanie-dorn/ingo-terpelle (+www-Varianten)
- 404 (normal): api.leitivo.com, pulse.true-code.de, llm.true-code.de (`/`), api.oekotopia.com
- 403 (normal): minio.pulse.true-code.de, media.oekotopia.com

## Rollback (falls W5 Probleme zeigt)

`deploy/oekotopia/ROLLBACK_RUNBOOK.md` — ein SSH-Block hängt das Label zurück auf tc-llm-proxy
(<1 min). tc-llm-proxy läuft als Hot-Standby bis W5 grün (Doktrin: Rollback-Pfad nicht voreilig
zerstören).

## Artefakte
- `deploy/oekotopia/docker-compose.cutover.yml` — Traefik-Label-Override (orkoprox)
- `deploy/oekotopia/ROLLBACK_RUNBOOK.md` — Rollback + Kriterien
- tc-llm-proxy-Disable: `/tmp/tc-disable-traefik.yml` (auf oekotopia, ephemeral)
