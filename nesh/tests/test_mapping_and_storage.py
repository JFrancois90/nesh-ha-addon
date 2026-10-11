from nesh_agent.contract import Observation, validate_observation
from nesh_agent.mapping import readings_for_state
from nesh_agent.storage import ObsWriter, Recorder
from tests.conftest import read_obs

TS = "2026-10-11T05:40:00.123456+00:00"


def st(eid, state, attrs):
    return {"entity_id": eid, "state": state, "attributes": attrs, "last_updated": TS, "last_changed": TS}


def kinds(rows):
    return {(r.kind, r.value) for r in rows}


def test_trv_climate_maps_to_valve_kinds():
    rows = readings_for_state(st("climate.kitchen_trv", "heat", {
        "current_temperature": 19.2, "temperature": 21, "hvac_action": "heating"}),
        [("valve", "kitchen")], "myhome")
    assert kinds(rows) == {("valve_temp", 19.2), ("valve_setpoint", 21.0), ("valve_demand", 1.0)}
    assert all(r.ts == "2026-10-11T05:40:00Z" and r.source == "valve" and r.room == "kitchen" for r in rows)
    assert all(not validate_observation(r) for r in rows)


def test_thermostat_idle_and_off():
    rows = readings_for_state(st("climate.heating", "heat", {
        "current_temperature": 18.5, "temperature": 18, "hvac_action": "idle"}), [("thermostat", "hall")], "h")
    assert ("heating_on", 0.0) in kinds(rows) and ("thermostat_temp", 18.5) in kinds(rows)
    rows = readings_for_state(st("climate.heating", "off", {"current_temperature": 18.5}), [("thermostat", "hall")], "h")
    assert ("heating_on", 0.0) in kinds(rows)


def test_sensor_units_and_bad_values():
    f = readings_for_state(st("sensor.t", "68", {"unit_of_measurement": "°F"}), [("room_temp", "office")], "h")
    assert f[0].value == 20.0 and f[0].kind == "room_temp" and f[0].source == "sensor"
    assert readings_for_state(st("sensor.t", "unavailable", {}), [("room_temp", "office")], "h") == []
    assert readings_for_state(st("sensor.t", "abc", {}), [("room_temp", "office")], "h") == []
    assert readings_for_state(st("sensor.h", "140", {}), [("room_rh", "office")], "h") == []
    assert readings_for_state({"entity_id": "climate.x", "state": "heat", "attributes": None},
                              [("valve", "office")], "h") == []
    # garbage never raises
    assert readings_for_state({"entity_id": "climate.x", "state": None, "attributes": "nonsense"},
                              [("valve", "office")], "h") == []


def test_weather_wind_converted_to_ms():
    rows = readings_for_state(st("weather.home", "cloudy", {
        "temperature": 11.2, "temperature_unit": "°C", "wind_speed": 18.0,
        "wind_speed_unit": "km/h", "cloud_coverage": 75}), [("weather", "outside")], "h")
    assert kinds(rows) == {("outdoor_temp", 11.2), ("wind_speed", 5.0), ("cloud_cover", 75.0)}
    assert all(r.room == "outside" and r.source == "weather" for r in rows)


def test_hot_water_modes():
    on = readings_for_state(st("water_heater.hw", "on", {}), [("hot_water", "house")], "h")
    off = readings_for_state(st("water_heater.hw", "off", {}), [("hot_water", "house")], "h")
    eco = readings_for_state(st("water_heater.hw", "eco", {}), [("hot_water", "house")], "h")
    assert kinds(on) == {("hot_water_on", 1.0)} and kinds(off) == {("hot_water_on", 0.0)}
    assert eco == []  # schedule mode: actual heating not exposed, so nothing is guessed


def test_dedup_and_heartbeat(tmp_path):
    now = [0.0]
    writer = ObsWriter(tmp_path)
    rec = Recorder(writer, heartbeat_seconds=900, clock=lambda: now[0])

    def obs(ts, v):
        return Observation(ts, "h", "kitchen", "room_temp", v, "sensor", "sensor.k")

    assert len(rec.observe([obs("2026-10-11T00:00:00Z", 20.0)])) == 1
    now[0] = 60
    assert rec.observe([obs("2026-10-11T00:01:00Z", 20.0)], heartbeat_ts="2026-10-11T00:01:00Z") == []
    now[0] = 120
    assert len(rec.observe([obs("2026-10-11T00:02:00Z", 20.1)])) == 1   # changed
    now[0] = 120 + 899
    assert rec.observe([obs("x", 20.1)], heartbeat_ts="2026-10-11T00:16:59Z") == []
    now[0] = 120 + 900
    hb = rec.observe([obs("2026-10-11T00:02:00Z", 20.1)], heartbeat_ts="2026-10-11T00:17:00Z")
    assert [r.ts for r in hb] == ["2026-10-11T00:17:00Z"]                  # heartbeat stamped at poll time
    now[0] = 120 + 1000
    assert rec.observe([obs("2026-10-11T00:02:00Z", 20.1)]) == []        # events never heartbeat
    rows = read_obs(tmp_path)
    assert [r["value"] for r in rows] == [20.0, 20.1, 20.1]


def test_writer_splits_by_utc_day_and_drops_invalid(tmp_path):
    w = ObsWriter(tmp_path)
    w.write([Observation("2026-10-10T23:59:59Z", "h", "kitchen", "room_temp", 20.0, "sensor", "s"),
             Observation("2026-10-11T00:00:00Z", "h", "kitchen", "room_temp", 20.0, "sensor", "s"),
             Observation("2026-10-11T00:00:00Z", "h", "Kitchen!", "room_temp", 20.0, "sensor", "s"),
             Observation("2026-10-11T00:00:00Z", "h", "kitchen", "room_temp", 20.0, "valve", "s")])
    assert sorted(p.name for p in (tmp_path / "obs").iterdir()) == ["2026-10-10.ndjson", "2026-10-11.ndjson"]
    assert len(read_obs(tmp_path)) == 2


def test_restart_does_not_rewrite_unchanged_values(tmp_path):
    from datetime import datetime, timezone
    w = ObsWriter(tmp_path)
    Recorder(w).observe([Observation("2026-10-11T00:00:00Z", "h", "kitchen", "room_temp", 20.0, "sensor", "s")])
    now = [1000.0]
    rec = Recorder(ObsWriter(tmp_path), heartbeat_seconds=900, clock=lambda: now[0])
    rec.seed(ObsWriter(tmp_path).latest_rows(), datetime(2026, 10, 11, 0, 10, tzinfo=timezone.utc))
    same = Observation("2026-10-11T00:00:00Z", "h", "kitchen", "room_temp", 20.0, "sensor", "s")
    assert rec.observe([same], heartbeat_ts="2026-10-11T00:10:00Z") == []      # 10 min old: no heartbeat yet
    now[0] += 300
    assert len(rec.observe([same], heartbeat_ts="2026-10-11T00:15:00Z")) == 1  # 15 min after the last row
