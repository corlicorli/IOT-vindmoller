"""Integration-tests for FastAPI-endpoints (kræver MongoDB)."""
from __future__ import annotations

import pytest

import db


pytestmark = pytest.mark.integration


async def test_health_returns_mongo_true(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"mongo": True}


async def test_root_exposes_metadata(client):
    r = await client.get("/")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "Wind Farm API"
    assert body["alert_threshold_c"] == 70.0


async def test_root_does_not_leak_credentials(client):
    """Regression: connection-URL må aldrig eksponeres via HTTP."""
    r = await client.get("/")
    body_raw = r.text
    assert "mongodb://" not in body_raw
    assert "mongodb+srv" not in body_raw
    assert "mongo" not in r.json(), "mongo-feltet er fjernet for at undgå credential-lækage"


async def test_post_metrics_normal_does_not_create_alert(client, seeded):
    r = await client.post(
        "/metrics",
        json={
            "device_id": "IOT-DK-ALB-001",
            "wind_speed_ms": 8.0,
            "power_output_kw": 1500.0,
            "rotor_rpm": 12.0,
            "gearbox_temp_c": 55.0,
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["device_id"] == "IOT-DK-ALB-001"
    assert body["park_id"] == "PARK-ALB"
    assert "timestamp" in body

    # Ingen anomali → ingen alarm-event persisteret
    assert await db.alerts().count_documents({}) == 0


async def test_post_metrics_above_threshold_persists_alert_event(client, seeded):
    r = await client.post(
        "/metrics",
        json={
            "device_id": "IOT-DK-ALB-001",
            "wind_speed_ms": 12.0,
            "power_output_kw": 2400.0,
            "rotor_rpm": 15.0,
            "gearbox_temp_c": 78.5,  # > WARN_SEVERITY_C → CRITICAL
        },
    )
    assert r.status_code == 201

    persisted = [a async for a in db.alerts().find()]
    assert len(persisted) == 1
    a = persisted[0]
    assert a["event_type"] == "ANOMALY_DETECTED"
    assert a["severity"] == "CRITICAL"
    assert a["device_id"] == "IOT-DK-ALB-001"
    assert a["park_id"] == "PARK-ALB"
    assert a["rule"] == "gearbox_temp_c > 70"


async def test_post_metrics_unknown_device_is_rejected(client):
    r = await client.post(
        "/metrics",
        json={
            "device_id": "DOES-NOT-EXIST",
            "wind_speed_ms": 8.0,
            "power_output_kw": 1500.0,
            "rotor_rpm": 12.0,
            "gearbox_temp_c": 55.0,
        },
    )
    assert r.status_code == 400


async def test_alerts_history_endpoint_returns_persisted_events(client, seeded):
    # Skab et event via API'et
    await client.post(
        "/metrics",
        json={
            "device_id": "IOT-DK-ALB-001",
            "wind_speed_ms": 14.0,
            "power_output_kw": 2500.0,
            "rotor_rpm": 16.0,
            "gearbox_temp_c": 82.0,
        },
    )

    r = await client.get("/monitoring/alerts/history")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["event_type"] == "ANOMALY_DETECTED"
    assert rows[0]["severity"] == "CRITICAL"
    # Filter på park_id
    r2 = await client.get("/monitoring/alerts/history?park_id=PARK-ALB")
    assert r2.status_code == 200
    assert len(r2.json()) == 1
    r3 = await client.get("/monitoring/alerts/history?park_id=PARK-XYZ")
    assert r3.status_code == 200
    assert r3.json() == []


async def test_post_metrics_validation_rejects_negative_wind(client, seeded):
    r = await client.post(
        "/metrics",
        json={
            "device_id": "IOT-DK-ALB-001",
            "wind_speed_ms": -1.0,
            "power_output_kw": 100.0,
            "rotor_rpm": 5.0,
            "gearbox_temp_c": 50.0,
        },
    )
    assert r.status_code == 422
