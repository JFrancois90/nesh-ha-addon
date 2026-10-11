"""Discover heating entities and propose a room map.

Works from integration platform, device_class, device model and area, because the
exact entity ids in a given house are not known in advance. Every decision that
is a guess is written to the notes so the owner can check it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from .roommap import RoomEntry, RoomMap

log = logging.getLogger("nesh.discovery")

TRV_WORDS = ("trv", "radiator valve", "radiator", "rad valve", "valve")
THERMOSTAT_WORDS = ("thermostat", "heating", "receiver", "boiler", "slr", "central")
ROOM_ALIASES = {
    "living_room": "living",
    "lounge": "living",
    "sitting_room": "living",
    "hallway": "hall",
    "entrance_hall": "hall",
}
# room words recognised in entity names when an entity has no area
ROOM_VOCAB = (
    "living room", "sitting room", "lounge", "living", "kitchen", "dining room", "dining",
    "bedroom", "office", "study", "hallway", "hall", "landing", "bathroom", "nursery",
    "utility", "conservatory", "loft", "spare room",
)
ROOM_SENSOR_PLATFORM_PREFERENCE = ("govee_ble",)
IGNORED_SENSOR_PLATFORMS = ("hive",)  # Hive's own temperature sensors duplicate the climate entities


def slugify(text: str) -> str:
    s = text.strip().lower().replace("&", " and ").replace("'", "")
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s or "room"


@dataclass
class Registries:
    states: list[dict]
    entities: list[dict]
    devices: list[dict]
    areas: list[dict]
    floors: list[dict] = field(default_factory=list)


@dataclass
class Candidate:
    entity_id: str
    platform: str
    room: str | None
    room_how: str  # "area", "device area", "name", ""
    device_id: str | None
    label: str


@dataclass
class Discovery:
    room_map: RoomMap
    notes: list[str]
    unassigned: list[str]

    def proposal_yaml(self) -> str:
        lines = ["# Nesh proposed room map. Review it, then copy any corrections into the",
                 "# add-on Configuration tab under room_map. This file is rewritten on each start.", ""]
        body = self.room_map.to_yaml()
        lines.append(body.rstrip())
        if self.unassigned:
            lines += ["", "# Found but not used (no room, duplicate, or not recognised):"]
            lines += [f"#   {u}" for u in self.unassigned]
        if self.notes:
            lines += ["", "# Notes:"]
            lines += [f"#   {n}" for n in self.notes]
        return "\n".join(lines) + "\n"


class _AreaIndex:
    def __init__(self, areas: list[dict], floors: list[dict]):
        floor_level = {}
        for f in floors or []:
            if f.get("floor_id") is not None:
                floor_level[f["floor_id"]] = f.get("level")
        raw_slugs: dict[str, str] = {}
        for a in areas or []:
            raw_slugs[a["area_id"]] = slugify(a.get("name") or a["area_id"])
        used = set(raw_slugs.values())
        self.slug: dict[str, str] = {}
        self.floor: dict[str, int | None] = {}
        for a in areas or []:
            s = raw_slugs[a["area_id"]]
            alias = ROOM_ALIASES.get(s)
            if alias and alias not in used:
                s = alias
                used.add(alias)
            self.slug[a["area_id"]] = s
            self.floor[s] = floor_level.get(a.get("floor_id"))
        # names to look for in entity names, longest first so "living room" beats "living"
        names: dict[str, str] = {}
        for a in areas or []:
            names[(a.get("name") or "").lower()] = self.slug[a["area_id"]]
            for alias in a.get("aliases") or []:
                names[str(alias).lower()] = self.slug[a["area_id"]]
        for word in ROOM_VOCAB:
            if word not in names:
                s = slugify(word)
                names[word] = ROOM_ALIASES.get(s, s)
        self.name_patterns = sorted(((n, s) for n, s in names.items() if n), key=lambda x: -len(x[0]))

    def room_from_name(self, *texts: str) -> str | None:
        hay = " ".join(t for t in texts if t).lower().replace("_", " ").replace(".", " ")
        for name, slug in self.name_patterns:
            if re.search(r"(^|[^a-z])" + re.escape(name) + r"($|[^a-z])", hay):
                return slug
        return None


def _friendly(state: dict) -> str:
    return str((state.get("attributes") or {}).get("friendly_name") or "")


def propose_room_map(home_id: str, reg: Registries) -> Discovery:
    notes: list[str] = []
    unassigned: list[str] = []
    ents = {e["entity_id"]: e for e in reg.entities if e.get("entity_id")}
    devs = {d["id"]: d for d in reg.devices if d.get("id")}
    areas = _AreaIndex(reg.areas, reg.floors)

    climate_device_ids = set()
    for s in reg.states:
        if s.get("entity_id", "").startswith("climate."):
            did = (ents.get(s["entity_id"]) or {}).get("device_id")
            if did:
                climate_device_ids.add(did)

    def candidate(state: dict) -> Candidate | None:
        eid = state.get("entity_id", "")
        e = ents.get(eid, {})
        if e.get("disabled_by"):
            return None
        dev = devs.get(e.get("device_id") or "", {})
        label = " ".join(x for x in (_friendly(state), e.get("name") or "", e.get("original_name") or "",
                                     dev.get("name_by_user") or "", dev.get("name") or "") if x)
        room, how = None, ""
        if e.get("area_id") and e["area_id"] in areas.slug:
            room, how = areas.slug[e["area_id"]], "area"
        elif dev.get("area_id") and dev["area_id"] in areas.slug:
            room, how = areas.slug[dev["area_id"]], "device area"
        else:
            room = areas.room_from_name(label, eid.split(".", 1)[-1])
            how = "name" if room else ""
        return Candidate(eid, e.get("platform") or "", room, how, e.get("device_id"), label)

    def device_text(c: Candidate) -> str:
        dev = devs.get(c.device_id or "", {})
        return " ".join(str(x) for x in (dev.get("model"), dev.get("model_id"), dev.get("manufacturer"),
                                          c.label, c.entity_id) if x).lower()

    def has_word(text: str, words: tuple[str, ...]) -> bool:
        return any(re.search(r"(^|[^a-z])" + re.escape(w) + r"($|[^a-z])", text.replace("_", " "))
                   for w in words)

    valves: list[Candidate] = []
    thermostats: list[Candidate] = []
    ambiguous: list[Candidate] = []
    hot_water: list[Candidate] = []
    weather: list[Candidate] = []
    temp_sensors: list[Candidate] = []
    rh_sensors: list[Candidate] = []

    for state in reg.states:
        try:
            eid = state.get("entity_id", "")
            domain = eid.split(".", 1)[0]
            if domain not in ("climate", "water_heater", "weather", "sensor"):
                continue
            c = candidate(state)
            if c is None:
                continue
            attrs = state.get("attributes") or {}
            if domain == "climate":
                text = device_text(c)
                if has_word(text, TRV_WORDS):
                    valves.append(c)
                elif has_word(text, THERMOSTAT_WORDS):
                    thermostats.append(c)
                elif c.platform == "hive":
                    ambiguous.append(c)
                else:
                    unassigned.append(f"{eid} (climate entity, platform {c.platform or 'unknown'}, not recognised)")
            elif domain == "water_heater":
                hot_water.append(c)
            elif domain == "weather":
                weather.append(c)
            elif domain == "sensor":
                dc = attrs.get("device_class")
                if dc not in ("temperature", "humidity"):
                    continue
                e = ents.get(eid, {})
                if e.get("entity_category"):
                    continue
                if c.platform in IGNORED_SENSOR_PLATFORMS or (c.device_id and c.device_id in climate_device_ids):
                    continue
                if c.room is None:
                    if c.platform in ROOM_SENSOR_PLATFORM_PREFERENCE:
                        unassigned.append(f"{eid} ({dc} sensor, platform {c.platform}, no area and no room in its name)")
                    continue
                (temp_sensors if dc == "temperature" else rh_sensors).append(c)
        except Exception as exc:  # never let one odd entity stop discovery
            log.warning("Skipping entity during discovery: %s", exc)

    # Resolve ambiguous Hive climate entities.
    if ambiguous:
        if not thermostats and len(ambiguous) == 1:
            thermostats.append(ambiguous[0])
            notes.append(f"{ambiguous[0].entity_id} assumed to be the Hive thermostat (only Hive climate entity without TRV in its model or name).")
        else:
            for c in ambiguous:
                valves.append(c)
                notes.append(f"{c.entity_id} assumed to be a TRV (Hive climate entity, model and name did not say).")

    rm = RoomMap(home_id=home_id)

    def room_entry(room: str) -> RoomEntry:
        if room not in rm.rooms:
            rm.rooms[room] = RoomEntry(floor=areas.floor.get(room))
        return rm.rooms[room]

    # thermostat
    if thermostats:
        pick = sorted(thermostats, key=lambda c: (c.platform != "hive", c.entity_id))[0]
        rm.thermostat = pick.entity_id
        rm.thermostat_room = pick.room or "house"
        if pick.room:
            room_entry(pick.room)
        else:
            notes.append(f"Thermostat {pick.entity_id} has no area, so its readings are logged under room 'house'.")
        for other in thermostats:
            if other is not pick:
                unassigned.append(f"{other.entity_id} (second thermostat candidate)")
    else:
        notes.append("No thermostat found. Set thermostat_entity in the Configuration tab.")

    # valves
    for c in sorted(valves, key=lambda c: c.entity_id):
        if not c.room:
            unassigned.append(f"{c.entity_id} (TRV with no area and no room in its name)")
            continue
        room_entry(c.room).valves.append(c.entity_id)
        if c.room_how == "name":
            notes.append(f"{c.entity_id} put in room '{c.room}' from its name; give it an area in Home Assistant to be sure.")
    for room, entry in rm.rooms.items():
        if len(entry.valves) > 1:
            notes.append(f"Room '{room}' has {len(entry.valves)} valves: {', '.join(entry.valves)}.")

    # room sensors: prefer govee_ble, then entities that sit in an area directly
    def rank(c: Candidate) -> tuple:
        return (c.platform not in ROOM_SENSOR_PLATFORM_PREFERENCE, c.room_how != "area" and c.room_how != "device area",
                c.entity_id)

    by_room_t: dict[str, list[Candidate]] = {}
    for c in temp_sensors:
        by_room_t.setdefault(c.room, []).append(c)
    by_room_h: dict[str, list[Candidate]] = {}
    for c in rh_sensors:
        by_room_h.setdefault(c.room, []).append(c)

    for room, cands in by_room_t.items():
        cands.sort(key=rank)
        entry = room_entry(room)
        entry.sensor = cands[0].entity_id
        for extra in cands[1:]:
            unassigned.append(f"{extra.entity_id} (extra temperature sensor in {room})")
    for room, cands in by_room_h.items():
        entry = room_entry(room)
        temp = next((c for c in by_room_t.get(room, []) if c.entity_id == entry.sensor), None)
        cands.sort(key=lambda c: (temp is None or c.device_id != temp.device_id, *rank(c)))
        entry.humidity = cands[0].entity_id
        for extra in cands[1:]:
            unassigned.append(f"{extra.entity_id} (extra humidity sensor in {room})")

    # hot water and weather
    if hot_water:
        pick = sorted(hot_water, key=lambda c: (c.platform != "hive", c.entity_id))[0]
        rm.hot_water = pick.entity_id
        unassigned += [f"{c.entity_id} (second water heater)" for c in hot_water if c is not pick]
    else:
        notes.append("No hot water entity found.")
    if weather:
        pick = sorted(weather, key=lambda c: (c.platform != "met", c.entity_id))[0]
        rm.weather = pick.entity_id
        unassigned += [f"{c.entity_id} (second weather entity)" for c in weather if c is not pick]
    else:
        notes.append("No weather entity found; outdoor temperature will be missing.")

    for room, entry in sorted(rm.rooms.items()):
        if not entry.sensor:
            notes.append(f"Room '{room}' has no independent temperature sensor.")

    return Discovery(room_map=rm, notes=notes, unassigned=unassigned)
