"""Pydantic-modeller (snake_case, matcher MongoDB-dokumenter)."""
from __future__ import annotations

from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field


class Park(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    id: str = Field(alias="_id")
    name: str
    region: str
    lat: float
    lng: float
    turbine_count: int


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


class Metric(MetricIn):
    park_id: str
    timestamp: datetime


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
