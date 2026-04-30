"""Demo data populator — VALGFRI script der fylder demo-data via HTTP API.

Tænk på det som et SaaS-onboarding-script: præcis hvad en kunde ville køre
for at registrere deres første park + møller og uploade lidt historisk data.
Bruger kun det offentlige API — ingen direkte database-skrivning.

I produktion ville hver måling i stedet komme fra en rigtig IoT-enhed via
POST /metrics. Dette script er kun til demo og lokal udvikling.

Bruger:
    python scripts/populate_demo.py                           # default
    python scripts/populate_demo.py --base-url http://...    # peg på deploy'd API
    python scripts/populate_demo.py --days 7 --interval 60   # 7 dage, 60-min ticks
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Importer physics fra projektets rod uanset hvor scriptet kaldes fra
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from physics import (  # noqa: E402
    FIRMWARES,
    OK_ERROR,
    TickState,
    next_tick,
    scenario_for,
)


# --- Demo park-definitioner --------------------------------------------------

PARKS = [
    {
        "park_id": "PARK-AALBORG-NORD",
        "name": "Aalborg Nord",
        "region": "Nordjylland",
        "lat": 57.05,
        "lng": 9.92,
        "device_count": 7,
    },
    {
        "park_id": "PARK-ESBJERG-VEST",
        "name": "Esbjerg Vest",
        "region": "Syddanmark",
        "lat": 55.47,
        "lng": 8.45,
        "device_count": 6,
    },
    {
        "park_id": "PARK-THY-KLIT",
        "name": "Thy Klit",
        "region": "Nordjylland",
        "lat": 56.96,
        "lng": 8.30,
        "device_count": 5,
    },
]


def park_code(park_id: str) -> str:
    """'PARK-AALBORG-NORD' -> 'AAL'."""
    return park_id.split("-")[1][:3]


def populate(
    base_url: str,
    days: int,
    interval_min: int,
    *,
    timeout_seconds: float = 30.0,
) -> None:
    rng = random.Random(42)
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = now - timedelta(days=days)
    total_steps = int((days * 24 * 60) / interval_min)

    with httpx.Client(base_url=base_url, timeout=timeout_seconds) as http:
        # Sundhedstjek først — fail fast hvis API'et ikke kører
        health = http.get("/health")
        health.raise_for_status()
        if not health.json().get("mongo"):
            sys.exit(f"❌ {base_url}/health rapporterer mongo=false — start MongoDB først")

        # 1. Opret parker via POST /parks (eller skip hvis allerede oprettet)
        device_index = 0
        for park in PARKS:
            payload = {k: v for k, v in park.items() if k != "device_count"}
            r = http.post("/parks", json=payload)
            if r.status_code == 409:
                print(f"  ⚠ Park {park['park_id']} eksisterer allerede — springer over")
            else:
                r.raise_for_status()
                print(f"  ✓ Park oprettet: {park['park_id']}")

        # 2. Opret devices via POST /parks/X/devices
        all_devices: list[tuple[str, str, TickState, int]] = []
        for park in PARKS:
            code = park_code(park["park_id"])
            for n in range(1, park["device_count"] + 1):
                device_id = f"IOT-DK-{code}-{n:03d}"
                payload = {
                    "device_id": device_id,
                    "wind_turbine_id": f"WTG-{code}-{n:03d}",
                    "firmware_version": rng.choice(FIRMWARES),
                    "battery_level": rng.randint(60, 100),
                    "signal_strength": rng.randint(-95, -55),
                    "last_error_code": OK_ERROR,
                }
                r = http.post(f"/parks/{park['park_id']}/devices", json=payload)
                if r.status_code == 409:
                    print(f"  ⚠ Device {device_id} eksisterer allerede — springer over")
                else:
                    r.raise_for_status()
                    print(f"  ✓ Device oprettet: {device_id}")

                state = TickState(wind=rng.uniform(4.0, 10.0), temp_drift=0.0)
                all_devices.append((device_id, park["park_id"], state, device_index))
                device_index += 1

        # 3. Generér historiske målinger og POST dem i bulk-batches
        print(
            f"\n  Genererer {total_steps:,} ticks × {len(all_devices)} enheder "
            f"= {total_steps * len(all_devices):,} historiske målinger…"
        )

        BATCH_SIZE = 500
        batch: list[dict] = []
        sent = 0
        t0 = time.time()

        for step in range(total_steps):
            ts = start + timedelta(minutes=interval_min * step)
            cumulative_days = (ts - start).total_seconds() / 86400.0

            for device_id, _park_id, state, idx in all_devices:
                sc = scenario_for(idx)
                wind, power, rpm, temp = next_tick(
                    state, sc, rng, ts.hour, cumulative_days
                )
                batch.append(
                    {
                        "device_id": device_id,
                        "wind_speed_ms": wind,
                        "power_output_kw": power,
                        "rotor_rpm": rpm,
                        "gearbox_temp_c": temp,
                        # Klient-side timestamp så historik fordeles korrekt
                        # over tid (en IoT-gateway der buffrer ville gøre samme)
                        "timestamp": ts.isoformat(),
                    }
                )

                if len(batch) >= BATCH_SIZE:
                    r = http.post("/metrics/bulk", json={"metrics": batch})
                    r.raise_for_status()
                    sent += len(batch)
                    batch = []

        if batch:
            r = http.post("/metrics/bulk", json={"metrics": batch})
            r.raise_for_status()
            sent += len(batch)

        elapsed = time.time() - t0
        print(
            f"\n✓ {len(PARKS)} parker, {len(all_devices)} møller, "
            f"{sent:,} historiske målinger uploaded via API "
            f"({days} dage @ {interval_min} min, {elapsed:.1f}s)"
        )


def main() -> None:
    p = argparse.ArgumentParser(
        description="Fyld demo-data ind via HTTP API (valgfrit)."
    )
    p.add_argument("--base-url", default="http://localhost:8000",
                   help="API base URL (default: http://localhost:8000)")
    p.add_argument("--days", type=int, default=14,
                   help="Antal dages historik der genereres (default: 14)")
    p.add_argument("--interval", type=int, default=30,
                   help="Minutter mellem historiske ticks (default: 30)")
    args = p.parse_args()
    populate(args.base_url, args.days, args.interval)


if __name__ == "__main__":
    main()
