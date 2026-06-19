# W6b — tc-llm-proxy-Archivierung (TERMINIERT: nach 24-48h-Stabilitätsfenster)

**NICHT vor 2026-06-21 ausführen.** Doktrin (Plan-SSOT W6): Legacy erst nach mind. 24-48h
orkoprox-Stabilität archivieren — der Rollback-Pfad (tc-llm-proxy Hot-Standby) darf nicht
voreilig zerstört werden. Der Cutover war 2026-06-19.

## Vorbedingung (alle müssen erfüllt sein, sonst NICHT archivieren)

```bash
ssh oekotopia '
  KEY=$(grep ^PROXY_API_KEYS= /srv/stacks/orkoprox/src/.env | cut -d= -f2- | cut -d, -f1)
  echo "orkoprox healthy: $(docker inspect -f "{{.State.Health.Status}}" src-proxy-1)"
  echo "healthz: HTTP $(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $KEY" https://llm.true-code.de/v1/healthz)"
  echo "5xx letzte 24h: $(docker logs src-proxy-1 --since 24h 2>&1 | grep -cE "\" 5[0-9][0-9] ")"
'
# Erwartung: healthy / HTTP 200 / 5xx == 0. Bei Abweichung: NICHT archivieren, triagieren.
```

## Archivierungs-Schritte (nach grüner Vorbedingung)

```bash
ssh oekotopia '
  set -e
  # 1. tc-llm-proxy-Stack stoppen (alle 5 Container)
  cd /srv/stacks/tc-llm-proxy
  docker compose --env-file .env.deploy -f docker-compose.deploy.yml down
  # 2. Stack-Ordner archivieren (umbenennen, nicht löschen — Audit-Trail)
  cd /srv/stacks
  mv tc-llm-proxy tc-llm-proxy_archived_20260621
  # 3. Verify: kein tc-llm-proxy-Container mehr, orkoprox unberührt
  docker ps --filter "name=tc-llm-proxy" --format "{{.Names}}" | grep . && echo "WARN: noch Container!" || echo "tc-llm-proxy gestoppt ✓"
  docker inspect -f "{{.State.Health.Status}}" src-proxy-1
'
```

## Nach W6b — letzter Verify

1. `docker ps` auf oekotopia: nur orkoprox-Stack (`src-proxy-1`, `src-redis-1`, `src-reranker-1`),
   kein `tc-llm-proxy-*` mehr.
2. `https://llm.true-code.de/v1/healthz` == 200 (orkoprox unverändert).
3. Ein finaler Client-Smoke (cgassler chat) grün.
4. Registry `tc_llm_proxy.json` status=archived ist bereits gesetzt (W6a).

## Rollback nach W6b (falls doch nötig)

Stack-Ordner ist nur umbenannt, nicht gelöscht. Rückweg:
```bash
ssh oekotopia 'cd /srv/stacks && mv tc-llm-proxy_archived_20260621 tc-llm-proxy &&
  cd tc-llm-proxy && docker compose --env-file .env.deploy -f docker-compose.deploy.yml up -d'
# dann ROLLBACK_RUNBOOK.md für Label-Swap zurück.
```
Endgültiges Löschen des Archiv-Ordners erst nach weiteren Wochen Stabilität (Owner-Entscheidung).
