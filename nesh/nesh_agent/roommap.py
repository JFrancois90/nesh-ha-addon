"""Room map: which Home Assistant entity feeds which room and kind.

YAML layout follows the contract:

    home_id: myhome
    rooms:
      kitchen: {sensor: sensor.x_temperature, humidity: sensor.x_humidity, valve: climate.kitchen_trv, floor: 0}
      hall:    {valve: null, floor: 0}
    thermostat: climate.heating
    hot_water: water_heater.hot_water
    weather: weather.forecast_home

Two optional extensions, both ignored by readers that do not know them:
  * valve may be a list when a room has more than one radiator valve
  * thermostat_room names the room the thermostat sits in (default "house")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import yaml

from .contract import SLUG_RE

log = logging.getLogger("nesh.roommap")

# role names used internally to route entity states to contract kinds
ROLE_ROOM_TEMP = "room_temp"
ROLE_ROOM_RH = "room_rh"
ROLE_VALVE = "valve"
ROLE_THERMOSTAT = "thermostat"
ROLE_HOT_WATER = "hot_water"
ROLE_WEATHER = "weather"


@dataclass
class RoomEntry:
    sensor: str | None = None
    humidity: str | None = None
    valves: list[str] = field(default_factory=list)
    floor: int | None = None

    def to_yaml_dict(self) -> dict:
        valve: object
        if not self.valves:
            valve = None
        elif len(self.valves) == 1:
            valve = self.valves[0]
        else:
            valve = list(self.valves)
        return {"sensor": self.sensor, "humidity": self.humidity, "valve": valve, "floor": self.floor}


@dataclass
class RoomMap:
    home_id: str
    rooms: dict[str, RoomEntry] = field(default_factory=dict)
    thermostat: str | None = None
    thermostat_room: str = "house"
    hot_water: str | None = None
    weather: str | None = None

    def entity_roles(self) -> dict[str, list[tuple[str, str]]]:
        """entity_id -> [(role, room)]."""
        out: dict[str, list[tuple[str, str]]] = {}

        def add(eid: str | None, role: str, room: str) -> None:
            if eid:
                out.setdefault(eid, []).append((role, room))

        for room, entry in self.rooms.items():
            add(entry.sensor, ROLE_ROOM_TEMP, room)
            add(entry.humidity, ROLE_ROOM_RH, room)
            for v in entry.valves:
                add(v, ROLE_VALVE, room)
        add(self.thermostat, ROLE_THERMOSTAT, self.thermostat_room or "house")
        add(self.hot_water, ROLE_HOT_WATER, "house")
        add(self.weather, ROLE_WEATHER, "outside")
        return out

    def entity_ids(self) -> list[str]:
        return sorted(self.entity_roles())

    def to_dict(self) -> dict:
        return {
            "home_id": self.home_id,
            "rooms": {r: e.to_yaml_dict() for r, e in sorted(self.rooms.items())},
            "thermostat": self.thermostat,
            "thermostat_room": self.thermostat_room,
            "hot_water": self.hot_water,
            "weather": self.weather,
        }

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.to_dict(), sort_keys=False, default_flow_style=False)


def _as_valve_list(value: object) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, list):
        return [str(v).strip() for v in value if v]
    return []


def room_map_from_dict(data: dict) -> RoomMap:
    """Load the contract YAML layout (as a dict)."""
    rm = RoomMap(home_id=str(data.get("home_id") or ""))
    for room, entry in (data.get("rooms") or {}).items():
        entry = entry or {}
        rm.rooms[str(room)] = RoomEntry(
            sensor=entry.get("sensor") or None,
            humidity=entry.get("humidity") or None,
            valves=_as_valve_list(entry.get("valve")),
            floor=entry.get("floor"),
        )
    rm.thermostat = data.get("thermostat") or None
    rm.thermostat_room = data.get("thermostat_room") or "house"
    rm.hot_water = data.get("hot_water") or None
    rm.weather = data.get("weather") or None
    return rm


def apply_overrides(base: RoomMap, room_overrides: list[dict], *, thermostat: str = "",
                    hot_water: str = "", weather: str = "") -> RoomMap:
    """Owner overrides win. A room listed in room_overrides replaces the discovered
    entry for that room entirely; any entity it names is removed from other rooms."""
    rm = RoomMap(home_id=base.home_id, thermostat=base.thermostat, thermostat_room=base.thermostat_room,
                 hot_water=base.hot_water, weather=base.weather)
    rm.rooms = {r: RoomEntry(e.sensor, e.humidity, list(e.valves), e.floor) for r, e in base.rooms.items()}

    for item in room_overrides or []:
        room = str(item.get("room") or "").strip().lower()
        if not SLUG_RE.match(room):
            log.warning("room_map entry with invalid room name %r ignored", item.get("room"))
            continue
        entry = RoomEntry(
            sensor=(item.get("sensor") or None),
            humidity=(item.get("humidity") or None),
            valves=_as_valve_list(item.get("valve")),
            floor=item.get("floor") if item.get("floor") is not None else
            (rm.rooms[room].floor if room in rm.rooms else None),
        )
        claimed = {e for e in (entry.sensor, entry.humidity, *entry.valves) if e}
        for other_name, other in rm.rooms.items():
            if other_name == room:
                continue
            if other.sensor in claimed:
                other.sensor = None
            if other.humidity in claimed:
                other.humidity = None
            other.valves = [v for v in other.valves if v not in claimed]
        rm.rooms[room] = entry

    if thermostat:
        rm.thermostat = thermostat
    if hot_water:
        rm.hot_water = hot_water
    if weather:
        rm.weather = weather
    return rm
