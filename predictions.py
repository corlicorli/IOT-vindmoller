"""Predictive maintenance — trend-baseret forudsigelse af threshold-brud.

Lag 3 i PdM-arkitekturen:
    Lag 1: Dataindsamling     (metrics-collection — Sensor Value Received)
    Lag 2: Anomaly detection  (alerts.py — threshold-regel her og nu)
    Lag 3: Trend-analyse      (denne fil — lineær regression på temperaturhistorik)

For hver mølle: aggregér de sidste N dages målinger til daglige gennemsnit
og fit en linje gennem disse. Daglig aggregering filtrerer wind/load-drevet
støj fra og isolerer den faktiske gearkasse-degradering.

Ekstrapolér derefter til hvornår threshold (70°C) bliver overskredet hvis
trenden fortsætter. Risk-niveau kombinerer trend-styrke med afvigelse fra
møllens egen baseline.

Pure-Python implementation — ingen tunge ML-dependencies.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import db
from alerts import ALERT_TEMP_THRESHOLD_C

logger = logging.getLogger(__name__)

PREDICTION_LOOKBACK_DAYS = 7
MIN_DATAPOINTS = 30  # mindst så mange målinger før vi tør forudsige
MIN_DAYS_FOR_TREND = 3  # mindst så mange dage med data før slope er meningsfuld

# Filtrér til høj-load målinger: ved >40% effekt domineres gearkasse-temp
# af baseline + drift, ikke vind-variationer. Det gør trend-signalet
# adskilleligt fra støj. Lav-effekt målinger kasseres.
HIGH_LOAD_KW_THRESHOLD = 1000.0


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

    Tærsklerne er kalibreret til daglig-aggregeret slope. Med daglig aggregering
    er typisk støj-niveau ~0.2°C/dag, så signal skal være > 0.25 for at tælle.

    Regler (i prioriteret rækkefølge):
      - Allerede over threshold                         → HIGH
      - Stærk opadgående trend (>0.6°C/dag) + outlier   → HIGH
      - Mild opadgående trend (>0.25°C/dag) ELLER outlier → MEDIUM
      - Ellers                                          → LOW
    """
    if current_temp > ALERT_TEMP_THRESHOLD_C:
        return "HIGH"
    z_score = (
        (current_temp - baseline_mean) / baseline_std if baseline_std > 0 else 0.0
    )
    if slope_c_per_day > 0.6 and z_score > 1.5:
        return "HIGH"
    if slope_c_per_day > 0.25 or z_score > 1.5:
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
            projection={
                "timestamp": 1,
                "gearbox_temp_c": 1,
                "park_id": 1,
                "power_output_kw": 1,
                "_id": 0,
            },
        )
        .sort("timestamp", 1)
    )
    docs = [d async for d in cursor]
    if len(docs) < MIN_DATAPOINTS:
        return None

    park_id = docs[0]["park_id"]
    raw_temps = [d["gearbox_temp_c"] for d in docs]

    # Filtrer til høj-load målinger til slope-beregning (men ikke baseline,
    # som skal afspejle alle driftstilstande). Hvis enheden aldrig kører høj
    # last (fx low_power scenario), falder vi tilbage til alle målinger.
    high_load_docs = [
        d for d in docs if d.get("power_output_kw", 0) >= HIGH_LOAD_KW_THRESHOLD
    ]
    trend_docs = high_load_docs if len(high_load_docs) >= MIN_DATAPOINTS else docs

    # Aggregér til daglige gennemsnit — filtrerer kortvarige wind-fluktuationer
    # fra så vi kan se den ægte gearkasse-degradering henover dage.
    daily_buckets: dict[date, list[float]] = defaultdict(list)
    for d in trend_docs:
        day = d["timestamp"].date()
        daily_buckets[day].append(d["gearbox_temp_c"])

    daily_pairs = sorted(daily_buckets.items())
    if len(daily_pairs) < MIN_DAYS_FOR_TREND:
        slope_c_per_day = 0.0  # for få dage til en meningsfuld trend
    else:
        first_day = daily_pairs[0][0]
        xs_days = [(day - first_day).days for day, _ in daily_pairs]
        ys_daily = [sum(temps) / len(temps) for _, temps in daily_pairs]
        slope_c_per_day, _intercept = linear_regression(xs_days, ys_daily)

    baseline_mean = sum(raw_temps) / len(raw_temps)
    baseline_std = stddev(raw_temps)
    current_temp = raw_temps[-1]

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
