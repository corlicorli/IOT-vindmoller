"""Unit-tests for threshold-regel (rene funktioner — ingen DB)."""
from __future__ import annotations

from datetime import datetime, timezone

from alerts import (
    ALERT_TEMP_THRESHOLD_C,
    WARN_SEVERITY_C,
    build_alert,
    severity_for,
)


def _metric(temp: float) -> dict:
    return {
        "device_id": "IOT-DK-ALB-001",
        "park_id": "PARK-ALB",
        "gearbox_temp_c": temp,
        "timestamp": datetime.now(timezone.utc),
    }


class TestSeverity:
    def test_warning_when_above_threshold_below_critical(self):
        assert severity_for(72.0) == "WARNING"

    def test_critical_when_above_critical(self):
        assert severity_for(WARN_SEVERITY_C + 0.1) == "CRITICAL"


class TestBuildAlert:
    def test_below_threshold_returns_none(self):
        assert build_alert(_metric(ALERT_TEMP_THRESHOLD_C - 0.1)) is None

    def test_at_threshold_returns_none(self):
        # Rule er strict greater-than, ikke >=
        assert build_alert(_metric(ALERT_TEMP_THRESHOLD_C)) is None

    def test_above_threshold_returns_alert_doc(self):
        a = build_alert(_metric(72.5))
        assert a is not None
        assert a["event_type"] == "ANOMALY_DETECTED"
        assert a["severity"] == "WARNING"
        assert a["device_id"] == "IOT-DK-ALB-001"

    def test_critical_severity_when_high(self):
        a = build_alert(_metric(80.0))
        assert a is not None
        assert a["severity"] == "CRITICAL"
