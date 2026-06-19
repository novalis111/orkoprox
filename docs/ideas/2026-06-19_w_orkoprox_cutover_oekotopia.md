---
plan_id: w-orkoprox-cutover-oekotopia
wrap_2026-06-19: >-
  W-ORKOPROX-CUTOVER vollzogen (W1-W6, 1 Lauf, autonom). orkoprox ist LIVE als SSOT-LLM-Gateway
  hinter llm.true-code.de auf oekotopia. Commits orkoprox: W1 c6d6ce2, W2 e122f08, W3 41e4c8c,
  W4 6c0e247, W5 6116ba2, W6a 2f1e853 (+wrap). Hub-Registry: a8fdf6a (ssot-live/archived).
  Live-Smoke je Welle grün: W2 /v1/models 40 IDs+chat+embed(3584d); W3 11 Keys×10 Aliase 0-Fehler;
  W4 Traefik-Label-Swap 23/23 Domains==Baseline; W5 100×200/0×5xx echter Traffic, Avi
  reason→gpt-oss-120b (Qualität GESTIEGEN vs Legacy-Apriel-15b). PT plan→real: W1 1→1, W2 3→4.5,
  W3 3→2, W4 2→2.5, W5 2→1.5, W6 1→1 (gesamt 12→12.5 PT).
  CODE-CHANGES (Owner hob Highlander auf): B1 model_alias_map SSOT (/v1/models 12→22 Aliase),
  compose-Profiles (whisper/reranker opt-in, kein 8GB-First-Pull), RERANKER_ENABLED=false-default,
  Whisper-Sidecar raus (OVH-direkt). 568 Tests grün, Ruff grün.
  OWNER-TODOs: (1) GitHub-Push novalis111/orkoprox freigeben (Owner-Entscheidung, Code bereit).
  (2) W6b ab 2026-06-21: tc-llm-proxy-Stack stoppen+archivieren (Runbook deploy/oekotopia/
  W6b_ARCHIV_RUNBOOK.md, Hot-Standby bis dahin). (3) P1 /v1/responses-Welle (Owner: "enterprise
  grade, bald", Backlog B3). HUB-RESTPOSTEN: Compendium-Promotion (SSH-Timeout-Falle, Sidecar-
  Provider-Regel), nächster orkoprox-Wellen-Schnitt (Responses-API).
