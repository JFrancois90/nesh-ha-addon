"""Turn Home Assistant state objects into contract Observations."""

from __future__ import annotations

import logging
import math

from .contract import Observation, iso_utc, parse_time, utcnow
from .roommap import (ROLE_HOT_WATER, ROLE_ROOM_RH, ROLE_ROOM_TEMP, ROLE_THERMOSTAT, ROLE_VALVE,
                      ROLE_WEATHER)

log = logging.getLogger("nesh.mapping")

UNUSABLE = {"unavailable", "unknown", "none", ""}

WIND_TO_MS = {
    "m/s": 1.0,
    "km/h": 1 / 3.6,
    "kmh": 1 / 3.6,
    "mph": 0.44704,
    "kn": 0.514444,
    "kt": 0.514444,
    "knots": 0.514444,
    "ft/s": 0.3048,
}

# water_heater operation states that mean the hot water is being held on
HOT_WATER_ON = {"on", "boost", "heat_pump", "electric", "gas", "high_demand", "performance"}
HOT_WATER_OFF = {"off"}
# "eco" is Hive's schedule mode: whether water is heating right now is not exposed, so we skip it


def _num(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _to_celsius(value: float | None, unit: str | None) -> float | None:
    if value is None:
        return None
    u = (unit or "").replace("°", "").strip().upper()
    if u in ("F", "FAHRENHEIT"):
        value = (value - 32) * 5 / 9
    elif u in ("K", "KELVIN"):
        value = value - 273.15
    if not -60 <= value <= 90:
        return None
    return round(value, 2)


def _rh(value: float | None) -> float | None:
    if value is None or not 0 <= value <= 100:
        return None
    return round(value, 1)


def _state_ts(state: dict) -> str:
    raw = state.get("last_updated") or state.get("last_changed")
    if raw:
        try:
            return iso_utc(parse_time(raw))
        except (TypeError, ValueError):
            pass
    return iso_utc(utcnow())


def readings_for_state(state: dict, roles: list[tuple[str, str]], home_id: str,
                       ts_override: str | None = None,
                       climate_unit: str | None = None) -> list[Observation]:
    """Convert one HA state to zero or more Observations. Never raises."""
    try:
        return _readings(state, roles, home_id, ts_override, climate_unit)
    except Exception as exc:  # defensive: a bad entity must never stop logging
        log.warning("Could not read %s: %s", state.get("entity_id"), exc)
        return []


def _readings(state, roles, home_id, ts_override, climate_unit):
    eid = state.get("entity_id", "")
    raw_state = str(state.get("state", "")).strip()
    if raw_state.lower() in UNUSABLE:
        return []
    attrs = state.get("attributes") or {}
    ts = ts_override or _state_ts(state)
    out: list[Observation] = []

    def emit(room: str, kind: str, value: float | None, source: str) -> None:
        if value is not None:
            out.append(Observation(ts=ts, home_id=home_id, room=room, kind=kind,
                                   value=float(value), source=source, entity_id=eid))

    for role, room in roles:
        if role in (ROLE_ROOM_TEMP, ROLE_ROOM_RH):
            value = _num(raw_state)
            if role == ROLE_ROOM_TEMP:
                emit(room, "room_temp", _to_celsius(value, attrs.get("unit_of_measurement")), "sensor")
            else:
                emit(room, "room_rh", _rh(value), "sensor")
        elif role in (ROLE_VALVE, ROLE_THERMOSTAT):
            unit = attrs.get("temperature_unit") or climate_unit
            cur = _to_celsius(_num(attrs.get("current_temperature")), unit)
            target = _to_celsius(_num(attrs.get("temperature")), unit)
            action = attrs.get("hvac_action")
            demand = None
            if isinstance(action, str) and action:
                demand = 1.0 if action.lower() in ("heating", "preheating") else 0.0
            elif raw_state.lower() == "off":
                demand = 0.0
            if role == ROLE_VALVE:
                emit(room, "valve_temp", cur, "valve")
                emit(room, "valve_setpoint", target, "valve")
                emit(room, "valve_demand", demand, "valve")
            else:
                emit(room, "thermostat_temp", cur, "thermostat")
                emit(room, "thermostat_setpoint", target, "thermostat")
                emit(room, "heating_on", demand, "thermostat")
        elif role == ROLE_HOT_WATER:
            mode = raw_state.lower()
            if eid.startswith("water_heater."):
                if mode in HOT_WATER_OFF:
                    emit(room, "hot_water_on", 0.0, "thermostat")
                elif mode in HOT_WATER_ON:
                    emit(room, "hot_water_on", 1.0, "thermostat")
            else:  # a binary_sensor, switch or sensor that reports on/off directly
                if mode in ("on", "true", "1", "heating"):
                    emit(room, "hot_water_on", 1.0, "thermostat")
                elif mode in ("off", "false", "0", "idle"):
                    emit(room, "hot_water_on", 0.0, "thermostat")
        elif role == ROLE_WEATHER:
            emit(room, "outdoor_temp", _to_celsius(_num(attrs.get("temperature")),
                                                   attrs.get("temperature_unit")), "weather")
            wind = _num(attrs.get("wind_speed"))
            if wind is not None:
                factor = WIND_TO_MS.get(str(attrs.get("wind_speed_unit") or "km/h").strip().lower())
                if factor is not None and 0 <= wind < 500:
                    emit(room, "wind_speed", round(wind * factor, 2), "weather")
            cloud = _num(attrs.get("cloud_coverage"))
            if cloud is not None and 0 <= cloud <= 100:
                emit(room, "cloud_cover", round(cloud, 1), "weather")
    return out
