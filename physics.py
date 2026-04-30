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
    temp_offset: float        # statisk baseline-offset (°C)
    power_factor: float
    base_error: str
    battery_drain: float
    temp_drift_per_day: float = 0.0  # lineær drift over tid (°C/dag) — driver PdM lag 3


SCENARIOS = [
    # 0: sundt — ingen drift, ingen offset
    Scenario("healthy",        0.0,  1.00, OK_ERROR, 0.02),
    # 1: accelerende gearkasse-fejl: +1.0°C/dag (svær, men realistisk failure-mode)
    Scenario("overheat_drift", 4.0,  0.95, "E12",    0.04, temp_drift_per_day=1.0),
    # 2: lav effekt — generator-issue, ingen temp-drift
    Scenario("low_power",     -2.0,  0.55, "W04",    0.06),
    # 3: aldring — langsom slidtage +0.3°C/dag
    Scenario("aging",          5.0,  0.90, "W11",    0.08, temp_drift_per_day=0.3),
    # 4: flaky sensor — støj, ingen ægte temp-issue
    Scenario("flaky_sensor",   2.0,  1.00, "E07",    0.03),
]


# Realistisk distribution: ~67% sunde, kun få i aktive failure-modes.
# (En park hvor 44% er i alarm-tilstand ville være lukket ned i virkeligheden.)
# Index i denne liste = scenarie-index i SCENARIOS.
DEVICE_SCENARIO_MAPPING = [
    0, 0, 1, 0, 0, 3, 2, 0, 4, 0,  # 10 første: 1 overheat, 1 aging, 1 low_power, 1 flaky, 6 healthy
    0, 3, 0, 0, 1, 0, 2, 0,         # næste 8: 1 overheat, 1 aging, 1 low_power, 5 healthy
]
# Resultat for 18 møller: 11 healthy, 2 overheat_drift, 2 aging, 2 low_power, 1 flaky_sensor


def scenario_for(idx: int) -> Scenario:
    """Map et device-index til dets driftscenarie.

    Brugt af både seed.py og simulator.py så hver mølle har samme scenarie
    konsistent over tid — kritisk for at predictions kan se trends.
    """
    return SCENARIOS[DEVICE_SCENARIO_MAPPING[idx % len(DEVICE_SCENARIO_MAPPING)]]


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


WIND_MEAN_MS = 9.0           # langsigtet middelvind
WIND_REVERSION_RATE = 0.05   # styrken af mean-reversion (Ornstein-Uhlenbeck)


def next_tick(
    state: TickState,
    scenario: Scenario,
    rng: random.Random,
    hour: int,
    cumulative_days: float = 0.0,
):
    """Beregn næste sensor-måling for en mølle.

    cumulative_days: hvor længe enheden har været i drift i dette scenarie.
    Driver lineær temperatur-drift (°C/dag × dage) — fundamentet for PdM-trend-analyse.

    Vind: mean-reverting random walk (Ornstein-Uhlenbeck) — pull tilbage mod
    WIND_MEAN_MS forhindrer langsigtet drift som ville forurene trend-signal.
    """
    state.wind += rng.gauss(0, 0.6) - WIND_REVERSION_RATE * (state.wind - WIND_MEAN_MS)
    state.wind = max(0.0, min(22.0, state.wind))
    state.wind += math.sin((hour - 3) / 24 * 2 * math.pi) * 0.05

    power = power_curve(state.wind) * scenario.power_factor
    power *= rng.uniform(0.96, 1.04)
    rpm = rpm_from_wind(state.wind) * rng.uniform(0.97, 1.03)

    load_ratio = power / RATED_POWER_KW
    ambient = 18 + math.sin((hour - 14) / 24 * 2 * math.pi) * 6
    long_term_drift = scenario.temp_drift_per_day * cumulative_days
    temp = (
        ambient
        + load_ratio * 45
        + scenario.temp_offset
        + long_term_drift          # lineær degradering over tid (PdM)
        + state.temp_drift          # kortsigtet random walk (støj)
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