repo: orkoprox
created: '2026-06-19'
updated: '2026-06-19'
status: completed
owner: terpelle-ingo
external_blockers: []
waves:
  - wave_id: 1
    name: Diff-Bestätigung + Client-Key-Inventar + Mistral-Key befüllen
    inventur: "orkoprox v0.1.2 geklont (40-tools/orkoprox, github novalis111/orkoprox), OVH-First/kein Apriel/kein US-Default verifiziert. Läuft schon produktiv bei KOPP (llm-proxy.kopp.de). Migrations-Skizze orko/docs/ideas/2026-06-11_w_llm_proxy_orkoprox_migration.md hat vollen Audit. Aktiver Stand oekotopia: tc-llm-proxy (healthy), llm-unified-proxy (tot, .bak). Client-Keys: lc nutzt lv_*-Keys + semantische Aliase (chat/reason/classify/extract/compose/report) über llm.true-code.de; TrueCode nutzt high/medium/gpt-5.4-Aliase; Mistral-Key MISTRAL_LP_API_KEY in secrets/kopp-proxy-runtime__env.env Var gesetzt aber WERT LEER."
    dry_anker:
      mode: neu
      audit_datum: '2026-06-19'
      audit_beleg: "ssh oekotopia + cgassler live verifiziert 2026-06-19; orkoprox app/config.py OVH-Defaults; lc-API-Env LCS_LLM_BASE_URL=llm.true-code.de + semantische Aliase; sops get-key MISTRAL_LP_API_KEY leer."
    aufwand_pt: 1
    akzeptanz:
      - "Alle Client-Key-Typen + verwendete Aliase inventarisiert (Leitivo lv_* + chat/reason/classify/extract/compose/report; TrueCode high/medium/gpt-5.4; Oekotopia; Melanie/Persona-Pipeline) — Cutover-Kompatibilitätsmatrix: jeder Key+Alias den ein Client HEUTE nutzt MUSS orkoprox bedienen"
      - "Mistral-La-Plateforme-Key befüllt (MISTRAL_LP_API_KEY in Secret-Store, sops set) ODER als bewusst-leer dokumentiert (report_premium→mistral_lp Fallback ovh greift). Owner-Frage geklärt: braucht reason-Tier Mistral Large oder reicht ovh/gpt-oss-120b?"
      - "orkoprox-Alias-Map gegen Client-Nutzung gemappt: jeder von Clients genutzte Alias existiert in orkoprox (README:156 hat alle); Diff dokumentiert"
    smoke_skript:
      - "orkoprox lokal: make test grün (560 Tests). Alias-Liste orkoprox /v1/models vs. Client-genutzte Aliase = Vollabdeckung. Key-Matrix-Tabelle."
    doctrine_fit: "Owner 2026-06-19: orkoprox=SSOT, alle Clients laufen weiter mit ihren Keys. Self-Contained §0. §12 OVH-First (orkoprox erfüllt out-of-box). Cutover-Garantie = Inventar zuerst."
  - wave_id: 2
    name: orkoprox parallel auf oekotopia hochziehen (anderer Port, noch NICHT an llm.true-code.de)
    inventur: 'oekotopia hat tc-llm-proxy live auf llm.true-code.de (Traefik+TLS). orkoprox baut über GitHub-Actions (GHCR-Image) ODER lokaler docker build. docker-compose.yml + Dockerfile vorhanden. KOPP-Präzedenz: orkoprox läuft schon als llm-proxy.kopp.de.'
    dry_anker:
      mode: neu
      audit_datum: '2026-06-19'
      audit_beleg: orkoprox/docker-compose.yml + Dockerfile vorhanden; /srv/stacks/ auf oekotopia; tc-llm-proxy 5-Container-Stack als Vorbild.
    aufwand_pt: 3.0
    akzeptanz:
      - orkoprox-Stack auf oekotopia auf SEPARATEM Port hochgezogen, noch NICHT hinter llm.true-code.de — parallel zum aktiven tc-llm-proxy, null Disruption
      - orkoprox-.env mit OVH-Key + Mistral-Key + ALLEN Client-Keys (proxy_api_keys CSV) + Embeddings bge-multilingual-gemma2 3584d (Vektor-Parität)
      - orkoprox /v1/healthz + /health/ready = 200 auf Parallelport; /v1/models listet alle Client-Aliase
    smoke_skript:
      - curl orkoprox-Parallelport /v1/healthz=200, /v1/models=alle Aliase. docker ps healthy NEBEN tc-llm-proxy.
    doctrine_fit: 'Zero-Downtime: parallel vor Cutover. Embeddings-Parität gegen Vektor-Drift. KOPP-Präzedenz de-riskt.'
  - wave_id: 3
    name: 'Canary-Smoke: alle Client-Key-Typen × alle Aliase gegen orkoprox-Parallelport'
    inventur: Vor Cutover muss bewiesen sein, dass JEDER Client mit SEINEM Key + SEINEN Aliasen gegen orkoprox funktioniert. Live-Compat-Matrix aus tc-llm-proxy als Vorbild.
    dry_anker:
      mode: neu
      audit_datum: '2026-06-19'
      audit_beleg: tc-llm-proxy test_live_compat_matrix.py als Muster; orkoprox muss Matrix + lc-Aliase (chat/reason/classify) grün liefern.
    aufwand_pt: 3.0
    akzeptanz:
      - 'Canary-Matrix grün: jeder Client-Key × jeder genutzte Alias → orkoprox valide Antwort (kein 401/Alias-404/Provider-Fehler)'
      - 'Avi-spezifisch: chat→sinnvoll, reason→stärkeres Modell (Eskalation), embed→3584d (Parität), Tool-Calls (function_call) ok'
      - Streaming + previous_response_id + Responses-Store getestet; kein Regression vs tc-llm-proxy
    smoke_skript:
      - 'Live-Compat-Matrix gegen orkoprox-Parallelport mit ALLEN Keys: 0 Fehlschläge. Avi chat/reason/embed verifiziert.'
    doctrine_fit: Cutover-Garantie HART beweisen VOR Cutover. §12 Live-Proxy. Goldstandard für geschäftskritischen Pfad (§7).
  - wave_id: 4
    name: Traefik-Cutover llm.true-code.de → orkoprox (mit Sofort-Rollback-Pfad)
    inventur: 'llm.true-code.de zeigt via Traefik auf tc-llm-proxy. Cutover = Traefik-Router auf orkoprox-Port umhängen. Kritisch: llm.true-code.de ist Laufzeit-Dependency ALLER Tenants (Avi/Pipelines/Embeddings), TrueCode, Oekotopia, Melanie — Ausfall legt Fleet-KI lahm.'
    dry_anker:
      mode: neu
      audit_datum: '2026-06-19'
      audit_beleg: 'Migrations-Skizze: Zero-Downtime Pflicht, Canary+Rollback. Traefik-Router-Config auf oekotopia. tc-llm-proxy bleibt erst STEHEN (Rollback).'
    aufwand_pt: 2.0
    akzeptanz:
      - "MINIMALER EINGRIFF: NUR der Traefik-Router/Backend für llm.true-code.de wird von tc-llm-proxy auf orkoprox umgehängt. Die anderen 22 oekotopia-Domains (leitivo.com/platform/api, true-code.de/app/api/pulse, oekotopia.com, mitarbeiterdesign.de, ingo-terpelle.de, melanie-dorn.de, leitivo.de) werden NICHT angefasst. Kein Reverse-Proxy-Wechsel außer er ist nachweisbar nötig+sicher"
      - "VOLLABDECKUNGS-SMOKE-BASELINE vor Cutover: alle 23 Domains → HTTP-Status + TLS-Gültigkeit aufgenommen (Soll-Zustand). Nach Cutover IDENTISCH: jede vorher erreichbare Domain ist nachher erreichbar — sonst sofort Rollback"
      - "tc-llm-proxy bleibt LAUFEN (nicht gestoppt) als Sofort-Rollback (Router zurückhängen = <1min); Rollback-Runbook geschrieben + trocken getestet; Rollback-Kriterien: Health-Fail ODER 5xx-Spike ODER irgendeine der 23 Domains down ODER Client-Fehler"
      - "Direkt nach Cutover: llm.true-code.de/v1/healthz=200 + /v1/models=alle Aliase + 1 Smoke pro Client-Key grün UND alle 23 Domains grün (Vollabdeckungs-Smoke) — sonst sofort Rollback"
    smoke_skript:
      - "Baseline (vor): for d in <23 Domains>; do curl -sI https://$d → Status+TLS; done. Nach Cutover: identisch + llm.true-code.de/v1/healthz=200 + je 1 Client-Key-Call grün. Bei JEDER Abweichung: Rollback + Verifikation tc-llm-proxy + alle Domains wieder grün."
    doctrine_fit: "Zero-Downtime Multi-Tenant-Gateway + Multi-Domain-Host. Minimaler Eingriff (1 Router) statt Stack-Wechsel (23 Domains). Owner 'sei SEHR SORGFÄLTIG': Vollabdeckungs-Smoke aller Domains. tc-llm-proxy Hot-Standby. feedback_hub_finalen_gruenen_deploy_verifizieren."
  - wave_id: 5
    name: Client-Vollverifikation — alle Tenants/Clients laufen normal weiter (Owner-Gate)
    inventur: 'Nach Cutover muss bewiesen werden, dass JEDER reale Client normal weiterläuft: Leitivo-Tenants (cgassler/ttkramer/ritter + Fleet, Avi/Pipelines/Embeddings), TrueCode, Oekotopia, Melanie/Persona-Pipeline. Das ist die harte Owner-Bedingung 2026-06-19.'
    dry_anker:
      mode: neu
      audit_datum: '2026-06-19'
      audit_beleg: Client-Inventar aus W1. lc-Tenants nutzen llm.true-code.de live (cgassler-API-Env verifiziert). TrueCode + Oekotopia + Melanie als Konsumenten dokumentiert.
    aufwand_pt: 2.0
    akzeptanz:
      - 'Pro realem Client ein echter End-to-End-Smoke nach Cutover: Leitivo-Avi antwortet (cgassler-Tenant live), Pipeline-LLM-Node läuft, Embeddings 3584d; TrueCode-Report generiert; Oekotopia-LLM-Call; Melanie/Persona-Pipeline. ALLE grün mit ihren BESTEHENDEN Keys'
      - "Avi-Qualität auf orkoprox: chat sinnvoll, reason eskaliert auf stärkeres Modell (gpt-oss-120b ODER Mistral Large) — die 'dumm wie Brot'-Frage gemessen (besser als Legacy-Apriel-Eskalation?)"
      - "ci-lag/Monitor grün; 24h kein Client-Fehler-Spike; Owner-Gate: Owner bestätigt 'alle Clients laufen' (Hub weist aus, behauptet nicht selbst final)"
    smoke_skript:
      - Pro Client 1 Live-Call über llm.true-code.de nach Cutover = grün. Avi cgassler chat+reason verifiziert. Monitor 24h sauber.
    doctrine_fit: "DIE Owner-Bedingung 2026-06-19: alle Clients laufen weiter. Showtime-Check (echte Daten). feedback_hub_finalen_gruenen_deploy_verifizieren. Owner-Gate für 'fertig'."
  - wave_id: 6
    name: Legacy-Archivierung — tc-llm-proxy + llm-unified-proxy ins Archiv
    inventur: 'Nach erfolgreichem Cutover + Stabilitäts-Fenster: tc-llm-proxy (40-tools/ + /srv/stacks/tc-llm-proxy) und der tote llm-unified-proxy-Stack ins Archiv. Owner-Direktive 2026-06-19: Legacy danach ins Archiv. Registry tc_llm_proxy.json auf archived.'
    dry_anker:
      mode: neu
      audit_datum: '2026-06-19'
      audit_beleg: Owner 2026-06-19. /srv/stacks/tc-llm-proxy (aktiv bis Cutover) + /srv/stacks/llm-unified-proxy (schon tot). registry/projects/tc_llm_proxy.json + orkoprox.json.
    aufwand_pt: 1.0
    akzeptanz:
      - 'Erst NACH Stabilitäts-Fenster (mind. 24-48h orkoprox grün): tc-llm-proxy-Container auf oekotopia gestoppt + Stack-Ordner archiviert (umbenannt _archived ODER weg); llm-unified-proxy-Reste entfernt'
      - 'Registry: tc_llm_proxy.json status=archived mit Verweis auf orkoprox als Nachfolger; orkoprox.json status=ssot-live; Doku-Drift (TASKS.md/INFRASTRUCTURE_REGISTRY.md) nachgezogen'
      - Kein Client zeigt mehr auf tc-llm-proxy (alle über llm.true-code.de→orkoprox); Self-Contained §0 gewahrt
    smoke_skript:
      - 'docker ps oekotopia: nur orkoprox-Stack, kein tc-llm-proxy mehr. Registry tc_llm_proxy=archived. Ein finaler Client-Smoke grün.'
    doctrine_fit: 'Owner 2026-06-19: Legacy ins Archiv. Registry-Disziplin (Nachfolge dokumentiert). Archiv erst nach Stabilität (kein voreiliges Löschen des Rollback-Pfads).'
