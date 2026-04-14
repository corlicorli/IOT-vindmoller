"""Seed MongoDB med 3 vindmølleparker, deres enheder, og historiske målinger.

Bruger:
    python seed.py                        # default: 3 parker, 7 dage, 10-min historik
    python seed.py --days 14 --interval 5
    python seed.py --append               # behold eksisterende
"""
from __future__ import annotations

import argparse
import asyncio
import random
from datetime import datetime, timedelta, timezone

import db
from physics import FIRMWARES, OK_ERROR, TickState, next_tick, scenario_for

# --- Park-definitioner -------------------------------------------------------

PARKS = [
    {
        "_id": "PARK-ALB",
        "name": "Aalborg Nord",
        "region": "Nordjylland",
        "lat": 57.05,
        "lng": 9.92,
        "turbine_count": 7,
    },
    {
        "_id": "PARK-ESB",
        "name": "Esbjerg Vest",
        "region": "Syddanmark",
        "lat": 55.47,
        "lng": 8.45,
        "turbine_count": 6,
    },
    {
        "_id": "PARK-THY",
        "name": "Thy Klit",
        "region": "Nordjylland",
        "lat": 56.96,
        "lng": 8.30,
        "turbine_count": 5,
    },
]


def park_code(park_id: str) -> str:
    return park_id.split("-")[1]  # PARK-ALB -> ALB


async def run(days: int, interval_min: int, append: bool) -> None:
    if not await db.ping():
        raise SystemExit(
            f"❌ Kan ikke nå MongoDB på {db.MONGO_URL}.\n"
            "   Start den først (se README)."
        )

    await db.init_indexes()
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = now - timedelta(days=days)
    total_steps = int((days * 24 * 60) / interval_min)

    rng = random.Random(42)

    if not append:
        await db.metrics().delete_many({})
        await db.devices().delete_many({})
        await db.parks().delete_many({})

    # Indsæt parker
    await db.parks().insert_many(PARKS)

    devices_docs = []
    metrics_docs = []
    global_idx = 0  # bruges til at fordele scenarier over alle møller

    for park in PARKS:
        park_id = park["_id"]
        code = park_code(park_id)

        for n in range(1, park["turbine_count"] + 1):
            device_id = f"IOT-DK-{code}-{n:03d}"
            wtg_id = f"WTG-{code}-{n:03d}"

            sc = scenario_for(global_idx)
            global_idx += 1

            fw = rng.choice(FIRMWARES)
            battery_start = rng.randint(60, 100)
            battery_end = max(5, int(battery_start - sc.battery_drain * total_steps))
            signal = rng.randint(-95, -55)
            last_error = sc.base_error if rng.random() < 0.7 else OK_ERROR

            devices_docs.append(
                {
                    "_id": device_id,
                    "park_id": park_id,
                    "wind_turbine_id": wtg_id,
                    "firmware_version": fw,
                    "battery_level": battery_end,
                    "signal_strength": signal,
                    "last_error_code": last_error,
                    "last_ping": now,
                }
            )

            state = TickState(wind=rng.uniform(4.0, 10.0), temp_drift=0.0)
            for step in range(total_steps):
                ts = start + timedelta(minutes=interval_min * step)
                wind, power, rpm, temp = next_tick(state, sc, rng, ts.hour)
                metrics_docs.append(
                    {
                        "device_id": device_id,
                        "park_id": park_id,
                        "timestamp": ts,
                        "wind_speed_ms": wind,
                        "power_output_kw": power,
                        "rotor_rpm": rpm,
                        "gearbox_temp_c": temp,
                    }
                )

    await db.devices().insert_many(devices_docs)
    # batch metrics i chunks af 5000 for at undgå store BSON-operationer
    for i in range(0, len(metrics_docs), 5000):
        await db.metrics().insert_many(metrics_docs[i : i + 5000])

    print(
        f"✓ {len(PARKS)} parker, {len(devices_docs)} møller, "
        f"{len(metrics_docs):,} historiske målinger "
        f"({days} dage @ {interval_min} min)"
    )
    db.close()


def main() -> None:
    p = argparse.ArgumentParser(description="Seed MongoDB med fake vindmøllepark-data.")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--interval", type=int, default=10, help="minutter mellem historiske målinger")
    p.add_argument("--append", action="store_true")
    args = p.parse_args()

    asyncio.run(run(args.days, args.interval, append=args.append))


if __name__ == "__main__":
    main()
