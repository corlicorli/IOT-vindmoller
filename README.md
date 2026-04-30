# IOT Vindmøller — Wind Farm API

REST-API platform til vindmølleparker for **Intelligent IoT Solutions A/S** (KEA 6. semester, Afleveringsopgave 2 — Teknisk MVP).

Kunder registrerer deres parker og IoT-enheder via API'et og uploader sensor-målinger fra deres møller. Systemet udfører trin-baseret anomaly detection, persisterer domain events, dispatcher operator-notifikationer ved kritiske alarmer, og laver trend-baserede forudsigelser om kommende fejl.

**Stack:** FastAPI · MongoDB · Motor · Docker · Prometheus · Grafana · GitHub Actions CI

[![CI](https://github.com/corlicorli/IOT-vindmoller/actions/workflows/ci.yml/badge.svg)](https://github.com/corlicorli/IOT-vindmoller/actions/workflows/ci.yml)

## Domæne — Predictive Maintenance i 3 lag

```
Lag 1: Dataindsamling     POST /metrics                  ─► metrics-collection
                          POST /metrics/bulk              ─► (Sensor Value Received)

Lag 2: Anomaly detection  Threshold: gearbox_temp > 70°C ─► alerts-collection
       + Notification     CRITICAL → operator-webhook    ─► notifications-collection
                                                            (SENT/FAILED/SKIPPED)

Lag 3: Trend-forudsigelse Lineær regression på           ─► /monitoring/predictions
                          7 dages historik                ─► ETA + risk-klassifikation
```

Plus: **API observability** via Prometheus + Grafana — viser request-rate, latency-percentiler, error rate.

## Arkitektur

```
┌─────────────┐       POST /parks, /devices, /metrics       ┌────────────────┐
│   Kunde     │ ────────────────────────────────────────►   │   FastAPI      │
│  (IoT/Pi/   │                                              │   :8000         │
│   Postman)  │ ◄────────────────────────────────────────── │                 │
└─────────────┘       JSON responses                         │                 │
                                                              │  ┌──────────┐ │
┌─────────────┐  CRITICAL alert webhook                      │  │ alerts   │ │
│  Operator   │ ◄────────────────────────────────────────── │  │ predict  │ │
│  Vagtsystem │                                              │  └──────────┘ │
└─────────────┘                                              └────────────────┘
                                                                       │
                                                                       ▼
                                                              ┌────────────────┐
                                                              │   MongoDB      │
                                                              │ parks/devices  │
                                                              │ metrics/alerts │
                                                              │ notifications  │
                                                              └────────────────┘
                                                                       ▲
                                                                       │ scrape
┌─────────────┐  Prometheus queries     ┌────────────────┐    ┌────────────────┐
│   Grafana   │ ─────────────────────►  │  Prometheus    │ ── │ /observability │
│   :3001     │                          │   :9090         │    │   /metrics     │
│  dashboards │  Infinity (REST polling) ──────────────► API endpoints
└─────────────┘
```

## Krav

- **Docker Desktop** (anbefalet) — eller **Python 3.12+** + en kørende MongoDB

## Quick start (Docker)

Hele stacken (API + MongoDB + mongo-express + Prometheus + Grafana) starter med én kommando:

```bash
git clone https://github.com/corlicorli/IOT-vindmoller.git
cd IOT-vindmoller
docker compose up --build -d
```

Stacken starter **tom** — ingen pre-seedet data. Kunden bygger sin overvågning op selv via API'et.

| Service | URL | Til hvad |
|---|---|---|
| **API + Swagger UI** | http://localhost:8000/docs | Test endpoints interaktivt |
| **Wind Farm Operations dashboard** | http://localhost:3001/d/wind-farm-ops | Live PdM-status (alle 3 lag) |
| **API Observability dashboard** | http://localhost:3001/d/api-observability | Request-rate, latency, errors |
| Prometheus | http://localhost:9090 | Rå metric-queries |
| mongo-express | http://localhost:8081 | Browse MongoDB |
| MongoDB | `localhost:27017` | mongosh-adgang |

## Onboarding — kunden registrerer sin første park

```bash
# 1. Opret en park
curl -X POST http://localhost:8000/parks \
  -H 'content-type: application/json' \
  -d '{
    "park_id": "PARK-AALBORG-NORD",
    "name": "Aalborg Nord",
    "region": "Nordjylland",
    "lat": 57.05,
    "lng": 9.92
  }'

# 2. Registrer en mølle på parken
curl -X POST http://localhost:8000/parks/PARK-AALBORG-NORD/devices \
  -H 'content-type: application/json' \
  -d '{
    "device_id": "IOT-DK-AAL-001",
    "wind_turbine_id": "WTG-AAL-001",
    "firmware_version": "v2.4.1"
  }'

# 3. Send en måling fra IoT-enheden
curl -X POST http://localhost:8000/metrics \
  -H 'content-type: application/json' \
  -d '{
    "device_id": "IOT-DK-AAL-001",
    "wind_speed_ms": 14,
    "power_output_kw": 2400,
    "rotor_rpm": 16,
    "gearbox_temp_c": 88
  }'

# 4. Se den persisterede Anomaly Detected event
curl http://localhost:8000/monitoring/alerts/history | jq

# 5. Se operator-notifications dispatch-historik
curl http://localhost:8000/monitoring/notifications | jq
```

### Postman-collection

Fuldt onboarding-flow er klar til import: [`postman/wind-farm-api.postman_collection.json`](postman/wind-farm-api.postman_collection.json) — 24 requests fordelt på Setup → Onboarding → Demo-flow → Monitoring → Cleanup.

### Demo-data populator (valgfri)

For at få et fyldt dashboard hurtigt — populér 14 dages historik via API'et:

```bash
docker compose exec api python scripts/populate_demo.py --days 14 --interval 30
```

Scriptet bruger udelukkende det offentlige API (`POST /parks`, `POST /parks/X/devices`, `POST /metrics/bulk`) — præcis som en kunde med en eksisterende fleet ville gøre. **I produktion vil hver måling i stedet komme fra en rigtig IoT-enhed.**

## Operator-notifikation ved CRITICAL alarmer

Når en CRITICAL alert udløses (gearkasse > 75°C), sendes en webhook til operatørens vagt-system:

```bash
# Sæt webhook URL i .env eller docker-compose
OPERATOR_WEBHOOK_URL=https://operator.example.com/iot-alerts
```

Payload:
```json
{
  "event_type": "ANOMALY_NOTIFICATION",
  "device_id": "IOT-DK-AAL-001",
  "park_id": "PARK-AALBORG-NORD",
  "severity": "CRITICAL",
  "gearbox_temp_c": 88.0,
  "timestamp": "2026-04-30T12:34:56+00:00",
  "rule": "gearbox_temp_c > 70",
  "action_required": "INSPECT_TURBINE"
}
```

Resultatet (SENT/FAILED/SKIPPED) persisteres i `notifications`-collection og kan ses via `GET /monitoring/notifications`. Til demo-formål kan du bruge https://webhook.site eller `nc -l 9999`.

Konfiguration:
- `OPERATOR_WEBHOOK_URL` — destination (uden = SKIPPED, kun logget)
- `NOTIFY_SEVERITIES` — default `CRITICAL`. Sæt til `CRITICAL,WARNING` for alle alarmer
- `WEBHOOK_TIMEOUT_S` — default 3.0 sek

## Test

```bash
pip install -r requirements-dev.txt
pytest -v
```

Forventet output: `55 passed`. Tests kræver kørende MongoDB (Docker eller lokal). Integration-tests skipper pænt hvis DB ikke er tilgængelig.

CI kører automatisk ved push til `main` — se [Actions-fanen](https://github.com/corlicorli/IOT-vindmoller/actions).

## Endpoints

### Kunde-registrering (CRUD)
| Endpoint | Metode | Beskrivelse |
|---|---|---|
| `/parks` | POST | Registrer ny park |
| `/parks` | GET | List alle parker |
| `/parks/{park_id}` | GET | Hent én park (turbine_count beregnet) |
| `/parks/{park_id}` | DELETE | Slet park + cascade devices/metrics/alerts |
| `/parks/{park_id}/devices` | GET | Møller på en park |
| `/parks/{park_id}/devices` | POST | Registrer ny mølle på en park |
| `/devices`, `/devices/{id}` | GET | Mølle-info |
| `/devices/{device_id}` | DELETE | Slet mølle + cascade metrics/alerts |

### IoT-data
| Endpoint | Metode | Beskrivelse |
|---|---|---|
| `/metrics` | POST | Modtag enkelt måling fra IoT-enhed (lag 1) |
| `/metrics/bulk` | POST | Batch-upload — for IoT-gateways der buffrer |
| `/metrics` | GET | Tidsserie-data (`?device_id=`, `?park_id=`, `?limit=`) |

### Monitoring (PdM 3-lag + notifikation)
| Endpoint | Metode | Beskrivelse |
|---|---|---|
| `/monitoring/alerts` | GET | **Lag 2** — live aktive alarmer |
| `/monitoring/alerts/history` | GET | **Lag 2** — Anomaly Detected event-log |
| `/monitoring/notifications` | GET | **Notification dispatch-historik** (SENT/FAILED/SKIPPED) |
| `/monitoring/predictions` | GET | **Lag 3** — trend-baseret forudsigelse pr. mølle |
| `/monitoring/predictions/{id}` | GET | **Lag 3** for én mølle |
| `/monitoring/stats` | GET | Aggregeret status (driver Grafana-dashboard) |
| `/monitoring/park-summary` | GET | Totaler pr. park |
| `/health` | GET | Liveness-tjek |
| `/observability/metrics` | GET | Prometheus-format API-metrics |
| `/docs` | GET | Auto-genereret Swagger UI |

## Datamodel — 5 MongoDB collections

| Collection | Indhold | Kategori |
|---|---|---|
| `parks` | Vindmølleparker | Domain entity |
| `devices` | IoT-enheder pr. mølle | Domain entity |
| `metrics` | Sensor-målinger (tidsserie) | **Sensor Value Received event-log** |
| `alerts` | Threshold-overskridelser | **Anomaly Detected event-log** |
| `notifications` | Webhook dispatch-resultater | **Notification audit-log** |

TTL-indekser sletter `metrics`, `alerts`, `notifications` ældre end 30 dage automatisk.

## Konfiguration (env-vars)

| Variabel | Default | Beskrivelse |
|---|---|---|
| `MONGO_URL` | `mongodb://localhost:27017` | Connection-string |
| `MONGO_DB` | `iot_solutions` | Database-navn |
| `METRIC_RETENTION_DAYS` | `30` | TTL for metrics, alerts, notifications |
| `OPERATOR_WEBHOOK_URL` | _(unset)_ | Destination for CRITICAL alert-notifikationer |
| `NOTIFY_SEVERITIES` | `CRITICAL` | Komma-separeret liste, fx `CRITICAL,WARNING` |
| `WEBHOOK_TIMEOUT_S` | `3.0` | HTTP-timeout for webhook-dispatch |
| `LOG_LEVEL` | `INFO` | DEBUG/INFO/WARNING/ERROR |

## Projektstruktur

```
main.py            — FastAPI app, lifespan, alle endpoints
alerts.py          — Lag 2: Threshold-regel + persistering af Anomaly Detected events
predictions.py     — Lag 3: Lineær regression på temperatur-historik
notifications.py   — Operator-notifikation via webhook ved CRITICAL alerts
db.py              — Motor MongoDB-klient + collection-helpers + indexer
models.py          — Pydantic-skemaer for I/O-validering
physics.py         — Turbine-fysik (kun brugt af populate_demo.py)
scripts/
  populate_demo.py — VALGFRI demo-data populator via HTTP API
tests/             — pytest-suite (55 tests: unit + integration)
Dockerfile         — Production-image (non-root, healthcheck)
docker-compose.yml — Hele stacken (api + mongo + mongo-express + prometheus + grafana)
prometheus/        — Prometheus scrape-konfig
grafana/
  dashboards/      — Wind Farm Operations + API Observability dashboards
  provisioning/    — Auto-provisioneret datasources (Infinity + Prometheus)
postman/           — Importerbar Postman-collection
.github/workflows/ — CI: pytest + Docker build
```

## Mapping til opgavekrav

| Krav (Afleveringsopgave 2) | Implementering |
|---|---|
| Predictive Maintenance / notifikation core subdomain | 3 lag: data → anomaly → prediction. Plus webhook-notifikation til operatør |
| Events: Sensor Value Received, Anomaly Detected | `metrics` og `alerts` collections — separate domain events |
| Beslutninger: Threshold-check, alarm/notifikation | `alerts.py` (regel) + `notifications.py` (dispatch) |
| Event → beslutning → handling | POST /metrics → threshold → persistér event + log + webhook |
| REST API tilgås remote | FastAPI på `0.0.0.0:8000`, fuld kunde-CRUD via Postman |
| Domæne-logik (validering, hændelse, beslutning) | Pydantic, anomaly-event, severity + risk-klassifikation, cascade-delete |
| Persistering af domain events i separat DB | MongoDB med 5 collections, 3 dedikerede event/audit-logs |
| Kvalitet og drift — monitorering | **Wind Farm Operations dashboard** (PdM-status) + **API Observability dashboard** (request-rate, latency, errors) — begge auto-provisioneret i Grafana |
| CI/CD med GitHub/Docker | GitHub Actions: pytest + Docker-build på hver push |