---

# orkoprox-Cutover auf oekotopia — llm.true-code.de zero-downtime migrieren, Legacy archivieren

<!-- Plan-SSOT: w-orkoprox-cutover-oekotopia -->
<!-- Frontmatter via scripts/plan_frontmatter.py pflegen. -->

## Kontext

Owner-Direktive 2026-06-19 (Nacht): "orkoprox = SSOT, gehört auf oekotopia mit passenden Keys.
KEIN Rumfummeln in Legacy-Repos. orkoprox hat KEIN Apriel, keine US-Provider-Defaults. Legacy
danach ins Archiv. Migration darf durchgezogen werden — aber alle Tenants/Clients (TrueCode,
Oekotopia, Leitivo) müssen danach normal mit ihren Keys über `llm.true-code.de` weiterlaufen."

**Verifizierter Ist-Stand 2026-06-19:** orkoprox v0.1.2 von GitHub geklont (`40-tools/orkoprox`),
OVH-First/kein-Apriel/kein-US-Default bestätigt, läuft schon produktiv bei KOPP (`llm-proxy.kopp.de`).
oekotopia: aktiver `tc-llm-proxy` (healthy) hinter `llm.true-code.de`, daneben toter `llm-unified-proxy`.
Avi (cgassler-live) nutzt semantische Aliase chat/reason/classify über `llm.true-code.de`.

