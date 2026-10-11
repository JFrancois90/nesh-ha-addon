"""Run the real agent main loop against the fake Home Assistant."""

import asyncio
import json
import logging
import threading
from collections import Counter

import aiohttp

from nesh_agent.__main__ import main as cli_main
from nesh_agent.agent import Agent
from nesh_agent.config import Options
from nesh_agent.contract import observation_from_dict, validate_observation
from nesh_agent.ha import Endpoints
from tests.conftest import read_obs
from tests.fake_ha import FakeHA
from tests.fixtures import add_history, build_house, climate_attrs


def opts(**kw):
    base = dict(home_id="myhome", poll_seconds=0.3, heartbeat_seconds=1.0, backfill_days=0)
    base.update(kw)
    return Options(**base)


async def wait_for(pred, timeout=5.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if pred():
            return True
        await asyncio.sleep(0.05)
    return False


async def run_agent(fake, data_dir, options, script, token=None, **agent_kw):
    url = await fake.start()
    agent = Agent(options, Endpoints.from_ha_url(url), token or fake.token, data_dir,
                  reconnect_min=0.1, reconnect_max=0.5, backfill_pause=0, **agent_kw)
    stop = asyncio.Event()
    async with aiohttp.ClientSession() as session:
        task = asyncio.create_task(agent.run(stop, session=session))
        try:
            await script(agent)
        finally:
            stop.set()
            await asyncio.wait_for(task, 10)
            await fake.close()
    return agent


def test_live_logging_end_to_end(tmp_path):
    fake = FakeHA()
    build_house(fake)

    async def script(agent):
        assert await wait_for(lambda: agent.discovered.is_set() and fake.subscribers)
        await fake.push_state("sensor.h5075_kitchen_temperature", "20.4")
        await fake.push_state("climate.kitchen_trv", "heat", climate_attrs(19.4, 21.0, "heating"))
        await fake.push_state("climate.heating", "heat", climate_attrs(19.1, 20.0, "heating"))
        await fake.push_state("water_heater.hot_water", "on")
        # bad entities must not stop anything
        await fake.push_state("climate.bedroom", "heat", climate_attrs("garbage", None, None))
        await fake.push_state("sensor.h5075_3c4d_humidity", "unavailable")
        await fake.push_raw_event({"event_type": "state_changed", "data": {"entity_id": "x", "new_state": None}})
        await fake.push_raw_event({"event_type": "state_changed", "data": None})
        await fake.push_state("sensor.h5075_1a2b_temperature", "21.0")  # arrives after the junk
        assert await wait_for(lambda: agent.events_seen >= 9)
        await asyncio.sleep(1.6)  # several polls, at least one heartbeat interval

    asyncio.run(run_agent(fake, tmp_path, opts(), script))
    rows = read_obs(tmp_path)
    assert rows, "no observations written"
    for r in rows:
        assert not validate_observation(observation_from_dict(r)), r
        assert r["home_id"] == "myhome"
    got = {(r["room"], r["kind"], r["value"]) for r in rows}
    assert ("kitchen", "room_temp", 19.5) in got and ("kitchen", "room_temp", 20.4) in got
    assert ("kitchen", "valve_demand", 1.0) in got and ("kitchen", "valve_temp", 19.4) in got
    assert ("hall", "heating_on", 1.0) in got and ("hall", "thermostat_temp", 19.1) in got
    assert ("house", "hot_water_on", 1.0) in got
    assert ("living", "room_temp", 21.0) in got
    assert ("office", "valve_temp", 18.6) in got and ("office", "room_rh", 55.0) in got
    assert ("outside", "wind_speed", 5.0) in got and ("outside", "outdoor_temp", 11.2) in got
    entities = {r["entity_id"] for r in rows}
    assert "sensor.processor_temperature" not in entities
    assert "sensor.heating_current_temperature" not in entities
    assert not any(r["room"] == "hall" and r["kind"].startswith("valve") for r in rows)
    assert not any(r["room"] == "hall" and r["kind"] == "room_temp" for r in rows)
    # de-dup: the unchanged bedroom sensor appears once plus heartbeats, never once per poll
    bed = [r for r in rows if r["entity_id"] == "sensor.h5075_3c4d_temperature"]
    assert 2 <= len(bed) <= 4, len(bed)
    assert fake.state_polls >= 3
    assert len(bed) < fake.state_polls + 1
    assert (tmp_path / "room_map.proposed.yaml").exists()
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["live_started_at"].endswith("Z") and state["last_live_at"].endswith("Z")


def test_reconnects_after_websocket_drop(tmp_path):
    fake = FakeHA()
    build_house(fake)

    async def script(agent):
        assert await wait_for(lambda: fake.subscribers)
        await fake.drop_connections()
        assert await wait_for(lambda: agent.ws_connections >= 2 and fake.subscribers, timeout=5)
        await fake.push_state("sensor.h5075_kitchen_temperature", "22.2")
        assert await wait_for(lambda: any(r["value"] == 22.2 for r in read_obs(tmp_path)))

    agent = asyncio.run(run_agent(fake, tmp_path, opts(poll_seconds=30), script))
    assert agent.ws_connections >= 2 and fake.ws_connections >= 2


def test_bad_token_is_refused_and_never_logged(tmp_path, caplog):
    fake = FakeHA()
    build_house(fake)
    bad = "wrong-token-zyx987654"

    async def script(agent):
        assert await wait_for(lambda: "rejected the token" in caplog.text)
        await asyncio.sleep(0.3)

    with caplog.at_level(logging.DEBUG):
        asyncio.run(run_agent(fake, tmp_path, opts(), script, token=bad))
    assert "rejected the token" in caplog.text
    assert bad not in caplog.text and fake.token not in caplog.text
    assert read_obs(tmp_path) == []


def test_backfill_is_idempotent(tmp_path):
    fake = FakeHA()
    build_house(fake)
    add_history(fake, days=3)

    async def script(agent):
        assert await wait_for(lambda: agent.backfill_task is not None and agent.backfill_task.done(), timeout=10)

    asyncio.run(run_agent(fake, tmp_path, opts(backfill_days=3, poll_seconds=30), script))
    first_requests = len(fake.history_requests)
    assert first_requests == 4  # three whole days back plus today, one request per UTC day
    for req in fake.history_requests:
        assert not req["minimal_response"] and not req["no_attributes"]
        assert req["significant_changes_only"] == "0"
        assert "climate.kitchen_trv" in req["ids"]
    rows = read_obs(tmp_path)
    back = [r for r in rows if r["entity_id"] == "climate.kitchen_trv" and r["kind"] == "valve_demand"]
    assert {r["value"] for r in back} == {0.0, 1.0}
    keys = Counter((r["ts"], r["room"], r["kind"], r["entity_id"]) for r in rows)
    assert max(keys.values()) == 1, "duplicate rows"
    state = json.loads((tmp_path / "state.json").read_text())
    assert len(state["backfill"]["done"]["climate.kitchen_trv"]) == 4
    n_backfilled = len(rows)

    # second start: nothing left to fetch
    fake2 = FakeHA()
    build_house(fake2)
    add_history(fake2, days=3)
    asyncio.run(run_agent(fake2, tmp_path, opts(backfill_days=3, poll_seconds=30), script))
    assert fake2.history_requests == []

    # forced re-read (backfill_now) fetches again but writes no duplicates
    fake3 = FakeHA()
    build_house(fake3)
    add_history(fake3, days=3)
    asyncio.run(run_agent(fake3, tmp_path, opts(backfill_days=3, poll_seconds=30, backfill_now=True), script))
    assert len(fake3.history_requests) == 4
    rows = read_obs(tmp_path)
    keys = Counter((r["ts"], r["room"], r["kind"], r["entity_id"]) for r in rows)
    assert max(keys.values()) == 1, "forced backfill wrote duplicates"
    assert len(rows) >= n_backfilled


def test_upload_retries_then_delivers(tmp_path):
    fake = FakeHA()
    build_house(fake)
    fake.upload_failures_left = 1

    async def script(agent):
        assert await wait_for(lambda: hasattr(agent, "uploader"))
        url = str(fake.server.make_url("/upload"))
        agent.options.upload_url = url
        agent.uploader.url = url
        assert await wait_for(lambda: fake.uploads, timeout=8)

    async def go():
        o = opts(upload_url="http://placeholder/upload", upload_interval_seconds=0.2,
                 upload_token="upload-secret-123456")
        return await run_agent(fake, tmp_path, o, script)

    asyncio.run(go())
    up = fake.uploads[0]
    assert up["headers"]["X-Nesh-Home-Id"] == "myhome"
    assert up["headers"]["Authorization"] == "Bearer upload-secret-123456"
    assert up["headers"]["Content-Encoding"] == "gzip"
    for line in up["lines"]:
        assert not validate_observation(observation_from_dict(json.loads(line)))


def test_cli_runs_against_fake_and_hides_token(tmp_path, capsys):
    fake = FakeHA()
    build_house(fake)
    ready = threading.Event()
    holder = {}

    def serve():
        loop = asyncio.new_event_loop()
        holder["loop"] = loop
        holder["url"] = loop.run_until_complete(fake.start())
        ready.set()
        loop.run_forever()
        loop.run_until_complete(fake.close())

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    assert ready.wait(5)
    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    try:
        code = cli_main(["--ha-url", holder["url"], "--token", fake.token, "--data", str(tmp_path),
                         "--home-id", "myhome", "--poll-seconds", "0.5", "--backfill-days", "0",
                         "--run-seconds", "2", "--log-level", "debug"])
    finally:
        root.handlers[:], _ = saved[0], root.setLevel(saved[1])
        holder["loop"].call_soon_threadsafe(holder["loop"].stop)
        t.join(5)
    out = capsys.readouterr()
    assert code == 0
    assert fake.token not in out.out and fake.token not in out.err
    assert "Room map in use" in out.out
    assert any(r["kind"] == "room_temp" for r in read_obs(tmp_path))
