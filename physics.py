"""Turbine-fysik og scenarie-profiler — delt mellem seed og live simulator."""
from __future__ import annotations

import math
import random
from dataclasses import dataclass

RATED_POWER_KW = 2500.0
CUT_IN_MS = 3.0
RATED_MS = 12.0
CUT_OUT_MS = 25.0

OK_ERROR = "00"
FIRMWARES = ["v2.4.1", "v2.4.0", "v2.3.9"]


@dataclass
class Scenario:
    name: str
    temp_offset: float
    power_factor: float
    base_error: str
    battery_drain: float


SCENARIOS = [
    Scenario("healthy",        0.0,  1.00, OK_ERROR, 0.02),
    Scenario("overheat_drift", 18.0, 0.95, "E12",    0.04),
    Scenario("low_power",     -2.0,  0.55, "W04",    0.06),
    Scenario("aging",         12.0,  0.90, "W11",    0.08),
    Scenario("flaky_sensor",   2.0,  1.00, "E07",    0.03),
]


def scenario_for(idx: int) -> Scenario:
    return SCENARIOS[idx % len(SCENARIOS)]


def power_curve(wind_ms: float) -> float:
    if wind_ms < CUT_IN_MS or wind_ms >= CUT_OUT_MS:
        return 0.0
    if wind_ms >= RATED_MS:
        return RATED_POWER_KW
    ratio = (wind_ms - CUT_IN_MS) / (RATED_MS - CUT_IN_MS)
    return RATED_POWER_KW * ratio**3


def rpm_from_wind(wind_ms: float) -> float:
    if wind_ms < CUT_IN_MS:
        return 0.0
    return min(16.0, 4.0 + wind_ms * 0.8)


@dataclass
class TickState:
    wind: float
    temp_drift: float


def next_tick(state: TickState, scenario: Scenario, rng: random.Random, hour: int):
    state.wind += rng.gauss(0, 0.6)
    state.wind = max(0.0, min(22.0, state.wind))
    state.wind += math.sin((hour - 3) / 24 * 2 * math.pi) * 0.05

    power = power_curve(state.wind) * scenario.power_factor
    power *= rng.uniform(0.96, 1.04)
    rpm = rpm_from_wind(state.wind) * rng.uniform(0.97, 1.03)

    load_ratio = power / RATED_POWER_KW
    ambient = 18 + math.sin((hour - 14) / 24 * 2 * math.pi) * 6
    temp = (
        ambient
        + load_ratio * 45
        + scenario.temp_offset
        + state.temp_drift
        + rng.gauss(0, 0.8)
    )
    state.temp_drift += rng.gauss(0, 0.02)
    state.temp_drift = max(-3.0, min(6.0, state.temp_drift))

    return (
        round(state.wind, 2),
        round(max(0.0, power), 2),
        round(rpm, 2),
        round(temp, 2),
    )