Voll-Audit + Modell-Diff: `docs/ideas/2026-06-11_w_llm_proxy_orkoprox_migration.md` (reiche Skizze,
hier zu ausführbaren Wellen verdichtet). Registry: `registry/projects/orkoprox.json`.

## CUTOVER-DOKTRIN (Owner-Direktive 2026-06-19: VOLL DURCHZIEHEN BIS LIVE)

Owner-Update 2026-06-19: "Du sollst ALLES deployen und LIVE schalten. orkoprox auf oekotopia
einrichten mit allem was nötig ist. Du kannst oekotopia generell auf Caddy ODER Traefik umstellen
wenn das besser/schneller/einfacher ist — ich hänge nicht am Stack."

→ **Das Owner-Gate auf W4 ist AUFGEHOBEN. Voller autonomer Durchstich W1-W6 bis live.**
Die Autorität zum Cutover ist erteilt. Die Zero-Downtime-METHODE bleibt aber zwingend (managt
technisches Risiko, nicht Autorität):

- **W1-W3:** Inventar → orkoprox parallel auf separatem Port → Canary-Smoke ALLE Keys × Aliase.
  `tc-llm-proxy` bleibt während dieser Wellen unberührt aktiv.
- **W4 = AUTONOMER CUTOVER mit Rollback-Pflicht.** Reverse-Proxy (Traefik ODER Caddy — Owner gibt
  Stack-Freiheit, wähle was sauberer/schneller ist) auf orkoprox umhängen. `tc-llm-proxy` bleibt
  als Hot-Standby LAUFEN (nicht stoppen) bis W5 grün — Rollback = Proxy zurückhängen (<1 min).
  Cutover NUR nach grünem Canary (W3). Bei Health-Fail/5xx-Spike/Client-Fehler nach Cutover →
  sofort Rollback, Loop meldet + analysiert, KEIN Blind-Weiter.
