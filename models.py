"""Pydantic-modeller (snake_case, matcher MongoDB-dokumenter)."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field


# --- Input-modeller (kunde-vendt API) ---------------------------------------


# Tvinger park_id og device_id til at være store bogstaver, tal og bindestreger
# (forhindrer URL-kollisioner og gør IDs konsistente i logs)
IdField = Annotated[
    str,
    Field(min_length=3, max_length=64, pattern=r"^[A-Z][A-Z0-9-]*$"),
]


class ParkCreate(BaseModel):
    """Kunde registrerer en ny vindmøllepark."""

    park_id: IdField = Field(description="Unik identifikator, fx 'PARK-AALBORG-NORD'")
    name: str = Field(min_length=1, max_length=100)
    region: str = Field(min_length=1, max_length=50)
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class DeviceCreate(BaseModel):
    """Kunde registrerer en ny IoT-enhed (mølle) på en park."""

    device_id: IdField = Field(description="Unik IoT-enhed-id, fx 'IOT-DK-AAL-001'")
    wind_turbine_id: str = Field(min_length=1, max_length=50)
    firmware_version: str = Field(default="v1.0.0", max_length=20)
    battery_level: int = Field(default=100, ge=0, le=100)
    signal_strength: int = Field(default=-70, ge=-120, le=0)
    last_error_code: str = Field(default="00", max_length=10)


# --- Output-modeller (matcher Mongo-dokumenter) -----------------------------


class Park(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    id: str = Field(alias="_id")
    name: str
    region: str
    lat: float
    lng: float
    turbine_count: int = 0


class DeviceStatus(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    id: str = Field(alias="_id")
    park_id: str
    wind_turbine_id: str
    firmware_version: str
    battery_level: int = Field(ge=0, le=100)
    signal_strength: int
    last_error_code: str
    last_ping: datetime


class MetricIn(BaseModel):
    device_id: str
    wind_speed_ms: float = Field(ge=0)
    power_output_kw: float = Field(ge=0)
    rotor_rpm: float = Field(ge=0)
    gearbox_temp_c: float
    # Valgfri timestamp — IoT-gateways der buffrer kan angive originalt
    # tidspunkt. Uden = server-tid ved modtagelse.
    timestamp: datetime | None = None


class Metric(MetricIn):
    park_id: str
    timestamp: datetime


class MetricBulk(BaseModel):
    """Batch-upload af målinger — for IoT-gateways der buffrer.

    Ved upload knyttes alle målinger til samme park_id som deres device,
    og samme server-timestamp så de er sorterbare. Maksimalt 1000 målinger
    pr. request for at undgå store BSON-operationer.
    """

    metrics: list[MetricIn] = Field(min_length=1, max_length=1000)


class Alert(BaseModel):
    """Live-view: seneste tilstand pr. mølle der overskrider threshold."""

    device_id: str
    park_id: str
    park_name: str
    wind_turbine_id: str
    gearbox_temp_c: float
    timestamp: datetime
    severity: str


class AlertEvent(BaseModel):
    """Persisteret 'Anomaly Detected' domain event."""

    device_id: str
    park_id: str
    gearbox_temp_c: float
    timestamp: datetime
    severity: str
    event_type: str
    rule: str


class Prediction(BaseModel):
    """Lag 3 PdM: trend-baseret forudsigelse for én mølle."""

    device_id: str
    park_id: str
    current_temp_c: float
    baseline_mean_c: float
    baseline_stddev_c: float
    trend_c_per_day: float
    days_until_breach: float | None
    eta_threshold_breach: datetime | None
    risk: str
    datapoints: int
    lookback_days: int


class NotificationRecord(BaseModel):
    """Operator-notification dispatch attempt — historik."""

    device_id: str
    park_id: str
    severity: str
    gearbox_temp_c: float
    alert_timestamp: datetime
    dispatched_at: datetime
    webhook_url: str | None
    status: str  # SENT, FAILED, SKIPPED
    http_status: int | None
    error: str | None
