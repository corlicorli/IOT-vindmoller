"""Anomaly detection — threshold-regel og persistering af 'Anomaly Detected' events.

Domain event flow:
    Sensor Value Received  -> metrics-collection
    Anomaly Detected       -> alerts-collection (denne fil)
"""
from __future__ import annotations

import logging
from typing import Iterable

import db

logger = logging.getLogger(__name__)

# Tærskler — én kilde til sandhed for hele applikationen
ALERT_TEMP_THRESHOLD_C = 70.0
WARN_SEVERITY_C = 75.0
RULE_NAME = "gearbox_temp_c > 70"


def severity_for(temp_c: float) -> str:
    return "CRITICAL" if temp_c > WARN_SEVERITY_C else "WARNING"


def build_alert(metric: dict) -> dict | None:
    """Beslutningslogik: skab alarm-event hvis tærskel overskredet."""
    temp = metric.get("gearbox_temp_c")
    if temp is None or temp <= ALERT_TEMP_THRESHOLD_C:
        return None
    return {
        "device_id": metric["device_id"],
        "park_id": metric["park_id"],
        "gearbox_temp_c": temp,
        "timestamp": metric["timestamp"],
        "severity": severity_for(temp),
        "event_type": "ANOMALY_DETECTED",
        "rule": RULE_NAME,
    }


async def evaluate_and_persist(metrics_docs: Iterable[dict]) -> int:
    """Kør threshold-regel på en batch af metrics og gem alarmer som events.

    Returnerer antal nye alarm-events der blev persisteret.
    """
    new_alerts = [a for m in metrics_docs if (a := build_alert(m)) is not None]
    if not new_alerts:
        return 0

    await db.alerts().insert_many(new_alerts)
    for a in new_alerts:
        logger.warning(
            "Anomaly detected: device=%s park=%s temp=%.1f°C severity=%s rule=%s",
            a["device_id"],
            a["park_id"],
            a["gearbox_temp_c"],
            a["severity"],
            a["rule"],
        )
    return len(new_alerts)
