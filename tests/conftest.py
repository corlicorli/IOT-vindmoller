"""Test-fixtures.

Kræver en kørende MongoDB. Lokalt: `docker compose up -d mongo`.
I CI: leveret som service container i workflow.

OBS: env-vars skal sættes FØR `db` importeres (load_dotenv kører ved import),
ellers vil .env's Atlas-URL trumfe vores test-konfiguration.
"""
from __future__ import annotations

import os

# --- Test-environment (sættes inden moduler der læser env importeres) -------
os.environ["SIMULATOR_ENABLED"] = "0"
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("MONGO_DB", "iot_solutions_test")

from datetime import datetime, timezone  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

import db  # noqa: E402
import main  # noqa: E402


@pytest_asyncio.fixture
async def client():
    """HTTP-klient + ren test-database pr. test.

    Lukker Motor-klienten ved teardown — Motor binder klienten til den event
    loop hvor den blev oprettet, og pytest-asyncio laver ny loop pr. test.
    """
    if not await db.ping():
        pytest.skip("MongoDB ikke tilgængelig — start med `docker compose up -d mongo`")
    await db.init_indexes()
    for coll in (db.metrics(), db.alerts(), db.devices(), db.parks()):
        await coll.delete_many({})

    transport = ASGITransport(app=main.app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
    finally:
        db.close()


@pytest_asyncio.fixture
async def seeded(client):
    """Indsæt minimum park + device til metric-tests."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    await db.parks().insert_one(
        {
            "_id": "PARK-ALB",
            "name": "Aalborg Nord",
            "region": "Nordjylland",
            "lat": 57.05,
            "lng": 9.92,
            "turbine_count": 1,
        }
    )
    await db.devices().insert_one(
        {
            "_id": "IOT-DK-ALB-001",
            "park_id": "PARK-ALB",
            "wind_turbine_id": "WTG-ALB-001",
            "firmware_version": "v2.4.1",
            "battery_level": 90,
            "signal_strength": -70,
            "last_error_code": "00",
            "last_ping": now,
        }
    )
    return now