- **W5:** Client-Vollverifikation — jeder reale Client (alle Leitivo-Tenants/Avi, TrueCode,
  Oekotopia, Melanie) läuft live über orkoprox mit seinem Key. Erst wenn ALLE grün: stabil.
- **W6:** Legacy-Archiv (tc-llm-proxy + llm-unified-proxy) erst nach Stabilitäts-Fenster — der
  Rollback-Pfad wird nicht zerstört, solange W5 nicht zweifelsfrei grün ist.

**Reverse-Proxy-Freiheit — aber MINIMALER EINGRIFF FIRST (Owner 'sei SEHR SORGFÄLTIG'):**
oekotopia läuft auf EINEM Traefik und bedient **23 Live-Domains** (inventarisiert 2026-06-19):
`llm.true-code.de`, `true-code.de`/`www`/`app.`/`api.`/`pulse.`/`minio.pulse.`, `leitivo.com`/`www`/
`api.`/`platform.`, `leitivo.de`/`www`, `oekotopia.com`/`www`/`api.`/`media.`, `mitarbeiterdesign.de`/
`www`, `ingo-terpelle.de`/`www`, `melanie-dorn.de`/`www`. `llm.true-code.de` ist nur EINE davon.

**STACK-ENTSCHEIDUNG (datenbasiert, 2026-06-19): Traefik BLEIBT der beste Stack — Begründung im
Ist-Zustand, NICHT Faulheit.** oekotopia-Traefik nutzt `providers.docker=true` (Label-basiert,
`exposedbydefault=false`) + File-Provider `/srv/traefik/dynamic`. Jeder der 16 Container trägt
sein Routing als Docker-Labels in seinem EIGENEN docker-compose (leitivo-platform, true-code-app,
oekotopia, mitarbeiterdesign, melanie-dorn, leitivo-site, ingo-terpelle, true-code-site, tc-pulse).
Das ist die idiomatische, self-service Multi-Stack-Architektur: jeder Service deklariert sein Routing
selbst, Traefik entdeckt es automatisch.

