"""Intelligent IoT Solutions A/S — Wind Farm API (MongoDB).

Run:
    uvicorn main:app --reload

Interaktive docs på http://127.0.0.1:8000/docs
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query

import alerts as alerts_service
import db
import predictions as predictions_service
import simulator
from alerts import ALERT_TEMP_THRESHOLD_C, WARN_SEVERITY_C
from models import (
    Alert,
    AlertEvent,
    DeviceStatus,
    Metric,
    MetricIn,
    Park,
    Prediction,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("windfarm")

SIMULATOR_INTERVAL_S = float(os.getenv("SIMULATOR_INTERVAL", "5"))
SIMULATOR_ENABLED = os.getenv("SIMULATOR_ENABLED", "1") != "0"


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not await db.ping():
        raise RuntimeError(
            f"Kan ikke nå MongoDB på {db.redacted_url()}. Start databasen først."
        )
    await db.init_indexes()
    logger.info("Forbundet til MongoDB %s (db=%s)", db.redacted_url(), db.MONGO_DB)

    task: asyncio.Task | None = None
    if SIMULATOR_ENABLED:
        logger.info("Starter live-simulator (interval=%.1fs)", SIMULATOR_INTERVAL_S)
        task = asyncio.create_task(simulator.run(SIMULATOR_INTERVAL_S))
    else:
        logger.info("Live-simulator deaktiveret (SIMULATOR_ENABLED=0)")
    try:
        yield
    finally:
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        db.close()
        logger.info("Lukket MongoDB-forbindelse")


app = FastAPI(
    title="Intelligent IoT Solutions — Wind Farm API",
    description="Managed Services kontrolcenter for 3 vindmølleparker (Aalborg, Esbjerg, Thy).",
    version="0.2.0",
    lifespan=lifespan,
)


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

@app.get("/parks", response_model=list[Park], tags=["parks"])
async def list_parks():
    return [p async for p in db.parks().find().sort("_id", 1)]


@app.get("/parks/{park_id}", response_model=Park, tags=["parks"])
async def get_park(park_id: str):
    doc = await db.parks().find_one({"_id": park_id})
    if not doc:
        raise HTTPException(404, f"Park {park_id} not found")
    return doc


@app.get("/parks/{park_id}/devices", response_model=list[DeviceStatus], tags=["parks"])
async def devices_in_park(park_id: str):
    return [d async for d in db.devices().find({"park_id": park_id}).sort("_id", 1)]


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

    doc = {
        **payload.model_dump(),
        "park_id": device["park_id"],
        "timestamp": datetime.now(timezone.utc).replace(microsecond=0),
    }
    await db.metrics().insert_one(doc)
    await db.devices().update_one(
        {"_id": payload.device_id}, {"$set": {"last_ping": doc["timestamp"]}}
    )
    await alerts_service.evaluate_and_persist([doc])
    return doc


# --- Managed Services monitoring --------------------------------------------

@app.get("/monitoring/simulator", tags=["monitoring"])
async def simulator_status():
    s = simulator.status
    return {
        "running": s.running,
        "interval_seconds": s.interval_seconds,
        "ticks": s.ticks,
        "rows_inserted": s.rows_inserted,
        "device_count": s.device_count,
        "last_tick_at": s.last_tick_at,
        "last_error": s.last_error,
    }


@app.get("/monitoring/alerts", response_model=list[Alert], tags=["monitoring"])
async def active_alerts(park_id: str | None = Query(None)):
    """Seneste måling pr. mølle; alarm hvis gearkasse-temp > 70°C."""
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
                "severity": "CRITICAL" if row["gearbox_temp_c"] > WARN_SEVERITY_C else "WARNING",
            }
        )
    return out


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
