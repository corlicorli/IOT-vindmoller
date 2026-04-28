"""Predictive maintenance — trend-baseret forudsigelse af threshold-brud.

Lag 3 i PdM-arkitekturen:
    Lag 1: Dataindsamling     (metrics-collection — Sensor Value Received)
    Lag 2: Anomaly detection  (alerts.py — threshold-regel her og nu)
    Lag 3: Trend-analyse      (denne fil — lineær regression på temperaturhistorik)

For hver mølle: fit en linje gennem de sidste N dages temperaturmålinger og
ekstrapolér til hvornår threshold (70°C) bliver overskredet hvis trenden fortsætter.
Risk-niveau kombinerer trend-styrke med afvigelse fra møllens egen baseline.

Pure-Python implementation — ingen tunge ML-dependencies.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import db
from alerts import ALERT_TEMP_THRESHOLD_C

logger = logging.getLogger(__name__)

PREDICTION_LOOKBACK_DAYS = 7
MIN_DATAPOINTS = 30  # mindst så mange målinger før vi tør forudsige


# --- Pure-Python statistik (uden numpy) -------------------------------------


def linear_regression(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Returnér (slope, intercept) for least-squares-linje gennem (xs, ys).

    Returnerer (0, 0) for tomme input og (0, ys[0]) for et enkelt datapunkt —
    så kaldere ikke skal håndtere None.
    """
    n = len(xs)
    if n == 0:
        return 0.0, 0.0
    if n < 2:
        return 0.0, ys[0]
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    den = sum((x - mean_x) ** 2 for x in xs)
    if den == 0:
        return 0.0, mean_y
    slope = num / den
    intercept = mean_y - slope * mean_x
    return slope, intercept


def stddev(values: list[float]) -> float:
    """Sample standardafvigelse (n-1 i nævneren)."""
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    return (sum((v - mean) ** 2 for v in values) / (n - 1)) ** 0.5


def classify_risk(
    current_temp: float,
    baseline_mean: float,
    baseline_std: float,
    slope_c_per_day: float,
) -> str:
    """Risk-niveau baseret på nuværende afvigelse + trend.

    Regler (i prioriteret rækkefølge):
      - Allerede over threshold              → HIGH
      - Stærk opadgående trend + outlier     → HIGH
      - Moderat opadgående trend ELLER outlier → MEDIUM
      - Ellers                               → LOW
    """
    if current_temp > ALERT_TEMP_THRESHOLD_C:
        return "HIGH"
    z_score = (
        (current_temp - baseline_mean) / baseline_std if baseline_std > 0 else 0.0
    )
    if slope_c_per_day > 1.0 and z_score > 1.5:
        return "HIGH"
    if slope_c_per_day > 0.5 or z_score > 1.5:
        return "MEDIUM"
    return "LOW"


# --- Forudsigelser -----------------------------------------------------------


async def predict_for_device(device_id: str) -> dict | None:
    """Trend-baseret forudsigelse for én mølle.

    Returnerer None hvis der er for få datapunkter eller mølle ukendt.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=PREDICTION_LOOKBACK_DAYS)
    cursor = (
        db.metrics()
        .find(
            {"device_id": device_id, "timestamp": {"$gte": cutoff}},
            projection={"timestamp": 1, "gearbox_temp_c": 1, "park_id": 1, "_id": 0},
        )
        .sort("timestamp", 1)
    )
    docs = [d async for d in cursor]
    if len(docs) < MIN_DATAPOINTS:
        return None

    park_id = docs[0]["park_id"]
    t0 = docs[0]["timestamp"]
    xs = [(d["timestamp"] - t0).total_seconds() / 86400.0 for d in docs]
    ys = [d["gearbox_temp_c"] for d in docs]

    slope_c_per_day, _intercept = linear_regression(xs, ys)
    baseline_mean = sum(ys) / len(ys)
    baseline_std = stddev(ys)
    current_temp = ys[-1]

    eta_breach: datetime | None = None
    days_until_breach: float | None = None
    if slope_c_per_day > 0.01 and current_temp < ALERT_TEMP_THRESHOLD_C:
        days_until_breach = (ALERT_TEMP_THRESHOLD_C - current_temp) / slope_c_per_day
        eta_breach = datetime.now(timezone.utc) + timedelta(days=days_until_breach)

    risk = classify_risk(current_temp, baseline_mean, baseline_std, slope_c_per_day)

    return {
        "device_id": device_id,
        "park_id": park_id,
        "current_temp_c": round(current_temp, 2),
        "baseline_mean_c": round(baseline_mean, 2),
        "baseline_stddev_c": round(baseline_std, 2),
        "trend_c_per_day": round(slope_c_per_day, 3),
        "days_until_breach": (
            round(days_until_breach, 1) if days_until_breach is not None else None
        ),
        "eta_threshold_breach": eta_breach,
        "risk": risk,
        "datapoints": len(docs),
        "lookback_days": PREDICTION_LOOKBACK_DAYS,
    }


async def predict_all() -> list[dict]:
    """Forudsigelse for alle møller, sorteret med højest risiko først.

    Inden for samme risiko-bucket sorteres efter stejlest opadgående trend først.
    Møller med utilstrækkelige data udelades stille.
    """
    devices = [d async for d in db.devices().find({}, {"_id": 1}).sort("_id", 1)]
    results: list[dict] = []
    for d in devices:
        pred = await predict_for_device(d["_id"])
        if pred is not None:
            results.append(pred)

    risk_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    results.sort(key=lambda p: (risk_order[p["risk"]], -p["trend_c_per_day"]))
    return results
