import logging

from nesh_agent.config import Options
from nesh_agent.control import apply_setpoint
from nesh_agent.roommap import RoomEntry, RoomMap

RM = RoomMap(home_id="h", rooms={"kitchen": RoomEntry(valves=["climate.kitchen_trv"]),
                                 "hall": RoomEntry()},
             thermostat="climate.heating", thermostat_room="hall")


def cmd(**kw):
    base = {"at": "2026-10-12T06:00:00Z", "room": "kitchen", "entity_id": "climate.kitchen_trv",
            "setpoint": 20.5, "reason": "preheat for 07:00 comfort window", "plan_id": "p1"}
    base.update(kw)
    return base


def test_refuses_when_control_disabled_by_default():
    r = apply_setpoint(cmd(), Options(home_id="h"), RM)
    assert not r.accepted and r.action == "refused" and "disabled" in r.reason


def test_refuses_below_safety_floor():
    opts = Options(home_id="h", control_enabled=True, dry_run=True, safety_floor=14)
    r = apply_setpoint(cmd(setpoint=12.0), opts, RM)
    assert not r.accepted and "safety floor" in r.reason
    assert apply_setpoint(cmd(setpoint=14.0), opts, RM).accepted


def test_refuses_invalid_commands():
    opts = Options(home_id="h", control_enabled=True)
    for bad in (cmd(at="tomorrow"), cmd(entity_id="switch.x"), cmd(setpoint="hot"), cmd(room="Kitchen"),
                {"room": "kitchen"}, "not a dict", cmd(reason="two\nlines")):
        r = apply_setpoint(bad, opts, RM)
        assert not r.accepted and r.reason.startswith("invalid command")


def test_refuses_entities_outside_room_map_or_wrong_room():
    opts = Options(home_id="h", control_enabled=True)
    assert "not a valve or thermostat" in apply_setpoint(cmd(entity_id="climate.unknown"), opts, RM).reason
    assert "belongs to room" in apply_setpoint(cmd(room="hall"), opts, RM).reason
    assert apply_setpoint(cmd(room="hall", entity_id="climate.heating", setpoint=18), opts, RM).accepted


def test_dry_run_only_logs(caplog):
    opts = Options(home_id="h", control_enabled=True, dry_run=True)
    with caplog.at_level(logging.INFO):
        r = apply_setpoint(cmd(), opts, RM)
    assert r.accepted and r.action == "dry_run"
    assert "DRY RUN: would set climate.kitchen_trv" in caplog.text


def test_live_writes_not_implemented():
    opts = Options(home_id="h", control_enabled=True, dry_run=False)
    r = apply_setpoint(cmd(), opts, RM)
    assert not r.accepted and "not implemented" in r.reason
