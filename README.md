# IOT Vindmøller — Wind Farm API

REST-API til overvågning af 3 fiktive vindmølleparker for **Intelligent IoT Solutions A/S** (KEA 6. semester case).

Stack: **FastAPI** + **MongoDB Atlas** + **Motor** (async driver).

## Funktioner

- 3 parker (Aalborg Nord, Esbjerg Vest, Thy Klit) med 5–7 møller hver (18 i alt)
- Live-simulator der genererer en ny måling pr. mølle hvert 5. sekund
- Realistisk turbine-fysik (power-kurve, gearkasse-temp afhænger af last og scenarie)
- Managed Services-alarmer når gearkasse-temp > 70°C
- Park-totaler (samlet effekt, gns. vind, max temp)
- TTL-index — gamle metrics slettes automatisk efter 30 dage

## Quick start

```bash
# 1. Klon og opret virtuelt miljø
git clone <repo-url>
cd IOT-vindmoller
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Opret .env (kopier fra eksempel og udfyld MONGO_URL fra Atlas)
cp .env.example .env
# rediger .env og indsæt din MongoDB-connection-string

# 3. Seed databasen med parker, møller og 7 dages historik
python seed.py

# 4. Start API'et (live-simulator starter automatisk)
uvicorn main:app --reload
```

Åbn http://127.0.0.1:8000/docs

## Endpoints

| Endpoint | Beskrivelse |
|---|---|
| `GET /parks` | Alle vindmølleparker |
| `GET /parks/{park_id}/devices` | Møller i en park |
| `GET /devices` | Alle IoT-enheder (status- og fejldata) |
| `GET /metrics?device_id=…&limit=` | Drifts- og tilstandsdata |
| `POST /metrics` | Modtag måling fra en IoT-enhed |
| `GET /monitoring/alerts` | Aktive alarmer (>70°C) |
| `GET /monitoring/park-summary` | Totaler pr. park |
| `GET /monitoring/simulator` | Live-simulatorens status |

## Projektstruktur

```
main.py        — FastAPI app + endpoints
db.py          — MongoDB connection (Motor)
models.py      — Pydantic-modeller
physics.py     — Turbine-fysik og scenarier
seed.py        — Bootstrap historiske data
simulator.py   — Live-tikker (baggrundstask)
```

## Datamodel

- `parks` — vindmølleparker
- `devices` — IoT-enheder (én pr. mølle), inkl. firmware/batteri/fejlkode
- `metrics` — tidsserie med drift- og tilstandsdata