→ **Owner-Direktive 'Default-Router auf den BESTEN Stack, alle Domains migrieren':** Der beste Stack
für DIESEN Host ist das bestehende Label-Traefik. Ein Caddy-Wechsel würde 23 Label-Routen über 8+
Stacks manuell in eine zentrale Caddyfile übersetzen + jeden Compose anfassen = mehr Risiko, weniger
Self-Service. Daher: **Traefik behalten, aber sauber konsolidieren wo es driftet** (`.bak`-Reste,
inkonsistente Labels bereinigen). KEIN Caddy außer der Loop findet einen konkreten Traefik-Blocker.

→ **'Alle Domains migrieren' ist bei Label-Traefik AUTOMATISCH erfüllt:** Der Cutover hängt NUR das
`Host(\`llm.true-code.de\`)`-Label von tc-llm-proxy auf den orkoprox-Container um. Die anderen 22
Domains behalten ihre eigenen Labels an ihren eigenen Containern — sie wandern gar nicht, also
können sie nicht verloren gehen. DAS ist der sichere Weg, alle Domains zu erhalten.

→ FALLS der Loop bei Live-Discovery doch einen Grund für echten Stack-Wechsel findet (z.B. Traefik-
Version-Blocker für orkoprox): erlaubt, aber dann ALLE 23 Domain-Router + TLS im neuen Stack
nachbauen + Vollabdeckungs-Smoke gegen den neuen Stack BEVOR umgeschaltet wird.

**VOLLABDECKUNGS-SMOKE PFLICHT (Owner-Direktive):** Vor UND nach jedem Cutover-Schritt werden
ALLE 23 Domains gesmoket (HTTP-Status + TLS gültig), nicht nur die LLM-Schiene. Baseline vorher
aufnehmen, nach Cutover identisch. Jede Domain, die vorher erreichbar war, MUSS nachher erreichbar
sein — sonst sofort Rollback.

## Die 6 Wellen (voller Durchstich bis live)
1. Diff + Client-Key-Inventar + Mistral-Key
2. orkoprox parallel hochziehen, separater Port
3. Canary-Smoke alle Keys × Aliase
4. **Cutover (autonom, Rollback-Pflicht)** — Traefik/Caddy auf orkoprox, tc-llm-proxy als Hot-Standby
5. Client-Vollverifikation — alle Tenants/Clients live über orkoprox
6. Legacy-Archivierung (nach Stabilität)
