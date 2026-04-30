"""Test-fixtures.

Kræver en kørende MongoDB. Lokalt: `docker compose up -d mongo`.
I CI: leveret som service container i workflow.

OBS: env-vars skal sættes FØR `db` importeres (load_dotenv kører ved import),
ellers vil .env's Atlas-URL trumfe vores test-konfiguration.
"""
from __future__ import annotations

import os

# --- Test-environment (sættes inden moduler der læser env importeres) -------
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
    for coll in (
        db.metrics(),
        db.alerts(),
        db.notifications(),
        db.devices(),
        db.parks(),
    ):
        await coll.delete_many({})

    transport = ASGITransport(app=main.app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
    finally:
        db.close()


@pytest_asyncio.fixture
async def seeded(client):
    """Onboard 1 park + 1 device via det offentlige API — som en kunde ville gøre.

    Returnerer timestamp så kalder kan bruge det til at indsætte historik.
    """
    park_resp = await client.post(
        "/parks",
        json={
            "park_id": "PARK-ALB",
            "name": "Aalborg Nord",
            "region": "Nordjylland",
            "lat": 57.05,
            "lng": 9.92,
        },
    )
    assert park_resp.status_code == 201, f"Park-create failed: {park_resp.text}"

    device_resp = await client.post(
        "/parks/PARK-ALB/devices",
        json={
            "device_id": "IOT-DK-ALB-001",
            "wind_turbine_id": "WTG-ALB-001",
            "firmware_version": "v2.4.1",
            "battery_level": 90,
            "signal_strength": -70,
            "last_error_code": "00",
        },
    )
    assert device_resp.status_code == 201, f"Device-create failed: {device_resp.text}"

    return datetime.now(timezone.utc).replace(microsecond=0)
