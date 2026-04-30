"""Intelligent IoT Solutions A/S — Wind Farm API (MongoDB).

Run:
    uvicorn main:app --reload

Interaktive docs på http://127.0.0.1:8000/docs
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Query
from prometheus_fastapi_instrumentator import Instrumentator

import alerts as alerts_service
import db
import predictions as predictions_service
from alerts import ALERT_TEMP_THRESHOLD_C, WARN_SEVERITY_C
from models import (
    Alert,
    AlertEvent,
    DeviceCreate,
    DeviceStatus,
    Metric,
    MetricBulk,
    MetricIn,
    NotificationRecord,
    Park,
    ParkCreate,
    Prediction,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("windfarm")


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not await db.ping():
        raise RuntimeError(
            f"Kan ikke nå MongoDB på {db.redacted_url()}. Start databasen først."
        )
    await db.init_indexes()
    logger.info("Forbundet til MongoDB %s (db=%s)", db.redacted_url(), db.MONGO_DB)
    try:
        yield
    finally:
        db.close()
        logger.info("Lukket MongoDB-forbindelse")


app = FastAPI(
    title="Intelligent IoT Solutions — Wind Farm API",
    description="Managed Services kontrolcenter for vindmølleparker.",
    version="0.3.0",
    lifespan=lifespan,
)


# --- Observability: Prometheus-instrumentering ------------------------------
# Eksponeres på /observability/metrics — IKKE /metrics (det er IoT-data!).
# Scrapeas af Prometheus container, visualiseret i Grafana API Observability dashboard.
Instrumentator(
    should_group_status_codes=False,
    excluded_handlers=["/observability/metrics", "/health", "/docs", "/openapi.json"],
).instrument(app).expose(app, endpoint="/observability/metrics", tags=["observability"])


# --- Meta --------------------------------------------------------------------

@app.get("/", tags=["meta"])
async def root():
    """Service-metadata. Eksponerer IKKE forbindelses-URL (kan indeholde credentials)."""
    return {
        "service": "Wind Farm API",
        "docs": "/docs",
        "alert_threshold_c": ALERT_TEMP_THRESHOLD_C,
        "db": db.MONGO_DB,
    }


@app.get("/health", tags=["meta"])
async def health():
    return {"mongo": await db.ping()}


# --- Parker ------------------------------------------------------------------

# Aggregation: tilfølg turbine_count som "antal devices i denne park" — så vi
# ikke skal vedligeholde tælleren manuelt og risikere drift.
_PARK_WITH_COUNT_PIPELINE: list[dict] = [
    {
        "$lookup": {
            "from": "devices",
            "localField": "_id",
            "foreignField": "park_id",
            "as": "_devs",
        }
    },
    {"$addFields": {"turbine_count": {"$size": "$_devs"}}},
    {"$project": {"_devs": 0}},
]


@app.post("/parks", response_model=Park, status_code=201, tags=["parks"])
async def create_park(payload: ParkCreate):
    """Kunde registrerer en ny vindmøllepark."""
    if await db.parks().find_one({"_id": payload.park_id}):
        raise HTTPException(409, f"Park {payload.park_id} eksisterer allerede")
    doc = {
        "_id": payload.park_id,
        "name": payload.name,
        "region": payload.region,
        "lat": payload.lat,
        "lng": payload.lng,
        "turbine_count": 0,
    }
    await db.parks().insert_one(doc)
    logger.info("Park oprettet: %s (%s, %s)", payload.park_id, payload.name, payload.region)
    return doc


@app.get("/parks", response_model=list[Park], tags=["parks"])
async def list_parks():
    pipeline = _PARK_WITH_COUNT_PIPELINE + [{"$sort": {"_id": 1}}]
    return [p async for p in db.parks().aggregate(pipeline)]


@app.get("/parks/{park_id}", response_model=Park, tags=["parks"])
async def get_park(park_id: str):
    pipeline = [{"$match": {"_id": park_id}}] + _PARK_WITH_COUNT_PIPELINE
    rows = [p async for p in db.parks().aggregate(pipeline)]
    if not rows:
        raise HTTPException(404, f"Park {park_id} not found")
    return rows[0]


@app.delete("/parks/{park_id}", status_code=204, tags=["parks"])
async def delete_park(park_id: str):
    """Slet en park + cascade alle dens devices, metrics og alerts."""
    park = await db.parks().find_one({"_id": park_id})
    if not park:
        raise HTTPException(404, f"Park {park_id} not found")

    devices_deleted = (await db.devices().delete_many({"park_id": park_id})).deleted_count
    metrics_deleted = (await db.metrics().delete_many({"park_id": park_id})).deleted_count
    alerts_deleted = (await db.alerts().delete_many({"park_id": park_id})).deleted_count
    await db.parks().delete_one({"_id": park_id})

    logger.info(
        "Park slettet: %s (cascade: %d devices, %d metrics, %d alerts)",
        park_id, devices_deleted, metrics_deleted, alerts_deleted,
    )


@app.get("/parks/{park_id}/devices", response_model=list[DeviceStatus], tags=["parks"])
async def devices_in_park(park_id: str):
    if not await db.parks().find_one({"_id": park_id}):
        raise HTTPException(404, f"Park {park_id} not found")
    return [d async for d in db.devices().find({"park_id": park_id}).sort("_id", 1)]


@app.post(
    "/parks/{park_id}/devices",
    response_model=DeviceStatus,
    status_code=201,
    tags=["parks"],
)
async def create_device(park_id: str, payload: DeviceCreate):
    """Kunde registrerer en ny IoT-enhed (mølle) på en eksisterende park."""
    if not await db.parks().find_one({"_id": park_id}):
        raise HTTPException(404, f"Park {park_id} not found — registrér den først")
    if await db.devices().find_one({"_id": payload.device_id}):
        raise HTTPException(409, f"Device {payload.device_id} eksisterer allerede")

    doc = {
        "_id": payload.device_id,
        "park_id": park_id,
        "wind_turbine_id": payload.wind_turbine_id,
        "firmware_version": payload.firmware_version,
        "battery_level": payload.battery_level,
        "signal_strength": payload.signal_strength,
        "last_error_code": payload.last_error_code,
        "last_ping": datetime.now(timezone.utc).replace(microsecond=0),
    }
    await db.devices().insert_one(doc)
    logger.info("Device oprettet: %s på park %s", payload.device_id, park_id)
    return doc


# --- Devices -----------------------------------------------------------------

@app.get("/devices", response_model=list[DeviceStatus], tags=["devices"])
async def list_devices(park_id: str | None = Query(None)):
    q = {"park_id": park_id} if park_id else {}
    return [d async for d in db.devices().find(q).sort("_id", 1)]


@app.get("/devices/{device_id}", response_model=DeviceStatus, tags=["devices"])
async def get_device(device_id: str):
    doc = await db.devices().find_one({"_id": device_id})
    if not doc:
        raise HTTPException(404, f"Device {device_id} not found")
    return doc


@app.delete("/devices/{device_id}", status_code=204, tags=["devices"])
async def delete_device(device_id: str):
    """Slet en device + cascade dens metrics og alerts."""
    if not await db.devices().find_one({"_id": device_id}):
        raise HTTPException(404, f"Device {device_id} not found")
    metrics_deleted = (await db.metrics().delete_many({"device_id": device_id})).deleted_count
    alerts_deleted = (await db.alerts().delete_many({"device_id": device_id})).deleted_count
    await db.devices().delete_one({"_id": device_id})
    logger.info(
        "Device slettet: %s (cascade: %d metrics, %d alerts)",
        device_id, metrics_deleted, alerts_deleted,
    )


# --- Metrics -----------------------------------------------------------------

@app.get("/metrics", response_model=list[Metric], tags=["metrics"])
async def list_metrics(
    device_id: str | None = Query(None),
    park_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
):
    q: dict = {}
    if device_id:
        q["device_id"] = device_id
    if park_id:
        q["park_id"] = park_id
    cursor = db.metrics().find(q).sort("timestamp", -1).limit(limit)
    return [m async for m in cursor]


@app.post("/metrics", response_model=Metric, status_code=201, tags=["metrics"])
async def upload_metric(payload: MetricIn):
    """Event flow: Sensor Value Received → threshold-check → (evt.) Anomaly Detected."""
    device = await db.devices().find_one({"_id": payload.device_id})
    if not device:
        logger.warning("Afvist måling fra ukendt device_id=%s", payload.device_id)
        raise HTTPException(400, f"Unknown device_id: {payload.device_id}")

    server_ts = datetime.now(timezone.utc).replace(microsecond=0)
    payload_dict = payload.model_dump(exclude={"timestamp"})
    doc = {
        **payload_dict,
        "park_id": device["park_id"],
        "timestamp": payload.timestamp or server_ts,
    }
    await db.metrics().insert_one(doc)
    await db.devices().update_one(
        {"_id": payload.device_id}, {"$set": {"last_ping": server_ts}}
    )
    await alerts_service.evaluate_and_persist([doc])
    return doc


@app.post("/metrics/bulk", status_code=201, tags=["metrics"])
async def upload_metrics_bulk(payload: MetricBulk) -> dict:
    """Batch-upload — for IoT-gateways der buffrer målinger.

    Validerer alle device_ids op-front, afviser hele batchen ved ukendte ids.
    Threshold-check kører på hele batchen i ét pass.
    """
    device_ids = list({m.device_id for m in payload.metrics})
    devices = {
        d["_id"]: d
        async for d in db.devices().find(
            {"_id": {"$in": device_ids}}, projection={"_id": 1, "park_id": 1}
        )
    }
    unknown = [d for d in device_ids if d not in devices]
    if unknown:
        raise HTTPException(400, f"Unknown device_ids: {unknown}")

    server_ts = datetime.now(timezone.utc).replace(microsecond=0)
    docs = [
        {
            **m.model_dump(exclude={"timestamp"}),
            "park_id": devices[m.device_id]["park_id"],
            # Brug klient-leveret timestamp hvis sat (IoT-gateway der buffrer);
            # ellers server-tid. last_ping bruger altid server-tid.
            "timestamp": m.timestamp or server_ts,
        }
        for m in payload.metrics
    ]
    await db.metrics().insert_many(docs, ordered=False)
    await db.devices().update_many(
        {"_id": {"$in": device_ids}}, {"$set": {"last_ping": server_ts}}
    )
    alerts_created = await alerts_service.evaluate_and_persist(docs)
    return {
        "metrics_inserted": len(docs),
        "alerts_created": alerts_created,
        "timestamp": server_ts,
    }


# --- Managed Services monitoring --------------------------------------------


async def _active_alerts_data(park_id: str | None = None) -> list[dict]:
    """Helper: seneste tilstand pr. mølle der overskrider threshold.

    Adskilt fra endpoint så den kan genbruges fra stats-endpoint uden at
    Query()-default-objektet kommer ind som "park_id".
    """
    match: dict = {}
    if park_id:
        match["park_id"] = park_id

    pipeline = [
        {"$match": match} if match else {"$match": {}},
        {"$sort": {"device_id": 1, "timestamp": -1}},
        {
            "$group": {
                "_id": "$device_id",
                "park_id": {"$first": "$park_id"},
                "timestamp": {"$first": "$timestamp"},
                "gearbox_temp_c": {"$first": "$gearbox_temp_c"},
            }
        },
        {"$match": {"gearbox_temp_c": {"$gt": ALERT_TEMP_THRESHOLD_C}}},
        {
            "$lookup": {
                "from": "devices",
                "localField": "_id",
                "foreignField": "_id",
                "as": "device",
            }
        },
        {
            "$lookup": {
                "from": "parks",
                "localField": "park_id",
                "foreignField": "_id",
                "as": "park",
            }
        },
        {"$sort": {"gearbox_temp_c": -1}},
    ]

    out: list[dict] = []
    async for row in db.metrics().aggregate(pipeline):
        device = (row.get("device") or [{}])[0]
        park = (row.get("park") or [{}])[0]
        out.append(
            {
                "device_id": row["_id"],
                "park_id": row["park_id"],
                "park_name": park.get("name", ""),
                "wind_turbine_id": device.get("wind_turbine_id", ""),
                "gearbox_temp_c": row["gearbox_temp_c"],
                "timestamp": row["timestamp"],
                "severity": (
                    "CRITICAL" if row["gearbox_temp_c"] > WARN_SEVERITY_C else "WARNING"
                ),
            }
        )
    return out


@app.get("/monitoring/alerts", response_model=list[Alert], tags=["monitoring"])
async def active_alerts(park_id: str | None = Query(None)):
    """Seneste måling pr. mølle; alarm hvis gearkasse-temp > 70°C."""
    return await _active_alerts_data(park_id)


@app.get(
    "/monitoring/alerts/history",
    response_model=list[AlertEvent],
    tags=["monitoring"],
)
async def alerts_history(
    park_id: str | None = Query(None),
    device_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
):
    """Persisteret 'Anomaly Detected' event log (kronologisk, nyeste først)."""
    q: dict = {}
    if park_id:
        q["park_id"] = park_id
    if device_id:
        q["device_id"] = device_id
    cursor = (
        db.alerts()
        .find(q, projection={"_id": 0})
        .sort("timestamp", -1)
        .limit(limit)
    )
    return [a async for a in cursor]


@app.get(
    "/monitoring/predictions",
    response_model=list[Prediction],
    tags=["monitoring"],
)
async def all_predictions():
    """PdM lag 3: trend-baseret forudsigelse pr. mølle, sorteret efter risiko (HIGH først)."""
    return await predictions_service.predict_all()


@app.get(
    "/monitoring/predictions/{device_id}",
    response_model=Prediction,
    tags=["monitoring"],
)
async def device_prediction(device_id: str):
    """PdM lag 3 for én mølle: lineær regression på sidste 7 dages temperatur."""
    pred = await predictions_service.predict_for_device(device_id)
    if pred is None:
        raise HTTPException(
            404,
            (
                f"Ikke nok data for {device_id} — kræver mindst "
                f"{predictions_service.MIN_DATAPOINTS} målinger inden for "
                f"{predictions_service.PREDICTION_LOOKBACK_DAYS} dage."
            ),
        )
    return pred


@app.get("/monitoring/stats", tags=["monitoring"])
async def stats() -> dict:
    """Aggregeret statusoverblik — én JSON med alle counters til dashboards (Grafana o.l.).

    Samler tal fra alle 3 PdM-lag plus event-log totaler i ét response, så
    visualiserings-værktøjer ikke skal rate ad flere endpoints for et statusbillede.
    """
    park_count = await db.parks().count_documents({})
    device_count = await db.devices().count_documents({})

    active = await _active_alerts_data()
    critical_count = sum(1 for a in active if a["severity"] == "CRITICAL")
    warning_count = sum(1 for a in active if a["severity"] == "WARNING")

    preds = await predictions_service.predict_all()
    high_risk = sum(1 for p in preds if p["risk"] == "HIGH")
    medium_risk = sum(1 for p in preds if p["risk"] == "MEDIUM")
    low_risk = sum(1 for p in preds if p["risk"] == "LOW")

    cutoff_24h = datetime.now(timezone.utc) - timedelta(days=1)

    # Lag 1: alle sensor-målinger (Sensor Value Received events)
    total_readings = await db.metrics().count_documents({})
    readings_24h = await db.metrics().count_documents(
        {"timestamp": {"$gte": cutoff_24h}}
    )

    # Lag 2: kun anomalier (Anomaly Detected events) — delmængde af readings
    total_anomalies = await db.alerts().count_documents({})
    anomalies_24h = await db.alerts().count_documents(
        {"timestamp": {"$gte": cutoff_24h}}
    )

    return {
        "parks": park_count,
        "devices": device_count,
        "active_alerts": {
            "critical": critical_count,
            "warning": warning_count,
            "total": critical_count + warning_count,
        },
        "predictions": {
            "high_risk": high_risk,
            "medium_risk": medium_risk,
            "low_risk": low_risk,
            "total_analyzed": len(preds),
            # Pivoteret form til Grafanas pie chart (label/value rækker)
            "by_risk": [
                {"risk": "HIGH", "count": high_risk},
                {"risk": "MEDIUM", "count": medium_risk},
                {"risk": "LOW", "count": low_risk},
            ],
        },
        "sensor_readings": {
            "total": total_readings,
            "last_24h": readings_24h,
        },
        "anomaly_events": {
            "total": total_anomalies,
            "last_24h": anomalies_24h,
        },
    }


@app.get(
    "/monitoring/notifications",
    response_model=list[NotificationRecord],
    tags=["monitoring"],
)
async def notifications_history(
    severity: str | None = Query(None, description="Filter: CRITICAL, WARNING"),
    status: str | None = Query(None, description="Filter: SENT, FAILED, SKIPPED"),
    limit: int = Query(50, ge=1, le=500),
):
    """Historik over operator-notifikations dispatches (webhook resultater)."""
    q: dict = {}
    if severity:
        q["severity"] = severity
    if status:
        q["status"] = status
    cursor = (
        db.notifications()
        .find(q, projection={"_id": 0})
        .sort("dispatched_at", -1)
        .limit(limit)
    )
    return [n async for n in cursor]


@app.get("/monitoring/park-summary", tags=["monitoring"])
async def park_summary():
    """Totaler pr. park — seneste tick."""
    pipeline = [
        {"$sort": {"device_id": 1, "timestamp": -1}},
        {
            "$group": {
                "_id": "$device_id",
                "park_id": {"$first": "$park_id"},
                "power": {"$first": "$power_output_kw"},
                "wind": {"$first": "$wind_speed_ms"},
                "temp": {"$first": "$gearbox_temp_c"},
                "timestamp": {"$first": "$timestamp"},
            }
        },
        {
            "$group": {
                "_id": "$park_id",
                "total_power_kw": {"$sum": "$power"},
                "avg_wind_ms": {"$avg": "$wind"},
                "avg_temp_c": {"$avg": "$temp"},
                "max_temp_c": {"$max": "$temp"},
                "device_count": {"$sum": 1},
                "latest": {"$max": "$timestamp"},
            }
        },
        {"$sort": {"_id": 1}},
    ]

    rows = [row async for row in db.metrics().aggregate(pipeline)]
    parks_by_id = {p["_id"]: p async for p in db.parks().find()}
    for r in rows:
        park = parks_by_id.get(r["_id"], {})
        r["park_name"] = park.get("name")
        r["region"] = park.get("region")
        for k in ("total_power_kw", "avg_wind_ms", "avg_temp_c", "max_temp_c"):
            if r.get(k) is not None:
                r[k] = round(r[k], 2)
        r["park_id"] = r.pop("_id")
    return rows
