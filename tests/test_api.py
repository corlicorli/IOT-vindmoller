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


# --- Customer-facing CRUD (kunde-onboarding) -------------------------------


async def test_create_park_succeeds_with_valid_payload(client):
    r = await client.post(
        "/parks",
        json={
            "park_id": "PARK-NEW-1",
            "name": "Test Park",
            "region": "Nordjylland",
            "lat": 57.0,
            "lng": 9.5,
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["_id"] == "PARK-NEW-1"
    assert body["name"] == "Test Park"
    assert body["turbine_count"] == 0


async def test_create_park_rejects_duplicate_id(client):
    payload = {
        "park_id": "PARK-DUPE",
        "name": "X",
        "region": "Y",
        "lat": 0,
        "lng": 0,
    }
    assert (await client.post("/parks", json=payload)).status_code == 201
    r = await client.post("/parks", json=payload)
    assert r.status_code == 409


async def test_create_park_rejects_invalid_id_format(client):
    r = await client.post(
        "/parks",
        json={
            "park_id": "lowercase-bad",  # skal starte med stort bogstav
            "name": "X",
            "region": "Y",
            "lat": 0,
            "lng": 0,
        },
    )
    assert r.status_code == 422


async def test_create_park_rejects_invalid_coordinates(client):
    r = await client.post(
        "/parks",
        json={
            "park_id": "PARK-OOB",
            "name": "X",
            "region": "Y",
            "lat": 999,  # uden for [-90, 90]
            "lng": 0,
        },
    )
    assert r.status_code == 422


async def test_create_device_attaches_to_existing_park(client, seeded):
    r = await client.post(
        "/parks/PARK-ALB/devices",
        json={
            "device_id": "IOT-DK-ALB-NEW",
            "wind_turbine_id": "WTG-ALB-NEW",
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["_id"] == "IOT-DK-ALB-NEW"
    assert body["park_id"] == "PARK-ALB"
    assert body["firmware_version"] == "v1.0.0"  # default
    assert body["battery_level"] == 100  # default


async def test_create_device_returns_404_when_park_unknown(client):
    r = await client.post(
        "/parks/PARK-NOT-EXIST/devices",
        json={
            "device_id": "IOT-DK-X-001",
            "wind_turbine_id": "WTG-X-001",
        },
    )
    assert r.status_code == 404


async def test_create_device_rejects_duplicate(client, seeded):
    payload = {"device_id": "IOT-DK-DUP", "wind_turbine_id": "WTG-DUP"}
    assert (await client.post("/parks/PARK-ALB/devices", json=payload)).status_code == 201
    r = await client.post("/parks/PARK-ALB/devices", json=payload)
    assert r.status_code == 409


async def test_list_parks_includes_computed_turbine_count(client, seeded):
    # seeded opretter 1 device → turbine_count=1
    r = await client.get("/parks")
    assert r.status_code == 200
    parks = r.json()
    assert len(parks) == 1
    assert parks[0]["turbine_count"] == 1

    # Tilføj endnu et device → 2
    await client.post(
        "/parks/PARK-ALB/devices",
        json={"device_id": "IOT-DK-ALB-002", "wind_turbine_id": "WTG-ALB-002"},
    )
    r2 = await client.get("/parks")
    assert r2.json()[0]["turbine_count"] == 2


async def test_delete_park_cascades_devices_metrics_alerts(client, seeded):
    # Skab et metric (og dermed evt. alert)
    await client.post(
        "/metrics",
        json={
            "device_id": "IOT-DK-ALB-001",
            "wind_speed_ms": 14.0,
            "power_output_kw": 2400.0,
            "rotor_rpm": 15.0,
            "gearbox_temp_c": 85.0,
        },
    )
    assert await db.devices().count_documents({"park_id": "PARK-ALB"}) == 1
    assert await db.metrics().count_documents({"park_id": "PARK-ALB"}) == 1
    assert await db.alerts().count_documents({"park_id": "PARK-ALB"}) == 1

    r = await client.delete("/parks/PARK-ALB")
    assert r.status_code == 204

    assert await db.parks().count_documents({"_id": "PARK-ALB"}) == 0
    assert await db.devices().count_documents({"park_id": "PARK-ALB"}) == 0
    assert await db.metrics().count_documents({"park_id": "PARK-ALB"}) == 0
    assert await db.alerts().count_documents({"park_id": "PARK-ALB"}) == 0


async def test_delete_park_returns_404_when_unknown(client):
    r = await client.delete("/parks/PARK-NOT-EXIST")
    assert r.status_code == 404


async def test_delete_device_cascades_metrics_alerts(client, seeded):
    await client.post(
        "/metrics",
        json={
            "device_id": "IOT-DK-ALB-001",
            "wind_speed_ms": 14.0,
            "power_output_kw": 2400.0,
            "rotor_rpm": 15.0,
            "gearbox_temp_c": 85.0,
        },
    )
    r = await client.delete("/devices/IOT-DK-ALB-001")
    assert r.status_code == 204
    assert await db.devices().count_documents({"_id": "IOT-DK-ALB-001"}) == 0
    assert await db.metrics().count_documents({"device_id": "IOT-DK-ALB-001"}) == 0
    assert await db.alerts().count_documents({"device_id": "IOT-DK-ALB-001"}) == 0


# --- Bulk metrics endpoint ---------------------------------------------------


async def test_bulk_metrics_inserts_all_and_creates_alerts(client, seeded):
    # Tilføj endnu et device så vi har to at bulk-uploade fra
    await client.post(
        "/parks/PARK-ALB/devices",
        json={"device_id": "IOT-DK-ALB-002", "wind_turbine_id": "WTG-ALB-002"},
    )

    payload = {
        "metrics": [
            {
                "device_id": "IOT-DK-ALB-001",
                "wind_speed_ms": 12,
                "power_output_kw": 2000,
                "rotor_rpm": 14,
                "gearbox_temp_c": 65,
            },
            {
                "device_id": "IOT-DK-ALB-002",
                "wind_speed_ms": 14,
                "power_output_kw": 2500,
                "rotor_rpm": 16,
                "gearbox_temp_c": 82,  # CRITICAL
            },
        ]
    }
    r = await client.post("/metrics/bulk", json=payload)
    assert r.status_code == 201
    body = r.json()
    assert body["metrics_inserted"] == 2
    assert body["alerts_created"] == 1  # kun en var > 70°C


async def test_bulk_metrics_rejects_unknown_device(client, seeded):
    r = await client.post(
        "/metrics/bulk",
        json={
            "metrics": [
                {
                    "device_id": "IOT-DK-ALB-001",
                    "wind_speed_ms": 8,
                    "power_output_kw": 1000,
                    "rotor_rpm": 10,
                    "gearbox_temp_c": 50,
                },
                {
                    "device_id": "IOT-DK-UNKNOWN",
                    "wind_speed_ms": 8,
                    "power_output_kw": 1000,
                    "rotor_rpm": 10,
                    "gearbox_temp_c": 50,
                },
            ]
        },
    )
    assert r.status_code == 400
    # Hele batchen afvises — første gyldige måling må IKKE persisteres
    assert await db.metrics().count_documents({}) == 0


async def test_bulk_metrics_rejects_empty_batch(client):
    r = await client.post("/metrics/bulk", json={"metrics": []})
    assert r.status_code == 422


# --- Operator notifications (webhook dispatch) ------------------------------


async def test_critical_alert_persists_skipped_notification_when_no_webhook(
    client, seeded, monkeypatch
):
    """Uden OPERATOR_WEBHOOK_URL skal CRITICAL alerts persistere SKIPPED notification."""
    monkeypatch.delenv("OPERATOR_WEBHOOK_URL", raising=False)
    await client.post(
        "/metrics",
        json={
            "device_id": "IOT-DK-ALB-001",
            "wind_speed_ms": 14,
            "power_output_kw": 2400,
            "rotor_rpm": 15,
            "gearbox_temp_c": 88,  # CRITICAL
        },
    )
    notifs = [n async for n in db.notifications().find()]
    assert len(notifs) == 1
    assert notifs[0]["severity"] == "CRITICAL"
    assert notifs[0]["status"] == "SKIPPED"
    assert notifs[0]["device_id"] == "IOT-DK-ALB-001"


async def test_warning_alert_does_not_trigger_notification_by_default(
    client, seeded, monkeypatch
):
    """Default-konfig sender kun ved CRITICAL — ikke WARNING."""
    monkeypatch.delenv("OPERATOR_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("NOTIFY_SEVERITIES", raising=False)
    await client.post(
        "/metrics",
        json={
            "device_id": "IOT-DK-ALB-001",
            "wind_speed_ms": 12,
            "power_output_kw": 2000,
            "rotor_rpm": 14,
            "gearbox_temp_c": 72,  # WARNING (>70 men <75)
        },
    )
    assert await db.notifications().count_documents({}) == 0


async def test_notifications_endpoint_returns_history(client, seeded, monkeypatch):
    monkeypatch.delenv("OPERATOR_WEBHOOK_URL", raising=False)
    # Skab tre CRITICAL alerts
    for temp in (82, 85, 88):
        await client.post(
            "/metrics",
            json={
                "device_id": "IOT-DK-ALB-001",
                "wind_speed_ms": 14,
                "power_output_kw": 2400,
                "rotor_rpm": 15,
                "gearbox_temp_c": temp,
            },
        )

    r = await client.get("/monitoring/notifications")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 3
    assert all(n["severity"] == "CRITICAL" for n in rows)
    assert all(n["status"] == "SKIPPED" for n in rows)


async def test_notifications_endpoint_filters_by_status(
    client, seeded, monkeypatch
):
    monkeypatch.delenv("OPERATOR_WEBHOOK_URL", raising=False)
    await client.post(
        "/metrics",
        json={
            "device_id": "IOT-DK-ALB-001",
            "wind_speed_ms": 14,
            "power_output_kw": 2400,
            "rotor_rpm": 15,
            "gearbox_temp_c": 85,
        },
    )
    r = await client.get("/monitoring/notifications?status=SENT")
    assert r.json() == []
    r2 = await client.get("/monitoring/notifications?status=SKIPPED")
    assert len(r2.json()) == 1


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
    # Mølle 1: stærkt stigende — 40 punkter spredt over 5 dage med ~2°C/dag drift
    rising = [
        {
            "device_id": "IOT-DK-ALB-001",
            "park_id": "PARK-ALB",
            "timestamp": now - timedelta(days=5 - i / 8),
            "wind_speed_ms": 8.0,
            "power_output_kw": 1500.0,
            "rotor_rpm": 12.0,
            "gearbox_temp_c": 50.0 + i * 0.3,
        }
        for i in range(40)
    ]
    # Mølle 2: stabil — samme tidsspand men konstant temperatur
    stable = [
        {
            "device_id": "IOT-DK-ALB-002",
            "park_id": "PARK-ALB",
            "timestamp": now - timedelta(days=5 - i / 8),
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
