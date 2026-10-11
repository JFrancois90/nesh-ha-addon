"""Nesh data contract v0.1: Observation rows and setpoint commands.

Mirrors /contracts/observations.md. All times are UTC ISO 8601 with a Z suffix.
Temperatures in degrees C, humidity in %RH, wind in m/s, GHI in W/m2, cloud in %.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone

CONTRACT_VERSION = "0.1"

# kind -> the source the contract says it must come from
KIND_SOURCE: dict[str, str] = {
    "room_temp": "sensor",
    "room_rh": "sensor",
    "valve_temp": "valve",
    "valve_setpoint": "valve",
    "valve_demand": "valve",
    "thermostat_temp": "thermostat",
    "thermostat_setpoint": "thermostat",
    "heating_on": "thermostat",
    "hot_water_on": "thermostat",
    "outdoor_temp": "weather",
    "wind_speed": "weather",
    "solar_ghi": "weather",
    "cloud_cover": "weather",
    "gas_kwh": "meter",
    "flow_temp": "boiler",
    "return_temp": "boiler",
    "burner_on": "boiler",
    "modulation": "boiler",
}
KINDS = frozenset(KIND_SOURCE)
SOURCES = frozenset(KIND_SOURCE.values())
BOOLEAN_KINDS = frozenset({"valve_demand", "heating_on", "hot_water_on", "burner_on"})

SLUG_RE = re.compile(r"^[a-z0-9_]+$")
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")
FIELDS = ("ts", "home_id", "room", "kind", "value", "source", "entity_id")


def iso_utc(dt: datetime) -> str:
    """Format an aware datetime as 2026-10-11T05:40:00Z (second precision)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: str) -> datetime:
    """Parse an ISO 8601 timestamp as Home Assistant sends it. Returns aware UTC."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Observation:
    ts: str
    home_id: str
    room: str
    kind: str
    value: float
    source: str
    entity_id: str = ""

    def to_dict(self) -> dict:
        return {f: getattr(self, f) for f in FIELDS}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"), ensure_ascii=True)

    def key(self) -> tuple:
        """Identity used for idempotent writes."""
        return (self.ts, self.room, self.kind, self.entity_id)


def validate_observation(obs: Observation) -> list[str]:
    errors: list[str] = []
    if not TS_RE.match(obs.ts or ""):
        errors.append(f"ts not ISO UTC with Z: {obs.ts!r}")
    if not obs.home_id:
        errors.append("home_id empty")
    if not SLUG_RE.match(obs.room or ""):
        errors.append(f"room not a slug: {obs.room!r}")
    if obs.kind not in KINDS:
        errors.append(f"unknown kind: {obs.kind!r}")
    elif KIND_SOURCE[obs.kind] != obs.source:
        errors.append(f"kind {obs.kind} needs source {KIND_SOURCE[obs.kind]}, got {obs.source}")
    if not isinstance(obs.value, (int, float)) or isinstance(obs.value, bool) or not math.isfinite(obs.value):
        errors.append(f"value not a finite number: {obs.value!r}")
    elif obs.kind in BOOLEAN_KINDS and obs.value not in (0, 1):
        errors.append(f"boolean kind {obs.kind} must be 0 or 1")
    if not isinstance(obs.entity_id, str):
        errors.append("entity_id must be a string")
    return errors


def observation_from_dict(data: dict) -> Observation:
    return Observation(**{f: data[f] for f in FIELDS})


# ---------------------------------------------------------------- commands

COMMAND_FIELDS = ("at", "room", "entity_id", "setpoint", "reason", "plan_id")


@dataclass(frozen=True)
class SetpointCommand:
    at: str
    room: str
    entity_id: str
    setpoint: float
    reason: str
    plan_id: str


def parse_command(data: object) -> tuple[SetpointCommand | None, list[str]]:
    """Validate a setpoint command dict against the contract."""
    if not isinstance(data, dict):
        return None, ["command must be an object"]
    errors: list[str] = []
    missing = [f for f in COMMAND_FIELDS if f not in data]
    if missing:
        errors.append(f"missing fields: {', '.join(missing)}")
        return None, errors
    at = data["at"]
    if not isinstance(at, str) or not TS_RE.match(at):
        errors.append(f"at must be ISO UTC with Z, got {at!r}")
    room = data["room"]
    if not isinstance(room, str) or not SLUG_RE.match(room):
        errors.append(f"room must be a slug, got {room!r}")
    entity_id = data["entity_id"]
    if not isinstance(entity_id, str) or not entity_id.startswith("climate."):
        errors.append(f"entity_id must be a climate entity, got {entity_id!r}")
    sp = data["setpoint"]
    if isinstance(sp, bool) or not isinstance(sp, (int, float)) or not math.isfinite(sp):
        errors.append(f"setpoint must be a number, got {sp!r}")
    elif not 0 <= sp <= 35:
        errors.append(f"setpoint {sp} outside plausible range 0 to 35 C")
    reason = data["reason"]
    if not isinstance(reason, str) or not reason.strip() or "\n" in reason:
        errors.append("reason must be one non-empty line")
    if not isinstance(data["plan_id"], str) or not data["plan_id"]:
        errors.append("plan_id must be a non-empty string")
    if errors:
        return None, errors
    return SetpointCommand(at=at, room=room, entity_id=entity_id, setpoint=float(sp),
                           reason=reason.strip(), plan_id=data["plan_id"]), []
