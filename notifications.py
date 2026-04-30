"""Operator notification dispatch — webhook ved CRITICAL alarmer.

Dette lukker hullet i opgavens "alarm/notifikation"-led: når en mølle udløser
en kritisk alarm, sendes en notifikation til operatørens vagt-system så de
kan rykke ud og fixe møllen.

Arkitektur:
    Anomaly Detected event (alerts.py)
        ↓
    dispatch_critical_alerts() — kun CRITICAL by default
        ↓
    POST til OPERATOR_WEBHOOK_URL (kunde-konfigureret)
        ↓
    Resultat persisteret i notifications-collection (SENT/FAILED/SKIPPED)

Konfiguration (env-vars):
    OPERATOR_WEBHOOK_URL — destination for webhook (valgfri; uden = SKIPPED)
    NOTIFY_SEVERITIES    — "CRITICAL" (default) eller "CRITICAL,WARNING"
    WEBHOOK_TIMEOUT_S    — HTTP-timeout i sekunder (default: 3.0)
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Iterable

import httpx

import db

logger = logging.getLogger(__name__)


def _notify_severities() -> set[str]:
    raw = os.getenv("NOTIFY_SEVERITIES", "CRITICAL")
    return {s.strip() for s in raw.split(",") if s.strip()}


def _webhook_url() -> str | None:
    """Læses ved hvert kald så tests kan override via monkeypatch.setenv."""
    return os.getenv("OPERATOR_WEBHOOK_URL")


def _timeout_seconds() -> float:
    return float(os.getenv("WEBHOOK_TIMEOUT_S", "3.0"))


def _build_payload(alert: dict) -> dict:
    """JSON-payload sendt til operatørens webhook."""
    return {
        "event_type": "ANOMALY_NOTIFICATION",
        "device_id": alert["device_id"],
        "park_id": alert["park_id"],
        "severity": alert["severity"],
        "gearbox_temp_c": alert["gearbox_temp_c"],
        "timestamp": alert["timestamp"].isoformat()
            if hasattr(alert["timestamp"], "isoformat")
            else alert["timestamp"],
        "rule": alert.get("rule", "gearbox_temp_c > 70"),
        "action_required": "INSPECT_TURBINE",
    }


async def dispatch_critical_alerts(alerts: Iterable[dict]) -> int:
    """Dispatcher webhook-notifikation for hver alert der matcher NOTIFY_SEVERITIES.

    Persisterer resultatet (SENT/FAILED/SKIPPED) i notifications-collection.
    Returnerer antal forsøg foretaget.

    Fejler aldrig — webhook-fejl må ikke ødelægge metric-ingestion.
    """
    severities = _notify_severities()
    matching = [a for a in alerts if a.get("severity") in severities]
    if not matching:
        return 0

    url = _webhook_url()
    timeout = _timeout_seconds()

    notifications: list[dict] = []
    for alert in matching:
        record: dict = {
            "device_id": alert["device_id"],
            "park_id": alert["park_id"],
            "severity": alert["severity"],
            "gearbox_temp_c": alert["gearbox_temp_c"],
            "alert_timestamp": alert["timestamp"],
            "dispatched_at": datetime.now(timezone.utc).replace(microsecond=0),
            "webhook_url": url,
            "status": "SKIPPED",
            "http_status": None,
            "error": None,
        }

        if not url:
            logger.info(
                "Notification skipped (OPERATOR_WEBHOOK_URL ikke sat): "
                "device=%s severity=%s",
                alert["device_id"], alert["severity"],
            )
        else:
            payload = _build_payload(alert)
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(url, json=payload)
                record["http_status"] = resp.status_code
                if 200 <= resp.status_code < 300:
                    record["status"] = "SENT"
                    logger.warning(
                        "Operator notification SENT: device=%s severity=%s http=%d",
                        alert["device_id"], alert["severity"], resp.status_code,
                    )
                else:
                    record["status"] = "FAILED"
                    record["error"] = f"HTTP {resp.status_code}"
                    logger.error(
                        "Operator notification FAILED: device=%s http=%d body=%s",
                        alert["device_id"], resp.status_code, resp.text[:200],
                    )
            except Exception as e:  # noqa: BLE001
                record["status"] = "FAILED"
                record["error"] = f"{type(e).__name__}: {e}"
                logger.exception(
                    "Operator notification dispatch exception: device=%s",
                    alert["device_id"],
                )

        notifications.append(record)

    if notifications:
        await db.notifications().insert_many(notifications)
    return len(notifications)
