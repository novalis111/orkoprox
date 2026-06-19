# W4 Cutover — Rollback-Runbook (llm.true-code.de)

**Zweck:** `llm.true-code.de` in <1 min zurück auf tc-llm-proxy hängen, falls orkoprox nach dem
Cutover Probleme zeigt. tc-llm-proxy-Container bleibt beim Cutover LAUFEN (nur Label entfernt) =
Hot-Standby.

## Cutover-Prinzip (Label-Swap, Traefik `providers.docker`)

`llm.true-code.de` wird über Docker-Labels am Backend-Container geroutet. Cutover =
- orkoprox-Container (`src-proxy-1`) bekommt die `traefik.*`-Labels für `Host(llm.true-code.de)`
- tc-llm-proxy-Container (`tc-llm-proxy-llm-proxy-1`) verliert seine `traefik.enable`/Router-Labels
- beide Container laufen weiter; nur das Routing-Ziel ändert sich.

Die anderen 22 Domains haben ihre Labels an ihren EIGENEN Containern → unberührt.

## Rollback-Kriterien (sofort zurück bei JEDEM davon)

- `https://llm.true-code.de/v1/healthz` ≠ 200 nach Cutover
- TLS-Fehler auf llm.true-code.de
- Irgendein Client-Key gibt 401/5xx auf einem zuvor grünen Alias
- Irgendeine der 22 anderen Domains weicht vom Baseline-Status ab
- 5xx-Rate-Spike / Health-Fail im Monitor

## ROLLBACK-BEFEHL (ein Schritt, <1 min)

orkoprox liegt als separater Compose-Stack vor. Der Cutover wird über eine Traefik-Label-Override-
Datei `docker-compose.cutover.yml` gefahren. Rollback = orkoprox-Labels entfernen + tc-llm-proxy-
Labels wiederherstellen:

```bash
ssh oekotopia '
  set -e
  cd /srv/stacks/orkoprox/src
  # 1. orkoprox zurück auf labellos (Parallelport-Zustand)
  docker compose -f docker-compose.oekotopia.yml up -d --force-recreate proxy
  # 2. tc-llm-proxy-Labels wiederherstellen (Original-Deploy-Compose)
  cd /srv/stacks/tc-llm-proxy
  docker compose -f docker-compose.deploy.yml up -d --force-recreate llm-proxy
  # 3. Verify: llm.true-code.de wieder auf tc-llm-proxy
  sleep 5
  curl -s -o /dev/null -w "rollback healthz: HTTP %{http_code}\n" https://llm.true-code.de/v1/healthz \
    -H "Authorization: Bearer $(grep ^PROXY_API_KEYS= /srv/stacks/tc-llm-proxy/.env | cut -d= -f2- | cut -d, -f1)"
'
```

Nach Rollback: `docker ps` zeigt tc-llm-proxy-llm-proxy-1 wieder mit Traefik-Labels; orkoprox läuft
weiter auf Parallelport 8081 (kein Traffic). Ursache analysieren, dann erneuter Cutover-Versuch.

## Cutover-Verifikation (nach Vorwärts-Cutover, alle müssen grün sein)

1. Alle 23 Domains HTTP+TLS == Baseline (`w4_baseline`)
2. `https://llm.true-code.de/v1/healthz` == 200
3. Je 1 Client-Call pro Key-Klasse über `https://llm.true-code.de` grün
4. orkoprox `src-proxy-1` healthy, tc-llm-proxy-llm-proxy-1 LÄUFT weiter (Standby)
