# oekotopia Domain-Inventar (Baseline für orkoprox-Cutover, 2026-06-19)

**Zweck:** Vollabdeckungs-Smoke-Baseline. Vor UND nach jedem Cutover-Schritt MÜSSEN alle diese
Domains erreichbar sein (HTTP-Status + TLS gültig). Quelle: live `ssh oekotopia` docker inspect.

**Reverse-Proxy:** EIN `traefik:latest`, `providers.docker=true` (Label-basiert, exposedbydefault=false)
+ File-Provider `/srv/traefik/dynamic` (Mount `/srv/traefik/dynamic:/dynamic:ro`).

## Router → Backend-Container (16 Container, 23 Domains)

| Domain(s) | Backend-Container | Stack |
|---|---|---|
| `platform.leitivo.com` | leitivo-platform-web-1 | leitivo-platform |
| `api.leitivo.com` | leitivo-platform-api-1 | leitivo-platform |
| `leitivo.com` / `www` / `leitivo.de` / `www.leitivo.de` | leitivo-site | leitivo-site |
| `app.true-code.de` | true-code-app-web-1 | true-code-app |
| `api.true-code.de` | true-code-app-api-1 | true-code-app |
| `true-code.de` / `www.true-code.de` | true-code-site | true-code-site |
| `pulse.true-code.de` | tc-pulse-api-1 | tc-pulse |
| `minio.pulse.true-code.de` | tc-pulse-minio-1 | tc-pulse |
| **`llm.true-code.de`** | **tc-llm-proxy-llm-proxy-1 → CUTOVER-ZIEL orkoprox** | tc-llm-proxy → orkoprox |
| `oekotopia.com` / `www` | oekotopia-web-1 | oekotopia |
| `api.oekotopia.com` | oekotopia-api-1 (+ node-exporter) | oekotopia |
| `media.oekotopia.com` | oekotopia-minio-1 | oekotopia |
| `mitarbeiterdesign.de` / `www` | mitarbeiterdesign-preview-web | mitarbeiterdesign |
| `melanie-dorn.de` / `www` | melanie-dorn-web | melanie-dorn |
| `ingo-terpelle.de` / `www` | ingo-terpelle-web | ingo-terpelle |

## Cutover-Prinzip (Label-Traefik)

NUR `Host(\`llm.true-code.de\`)` wandert von tc-llm-proxy-Container auf orkoprox-Container
(Label-Swap). Alle anderen 22 Domains behalten ihre Labels an ihren eigenen Containern → null Risiko
für sie. tc-llm-proxy-Container bleibt LAUFEN (Label entfernt, Container Hot-Standby) bis W5 grün.

## Smoke-Liste (alle 23, copy-paste-fähig)

```
platform.leitivo.com api.leitivo.com leitivo.com www.leitivo.com leitivo.de www.leitivo.de
app.true-code.de api.true-code.de true-code.de www.true-code.de pulse.true-code.de
minio.pulse.true-code.de llm.true-code.de oekotopia.com www.oekotopia.com api.oekotopia.com
media.oekotopia.com mitarbeiterdesign.de www.mitarbeiterdesign.de melanie-dorn.de
www.melanie-dorn.de ingo-terpelle.de www.ingo-terpelle.de
```
