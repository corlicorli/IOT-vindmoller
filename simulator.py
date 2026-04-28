"""Live simulator — genererer en ny måling for hver enhed hvert N sekund.

Køres som baggrundstask fra FastAPI's lifespan (main.py).
"""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timezone

import alerts as alerts_service
import db
from physics import TickState, next_tick, scenario_for

logger = logging.getLogger(__name__)


@dataclass
class SimulatorStatus:
    running: bool = False
    interval_seconds: float = 5.0
    ticks: int = 0
    rows_inserted: int = 0
    last_tick_at: datetime | None = None
    last_error: str | None = None
    device_count: int = 0


status = SimulatorStatus()


async def _load_states() -> list[tuple[str, str, TickState]]:
    """Liste af (device_id, park_id, state) fra DB, i fast rækkefølge."""
    out: list[tuple[str, str, TickState]] = []
    cursor = db.devices().find({}, {"_id": 1, "park_id": 1}).sort("_id", 1)
    async for dev in cursor:
        last = await db.metrics().find_one(
            {"device_id": dev["_id"]},
            sort=[("timestamp", -1)],
            projection={"wind_speed_ms": 1},
        )
        wind = last["wind_speed_ms"] if last else 8.0
        out.append((dev["_id"], dev["park_id"], TickState(wind=wind, temp_drift=0.0)))
    return out


async def run(interval_seconds: float = 5.0) -> None:
    rng = random.Random()
    states = await _load_states()

    status.running = True
    status.interval_seconds = interval_seconds
    status.device_count = len(states)

    if not states:
        status.last_error = "Ingen enheder — kør 'python seed.py' først."
        status.running = False
        return

    try:
        while True:
            await asyncio.sleep(interval_seconds)
            ts = datetime.now(timezone.utc).replace(microsecond=0)
            hour = ts.hour

            docs = []
            for idx, (device_id, park_id, state) in enumerate(states):
                sc = scenario_for(idx)
                wind, power, rpm, temp = next_tick(state, sc, rng, hour)
                docs.append(
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

            try:
                await db.metrics().insert_many(docs, ordered=False)
                await db.devices().update_many(
                    {"_id": {"$in": [d["device_id"] for d in docs]}},
                    {"$set": {"last_ping": ts}},
                )
                await alerts_service.evaluate_and_persist(docs)
                status.rows_inserted += len(docs)
                status.ticks += 1
                status.last_tick_at = ts
                status.last_error = None
            except Exception as e:  # noqa: BLE001
                status.last_error = f"{type(e).__name__}: {e}"
                logger.exception("Simulator-tick fejlede")
    except asyncio.CancelledError:
        status.running = False
        raise
