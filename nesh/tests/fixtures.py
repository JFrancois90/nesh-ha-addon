"""A Hive + Govee house in the fake Home Assistant.

Rooms: kitchen, living room, bedroom, office, hall. The hall has the Hive
thermostat but no TRV and no Govee sensor. The office TRV has no area, so it
has to be placed by name. The office Govee has an area on the entity only.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tests.fake_ha import FakeHA

C = "°C"


def _ent(entity_id, platform, device_id=None, area_id=None, name=None, original_name=None,
         entity_category=None, disabled_by=None):
    return {"entity_id": entity_id, "platform": platform, "device_id": device_id, "area_id": area_id,
            "name": name, "original_name": original_name, "entity_category": entity_category,
            "disabled_by": disabled_by, "hidden_by": None, "id": entity_id.replace(".", "_"),
            "has_entity_name": True}


def _dev(dev_id, name, manufacturer, model, area_id=None):
    return {"id": dev_id, "name": name, "name_by_user": None, "manufacturer": manufacturer,
            "model": model, "area_id": area_id}


def climate_attrs(current, target, action, mode="heat", name=""):
    return {"current_temperature": current, "temperature": target, "hvac_action": action,
            "hvac_mode": mode, "hvac_modes": ["off", "heat", "auto"], "min_temp": 5, "max_temp": 32,
            "friendly_name": name}


def sensor_attrs(dc, unit, name):
    return {"device_class": dc, "unit_of_measurement": unit, "state_class": "measurement",
            "friendly_name": name}


GOVEE = {
    "kitchen": ("sensor.h5075_kitchen", "dev_govee_kitchen", "H5075 Kitchen", "kitchen", None),
    "living": ("sensor.h5075_1a2b", "dev_govee_1a2b", "H5075 1A2B", "living_room", None),
    "bedroom": ("sensor.h5075_3c4d", "dev_govee_3c4d", "H5075 3C4D", "bedroom", None),
    "office": ("sensor.h5075_5e6f", "dev_govee_5e6f", "H5075 5E6F", None, "office"),  # entity area only
}

TRVS = {
    "kitchen": ("climate.kitchen_trv", "dev_trv_kitchen", "Kitchen TRV", "TRV", "kitchen"),
    "living": ("climate.living_room", "dev_trv_living", "Living Room", "Hive Radiator Valve", "living_room"),
    "bedroom": ("climate.bedroom", "dev_trv_bedroom", "Bedroom", "TRV", "bedroom"),
    "office": ("climate.office_trv", "dev_trv_office", "Office TRV", "TRV", None),  # no area: by name
}

START_TEMPS = {"kitchen": 19.5, "living": 20.2, "bedroom": 17.8, "office": 18.9}


def build_house(fake: FakeHA) -> None:
    fake.floors = [{"floor_id": "ground", "name": "Ground", "level": 0},
                   {"floor_id": "first", "name": "First", "level": 1}]
    fake.areas = [
        {"area_id": "kitchen", "name": "Kitchen", "floor_id": "ground", "aliases": []},
        {"area_id": "living_room", "name": "Living Room", "floor_id": "ground", "aliases": []},
        {"area_id": "hall", "name": "Hall", "floor_id": "ground", "aliases": []},
        {"area_id": "bedroom", "name": "Bedroom", "floor_id": "first", "aliases": []},
        {"area_id": "office", "name": "Office", "floor_id": "first", "aliases": []},
    ]
    devices = [
        _dev("dev_heating", "Heating", "Hive", "SLR2", "hall"),
        _dev("dev_hotwater", "Hot Water", "Hive", "SLR2", "hall"),
        _dev("dev_pc", "Home Assistant Host", None, "System Monitor", None),
    ]
    entities = [
        _ent("climate.heating", "hive", "dev_heating", original_name="Heating"),
        _ent("water_heater.hot_water", "hive", "dev_hotwater", original_name="Hot Water"),
        _ent("sensor.heating_current_temperature", "hive", "dev_heating", original_name="Current Temperature"),
        _ent("switch.heat_on_demand_kitchen", "hive", "dev_trv_kitchen", original_name="Heat on Demand"),
        _ent("sensor.processor_temperature", "systemmonitor", "dev_pc", original_name="Processor temperature"),
        _ent("weather.forecast_home", "met", None, name="Forecast Home"),
    ]
    for room, (eid, dev_id, name, model, area) in TRVS.items():
        devices.append(_dev(dev_id, name, "Hive", model, area))
        entities.append(_ent(eid, "hive", dev_id))
        # the TRV's own temperature sensor: must not be taken for a room sensor
        entities.append(_ent(f"sensor.{eid.split('.')[1]}_current_temperature", "hive", dev_id))
        entities.append(_ent(f"sensor.{eid.split('.')[1]}_battery", "hive", dev_id,
                             entity_category="diagnostic"))
    for room, (base, dev_id, name, dev_area, ent_area) in GOVEE.items():
        devices.append(_dev(dev_id, name, "Govee", "H5075", dev_area))
        entities.append(_ent(f"{base}_temperature", "govee_ble", dev_id, area_id=ent_area))
        entities.append(_ent(f"{base}_humidity", "govee_ble", dev_id, area_id=ent_area))
        entities.append(_ent(f"{base}_battery", "govee_ble", dev_id, area_id=ent_area,
                             entity_category="diagnostic"))
    fake.devices = devices
    fake.entities = entities

    t0 = datetime.now(timezone.utc) - timedelta(minutes=5)
    fake.set_state("climate.heating", "heat", climate_attrs(19.0, 18.0, "idle", name="Heating"), t0)
    fake.set_state("water_heater.hot_water", "eco",
                   {"operation_list": ["eco", "on", "off"], "operation_mode": "eco", "friendly_name": "Hot Water"}, t0)
    fake.set_state("sensor.heating_current_temperature", "19.0", sensor_attrs("temperature", C, "Current Temperature"), t0)
    fake.set_state("switch.heat_on_demand_kitchen", "off", {"friendly_name": "Kitchen TRV Heat on Demand"}, t0)
    fake.set_state("sensor.processor_temperature", "48.0", sensor_attrs("temperature", C, "Processor temperature"), t0)
    fake.set_state("weather.forecast_home", "cloudy", {
        "temperature": 11.2, "temperature_unit": C, "humidity": 81, "wind_speed": 18.0,
        "wind_speed_unit": "km/h", "cloud_coverage": 75.0, "pressure": 1012, "pressure_unit": "hPa",
        "friendly_name": "Forecast Home"}, t0)
    for room, (eid, dev_id, name, model, area) in TRVS.items():
        fake.set_state(eid, "heat", climate_attrs(START_TEMPS[room] - 0.3, 20.0, "idle", name=name), t0)
        fake.set_state(f"sensor.{eid.split('.')[1]}_current_temperature", str(START_TEMPS[room] - 0.3),
                       sensor_attrs("temperature", C, f"{name} Current Temperature"), t0)
        fake.set_state(f"sensor.{eid.split('.')[1]}_battery", "80", sensor_attrs("battery", "%", f"{name} Battery"), t0)
    for room, (base, dev_id, name, dev_area, ent_area) in GOVEE.items():
        fake.set_state(f"{base}_temperature", str(START_TEMPS[room]), sensor_attrs("temperature", C, f"{name} Temperature"), t0)
        fake.set_state(f"{base}_humidity", "55.0", sensor_attrs("humidity", "%", f"{name} Humidity"), t0)
        fake.set_state(f"{base}_battery", "90", sensor_attrs("battery", "%", f"{name} Battery"), t0)


def add_history(fake: FakeHA, days: int, now: datetime | None = None) -> None:
    """Hourly history for the last `days` days, ending one hour before now."""
    now = now or datetime.now(timezone.utc)
    start = (now - timedelta(days=days)).replace(minute=0, second=0, microsecond=0)
    t = start
    i = 0
    while t < now - timedelta(hours=1):
        heating = 1 if 6 <= t.hour < 8 or 17 <= t.hour < 22 else 0
        for room, (eid, _d, name, _m, _a) in TRVS.items():
            temp = round(START_TEMPS[room] - 1 + heating * 1.5 + (i % 3) * 0.1, 1)
            fake.add_history(eid, "heat", climate_attrs(temp, 20.0 if heating else 16.0,
                                                        "heating" if heating else "idle", name=name), t)
        for room, (base, *_rest) in GOVEE.items():
            fake.add_history(f"{base}_temperature", round(START_TEMPS[room] - 1 + heating * 1.2, 1),
                             sensor_attrs("temperature", C, "t"), t)
        fake.add_history("climate.heating", "heat",
                         climate_attrs(18.5 + heating, 20.0 if heating else 16.0,
                                       "heating" if heating else "idle", name="Heating"), t)
        fake.add_history("weather.forecast_home", "cloudy", {
            "temperature": 8 + (t.hour % 12) * 0.5, "temperature_unit": C, "wind_speed": 10.8,
            "wind_speed_unit": "km/h", "cloud_coverage": 50}, t)
        t += timedelta(hours=1)
        i += 1
