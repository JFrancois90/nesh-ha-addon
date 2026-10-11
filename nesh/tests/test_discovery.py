import asyncio

import aiohttp
import yaml

from nesh_agent.agent import Agent
from nesh_agent.config import Options
from nesh_agent.discovery import Registries, propose_room_map, slugify
from nesh_agent.ha import Endpoints, HAWebSocket
from nesh_agent.roommap import apply_overrides, room_map_from_dict
from tests.fake_ha import FakeHA
from tests.fixtures import build_house


def _registries(fake: FakeHA) -> Registries:
    return Registries(states=list(fake.states.values()), entities=fake.entities, devices=fake.devices,
                      areas=fake.areas, floors=fake.floors)


def test_slugify_and_aliases():
    assert slugify("Living Room") == "living_room"
    assert slugify("Kid's Bedroom") == "kids_bedroom"


def test_proposed_room_map_for_hive_govee_house():
    fake = FakeHA()
    build_house(fake)
    disc = propose_room_map("myhome", _registries(fake))
    rm = disc.room_map
    assert set(rm.rooms) == {"kitchen", "living", "bedroom", "office", "hall"}
    assert rm.thermostat == "climate.heating"
    assert rm.thermostat_room == "hall"
    assert rm.hot_water == "water_heater.hot_water"
    assert rm.weather == "weather.forecast_home"
    assert rm.rooms["kitchen"].valves == ["climate.kitchen_trv"]
    assert rm.rooms["kitchen"].sensor == "sensor.h5075_kitchen_temperature"
    assert rm.rooms["kitchen"].humidity == "sensor.h5075_kitchen_humidity"
    assert rm.rooms["living"].valves == ["climate.living_room"]
    assert rm.rooms["living"].sensor == "sensor.h5075_1a2b_temperature"
    assert rm.rooms["office"].valves == ["climate.office_trv"]          # placed by name
    assert rm.rooms["office"].sensor == "sensor.h5075_5e6f_temperature"  # entity area only
    hall = rm.rooms["hall"]
    assert hall.valves == [] and hall.sensor is None and hall.humidity is None
    assert hall.floor == 0 and rm.rooms["bedroom"].floor == 1
    # Hive's own temperature sensors and unrelated sensors are not room sensors
    used = set(rm.entity_ids())
    assert "sensor.heating_current_temperature" not in used
    assert "sensor.kitchen_trv_current_temperature" not in used
    assert "sensor.processor_temperature" not in used
    assert any("office_trv" in n and "name" in n for n in disc.notes)
    assert any("hall" in n and "no independent temperature sensor" in n for n in disc.notes)


def test_proposal_yaml_round_trips_to_contract_layout():
    fake = FakeHA()
    build_house(fake)
    disc = propose_room_map("myhome", _registries(fake))
    data = yaml.safe_load(disc.proposal_yaml())
    assert set(data) >= {"home_id", "rooms", "thermostat", "hot_water", "weather"}
    assert data["rooms"]["hall"] == {"sensor": None, "humidity": None, "valve": None, "floor": 0}
    assert data["rooms"]["kitchen"]["valve"] == "climate.kitchen_trv"
    again = room_map_from_dict(data)
    assert again.entity_roles() == disc.room_map.entity_roles()


def test_ambiguous_hive_climate_entities_are_handled():
    fake = FakeHA()
    build_house(fake)
    for d in fake.devices:  # strip every hint from models and names
        if d["manufacturer"] == "Hive":
            d["model"] = "UK7004240"
    for e in fake.entities:
        if e["entity_id"] == "climate.heating":
            e["original_name"] = None
    for d in fake.devices:
        if d["id"] == "dev_heating":
            d["name"] = "Receiver"
    disc = propose_room_map("h", _registries(fake))
    assert disc.room_map.thermostat == "climate.heating"
    assert disc.room_map.rooms["kitchen"].valves == ["climate.kitchen_trv"]  # "TRV" still in its name


def test_overrides_win_and_release_entities_from_other_rooms():
    fake = FakeHA()
    build_house(fake)
    rm = propose_room_map("h", _registries(fake)).room_map
    out = apply_overrides(rm, [{"room": "study", "valve": "climate.office_trv",
                                "sensor": "sensor.h5075_5e6f_temperature", "floor": 1}],
                          weather="weather.other")
    assert out.rooms["study"].valves == ["climate.office_trv"]
    assert out.rooms["office"].valves == [] and out.rooms["office"].sensor is None
    assert out.weather == "weather.other"


def test_discovery_over_websocket_writes_proposal(tmp_path):
    async def go():
        fake = FakeHA()
        build_house(fake)
        url = await fake.start()
        try:
            async with aiohttp.ClientSession() as session:
                agent = Agent(Options(home_id="myhome"), Endpoints.from_ha_url(url), fake.token, tmp_path)
                ws = HAWebSocket(session, Endpoints.from_ha_url(url), fake.token)
                await ws.connect()
                reg = await agent._fetch_registries(ws)
                agent.apply_discovery(reg)
                await ws.close()
        finally:
            await fake.close()
        return agent

    agent = asyncio.run(go())
    text = (tmp_path / "room_map.proposed.yaml").read_text()
    assert "climate.kitchen_trv" in text and text.startswith("# Nesh proposed room map")
    assert (tmp_path / "room_map.yaml").exists()
    assert agent.room_map.rooms["hall"].floor == 0


def test_registry_errors_fall_back_to_names(tmp_path):
    async def go():
        fake = FakeHA()
        build_house(fake)
        url = await fake.start()
        # make the area registry command fail like an older or restricted HA would
        fake.areas = None
        try:
            async with aiohttp.ClientSession() as session:
                agent = Agent(Options(home_id="h"), Endpoints.from_ha_url(url), fake.token, tmp_path)
                ws = HAWebSocket(session, Endpoints.from_ha_url(url), fake.token)
                await ws.connect()
                reg = await agent._fetch_registries(ws)
                await ws.close()
        finally:
            await fake.close()
        return reg

    reg = asyncio.run(go())
    assert reg.areas == [] and reg.entities  # other registries still read
