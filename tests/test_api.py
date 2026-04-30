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


# --- Predictive maintenance (lag 3) -----------------------------------------


async def test_predictions_empty_when_no_history(client, seeded):
    """Med kun seed-fixture (ingen historik) er der ikke nok datapunkter."""
    r = await client.get("/monitoring/predictions")
    assert r.status_code == 200
    assert r.json() == []


async def test_predictions_unknown_device_returns_404(client, seeded):
    r = await client.get("/monitoring/predictions/NOT-A-DEVICE")
    assert r.status_code == 404


async def test_predictions_detects_upward_trend(client, seeded):
    """Indsæt syntetisk opadgående trend og verificer at vi forudsiger den."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc).replace(microsecond=0)
    # 50 datapunkter spredt over 5 dage med ~1°C/dag opadgående trend
    docs = []
    for i in range(50):
        days_ago = 5 - (i / 49) * 5  # 5.0 → 0.0
        ts = now - timedelta(days=days_ago)
        temp = 50.0 + (5.0 - days_ago) * 1.0  # 50°C → 55°C
        docs.append(
            {
                "device_id": "IOT-DK-ALB-001",
                "park_id": "PARK-ALB",
                "timestamp": ts,
                "wind_speed_ms": 8.0,
                "power_output_kw": 1500.0,
                "rotor_rpm": 12.0,
                "gearbox_temp_c": temp,
            }
        )
    await db.metrics().insert_many(docs)

    r = await client.get("/monitoring/predictions/IOT-DK-ALB-001")
    assert r.status_code == 200
    body = r.json()
    assert body["device_id"] == "IOT-DK-ALB-001"
    assert body["park_id"] == "PARK-ALB"
    assert body["datapoints"] == 50
    assert 0.5 < body["trend_c_per_day"] < 1.5  # forventet ~1.0
    assert body["days_until_breach"] is not None
    assert body["days_until_breach"] > 0
    assert body["eta_threshold_breach"] is not None
    assert body["risk"] in ("MEDIUM", "HIGH")


async def test_predictions_stable_device_has_no_eta(client, seeded):
    """En mølle uden trend bør ikke få days_until_breach."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc).replace(microsecond=0)
    docs = [
        {
            "device_id": "IOT-DK-ALB-001",
            "park_id": "PARK-ALB",
            "timestamp": now - timedelta(hours=i),
            "wind_speed_ms": 8.0,
            "power_output_kw": 1500.0,
            "rotor_rpm": 12.0,
            "gearbox_temp_c": 55.0,  # konstant
        }
        for i in range(40)
    ]
    await db.metrics().insert_many(docs)

    r = await client.get("/monitoring/predictions/IOT-DK-ALB-001")
    assert r.status_code == 200
    body = r.json()
    assert body["days_until_breach"] is None
    assert body["eta_threshold_breach"] is None
    assert body["risk"] == "LOW"


async def test_stats_endpoint_empty_database(client):
    """Med tom DB skal stats returnere nuller, ikke fejle."""
    r = await client.get("/monitoring/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["parks"] == 0
    assert body["devices"] == 0
    assert body["active_alerts"]["total"] == 0
    assert body["predictions"]["high_risk"] == 0
    assert body["sensor_readings"]["total"] == 0
    assert body["anomaly_events"]["total"] == 0


async def test_stats_endpoint_with_seed_and_alert(client, seeded):
    """Stats skal afspejle seed + en POSTet anomaly-måling.

    Vigtigt: sensor_readings tæller ALLE målinger (lag 1).
    anomaly_events tæller kun threshold-overskridelser (lag 2 — delmængde).
    """
    # Trigger en anomaly via API
    await client.post(
        "/metrics",
        json={
            "device_id": "IOT-DK-ALB-001",
            "wind_speed_ms": 14.0,
            "power_output_kw": 2400.0,
            "rotor_rpm": 15.0,
            "gearbox_temp_c": 82.0,
        },
    )

    r = await client.get("/monitoring/stats")
    assert r.status_code == 200
    body = r.json()

    assert body["parks"] == 1
    assert body["devices"] == 1
    # 82 > 75 → CRITICAL
    assert body["active_alerts"]["critical"] == 1
    assert body["active_alerts"]["warning"] == 0
    assert body["active_alerts"]["total"] == 1
    # 1 reading (lag 1 — Sensor Value Received)
    assert body["sensor_readings"]["total"] == 1
    assert body["sensor_readings"]["last_24h"] == 1
    # 1 anomaly event (lag 2 — Anomaly Detected, delmængde af reading)
    assert body["anomaly_events"]["total"] == 1
    assert body["anomaly_events"]["last_24h"] == 1


async def test_predictions_list_sorts_high_risk_first(client, seeded):
    """Indsæt to møller — én med stærk trend, én stabil. HIGH/MEDIUM skal komme før LOW."""
    from datetime import datetime, timedelta, timezone

    # Tilføj endnu en device til park
    await db.devices().insert_one(
        {
            "_id": "IOT-DK-ALB-002",
            "park_id": "PARK-ALB",
            "wind_turbine_id": "WTG-ALB-002",
            "firmware_version": "v2.4.1",
            "battery_level": 90,
            "signal_strength": -70,
            "last_error_code": "00",
            "last_ping": datetime.now(timezone.utc),
        }
    )

    now = datetime.now(timezone.utc).replace(microsecond=0)
    # Mølle 1: stærkt stigende
    rising = [
        {
            "device_id": "IOT-DK-ALB-001",
            "park_id": "PARK-ALB",
            "timestamp": now - timedelta(hours=5 - i / 8),
            "wind_speed_ms": 8.0,
            "power_output_kw": 1500.0,
            "rotor_rpm": 12.0,
            "gearbox_temp_c": 50.0 + i * 0.3,  # ~7°C på 5 dage = ~1.4°C/dag
        }
        for i in range(40)
    ]
    # Mølle 2: stabil
    stable = [
        {
            "device_id": "IOT-DK-ALB-002",
            "park_id": "PARK-ALB",
            "timestamp": now - timedelta(hours=5 - i / 8),
            "wind_speed_ms": 8.0,
            "power_output_kw": 1500.0,
            "rotor_rpm": 12.0,
            "gearbox_temp_c": 55.0,
        }
        for i in range(40)
    ]
    await db.metrics().insert_many(rising + stable)

    r = await client.get("/monitoring/predictions")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 2
    # Stigende mølle skal stå først (højest risiko)
    assert rows[0]["device_id"] == "IOT-DK-ALB-001"
    assert rows[1]["device_id"] == "IOT-DK-ALB-002"
    assert rows[0]["risk"] in ("MEDIUM", "HIGH")
    assert rows[1]["risk"] == "LOW"
