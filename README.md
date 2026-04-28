# IOT Vindmøller — Wind Farm API

REST-API til overvågning af 3 fiktive vindmølleparker for **Intelligent IoT Solutions A/S** (KEA 6. semester, Afleveringsopgave 2 — Teknisk MVP).

**Stack:** FastAPI · MongoDB · Motor (async driver) · Docker · GitHub Actions CI

[![CI](https://github.com/corlicorli/IOT-vindmoller/actions/workflows/ci.yml/badge.svg)](https://github.com/corlicorli/IOT-vindmoller/actions/workflows/ci.yml)

## Domænet kort

**Predictive Maintenance** for vindmølleparker — implementeret i alle tre klassiske PdM-lag:

```
Lag 1: Dataindsamling     Sensor Value Received   ─► metrics-collection
                          (POST /metrics)

Lag 2: Anomaly detection  Threshold-regel:        ─► alerts-collection
                          gearbox_temp_c > 70°C    ─► WARNING-log
                          (alerts.py)              ─► severity: CRITICAL/WARNING

Lag 3: Trend-forudsigelse Lineær regression       ─► /monitoring/predictions
                          på sidste 7 dages temp.  ─► ETA til threshold-brud
                          (predictions.py)         ─► risk-klassifikation
```

## Funktioner

- 3 parker (Aalborg Nord, Esbjerg Vest, Thy Klit) med 5-7 møller hver (18 i alt)
- Live-simulator der genererer en ny måling pr. mølle hvert 5. sekund
- Realistisk turbine-fysik med scenarie-baseret slidtage (overheat_drift +2°C/dag, aging +0.7°C/dag)
- Domain event-log: `Anomaly Detected` events persisteres separat med severity og rule
- **Trend-baseret forudsigelse**: lineær regression giver ETA til threshold-brud + risk-klassifikation
- TTL-indekser — metrics og alerts slettes automatisk efter 30 dage
- 35 automatiserede tests (22 unit + 13 integration) kørt i GitHub Actions CI

## Krav

- **Docker Desktop** (anbefalet vej) — eller **Python 3.12+** + en MongoDB-instans

## Quick start

### Vej A — Docker (anbefalet, ~30 sekunder)

Hele stacken (API + MongoDB + mongo-express UI) startes med én kommando:

```bash
git clone https://github.com/corlicorli/IOT-vindmoller.git
cd IOT-vindmoller
docker compose up --build -d

# Seed databasen med 3 parker, 18 møller og 1 dags historik
docker compose exec api python seed.py --days 1 --interval 30
```

| Service | URL | Til hvad |
|---|---|---|
| API + Swagger UI | http://localhost:8000/docs | Test endpoints |
| mongo-express | http://localhost:8081 | Browse 4 collections live |
| MongoDB | `localhost:27017` | Direkte mongosh-adgang |

### Vej B — Lokal Python + Atlas

```bash
git clone https://github.com/corlicorli/IOT-vindmoller.git
cd IOT-vindmoller
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Rediger .env: indsæt din egen MongoDB Atlas connection-string
# OBS: Atlas kræver at din IP er whitelisted under Network Access

python seed.py --days 1 --interval 30
uvicorn main:app --reload
```

## Test

```bash
pip install -r requirements-dev.txt
pytest -v
```

Forventet output: `35 passed`. Tests kræver en kørende MongoDB; integration-tests skipper pænt hvis intet er tilgængeligt på `localhost:27017`. Den nemmeste vej er at lade Docker-stacken stå oppe mens man kører pytest.

CI kører automatisk ved push til `main` — se [Actions-fanen](https://github.com/corlicorli/IOT-vindmoller/actions).

## Eksempler — eventflow live

Mens API'et kører, prøv:

```bash
# Sundheds-tjek
curl http://localhost:8000/health
# → {"mongo": true}

# Send en normal måling (ingen anomali)
curl -X POST http://localhost:8000/metrics \
  -H 'content-type: application/json' \
  -d '{"device_id":"IOT-DK-ALB-001","wind_speed_ms":8,"power_output_kw":1500,
       "rotor_rpm":12,"gearbox_temp_c":55}'

# Send en måling der overskrider threshold (88°C)
curl -X POST http://localhost:8000/metrics \
  -H 'content-type: application/json' \
  -d '{"device_id":"IOT-DK-ALB-001","wind_speed_ms":15,"power_output_kw":2500,
       "rotor_rpm":16,"gearbox_temp_c":88}'

# Se det persisterede Anomaly Detected event (lag 2)
curl http://localhost:8000/monitoring/alerts/history | jq

# Se PdM-forudsigelser (lag 3) — sorteret med højest risiko først
curl http://localhost:8000/monitoring/predictions | jq

# Forudsigelse for én konkret mølle
curl http://localhost:8000/monitoring/predictions/IOT-DK-ALB-002 | jq

# Se park-totaler
curl http://localhost:8000/monitoring/park-summary | jq
```

Eksempel-output fra `/monitoring/predictions` efter `seed.py --days 14`:

```json
[
  { "device_id": "IOT-DK-ALB-002", "risk": "HIGH",
    "current_temp_c": 91.3, "trend_c_per_day": 8.62, ... },
  { "device_id": "IOT-DK-ALB-001", "risk": "MEDIUM",
    "current_temp_c": 58.5, "trend_c_per_day": 8.0,
    "days_until_breach": 1.4, "eta_threshold_breach": "2026-04-29T..." }
]
```

## Endpoints

| Endpoint | Metode | Beskrivelse |
|---|---|---|
| `/health` | GET | Liveness-tjek (Mongo-ping) |
| `/` | GET | Service-metadata |
| `/parks`, `/parks/{id}`, `/parks/{id}/devices` | GET | Park-info |
| `/devices`, `/devices/{id}` | GET | IoT-enhed status (firmware, batteri, fejlkode) |
| `/metrics` | GET | Tidsserie-data (`?device_id=`, `?park_id=`, `?limit=`) |
| **`/metrics`** | **POST** | **Modtag måling fra IoT-enhed — trigger event-flowet** |
| `/monitoring/alerts` | GET | **Lag 2** — live-view: aktuelle møller med temp > 70°C |
| `/monitoring/alerts/history` | GET | **Lag 2** — persisteret event-log af alle Anomaly Detected events |
| **`/monitoring/predictions`** | **GET** | **Lag 3 — trend-baseret forudsigelse pr. mølle med ETA og risk** |
| **`/monitoring/predictions/{id}`** | **GET** | **Lag 3 for én mølle** |
| `/monitoring/park-summary` | GET | Totaler pr. park (effekt, gns. vind, max temp) |
| `/monitoring/simulator` | GET | Status på live-simulatoren |
| `/docs` | GET | Auto-genereret Swagger UI |

## Datamodel — 4 MongoDB collections

| Collection | Indhold | Type |
|---|---|---|
| `parks` | Vindmølleparker | Domain entities |
| `devices` | IoT-enheder pr. mølle (firmware, batteri, signal, error-kode) | Domain entities |
| `metrics` | Tidsserie af drift-data | **`Sensor Value Received` event-log** |
| `alerts` | Threshold-overskridelser med severity og rule | **`Anomaly Detected` event-log** |

TTL-indekser sletter `metrics` og `alerts` ældre end 30 dage automatisk.

## Konfiguration (env-vars)

| Variabel | Default | Beskrivelse |
|---|---|---|
| `MONGO_URL` | `mongodb://localhost:27017` | Connection-string |
| `MONGO_DB` | `iot_solutions` | Database-navn |
| `SIMULATOR_ENABLED` | `1` | Sæt `0` for at slå live-tikken fra (bruges i tests) |
| `SIMULATOR_INTERVAL` | `5` | Sekunder mellem ticks |
| `METRIC_RETENTION_DAYS` | `30` | TTL for metrics og alerts |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

Læses fra `.env` ved opstart. Værdier sat i shell-environment trumfer `.env`.

## Projektstruktur

```
main.py            — FastAPI app, lifespan, alle endpoints
alerts.py          — Lag 2: Threshold-regel + persistering af Anomaly Detected events
predictions.py     — Lag 3: Lineær regression på temperatur-historik (ETA + risk)
db.py              — Motor MongoDB-klient + collection-helpers + indexer
models.py          — Pydantic-skemaer for I/O-validering
physics.py         — Turbine-fysik og driftsscenarier (incl. lineær drift over tid)
simulator.py       — Live-tikker som baggrundstask
seed.py            — Bootstrap historiske data
tests/             — pytest-suite (35 tests: unit + integration)
Dockerfile         — Production-image (non-root, healthcheck)
docker-compose.yml — Hele stacken (api + mongo + mongo-express)
.github/workflows/ — CI: pytest + Docker build
```

## Mapping til opgavekrav

| PDF-krav (Afleveringsopgave 2) | Implementering |
|---|---|
| Predictive Maintenance / notifikation som core subdomain | Implementeret i 3 lag (data, anomaly, prediction) |
| Events: Sensor Value Received, Anomaly Detected | Persisteret i `metrics` og `alerts` collections |
| Beslutninger: Threshold-check, alarm/notifikation | `alerts.py` (her og nu) + `predictions.py` (fremadrettet) |
| Event → beslutning → handling | `POST /metrics` → threshold-regel → persistér event + log |
| REST API tilgås remote (IoT device) | FastAPI på `0.0.0.0:8000`, dokumenteret i `/docs` |
| Domæne-logik (validering, hændelse, beslutning) | Pydantic-validering, anomaly-event, severity + risk-klassifikation |
| Persistering af domain events i separat DB | MongoDB med 4 collections, 2 dedikerede event-logs |
| CI/CD med GitHub/Docker | GitHub Actions: pytest + Docker-build på hver push |
