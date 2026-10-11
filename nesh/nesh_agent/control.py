"""Control stub. Version 0.1.0 never changes anything in Home Assistant.

apply_setpoint() validates a command against the contract and decides what
would happen. It refuses when control is disabled (the default), refuses any
setpoint below the safety floor, and when dry_run is on it only logs the action
it would have taken. Live writes are not implemented in this version, so with
dry_run off it refuses as well. Nothing is scheduled.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import Options
from .contract import SetpointCommand, parse_command
from .roommap import RoomMap

log = logging.getLogger("nesh.control")


@dataclass(frozen=True)
class ControlResult:
    accepted: bool        # True only for a dry run that passed every check
    action: str           # "refused" or "dry_run"
    reason: str
    command: SetpointCommand | None = None


def _refuse(reason: str, cmd: SetpointCommand | None = None) -> ControlResult:
    log.info("Setpoint command refused: %s", reason)
    return ControlResult(False, "refused", reason, cmd)


def apply_setpoint(command: object, options: Options, room_map: RoomMap | None) -> ControlResult:
    cmd, errors = parse_command(command)
    if cmd is None:
        return _refuse("invalid command: " + "; ".join(errors))
    if not options.control_enabled:
        return _refuse("control is disabled (control_enabled is false)", cmd)
    if cmd.setpoint < options.safety_floor:
        return _refuse(f"setpoint {cmd.setpoint:.1f} C is below the safety floor of "
                       f"{options.safety_floor:.1f} C", cmd)
    if room_map is None:
        return _refuse("no room map yet", cmd)
    allowed = {e: room for e, roles in room_map.entity_roles().items()
               for role, room in roles if role in ("valve", "thermostat")}
    if cmd.entity_id not in allowed:
        return _refuse(f"{cmd.entity_id} is not a valve or thermostat in the room map", cmd)
    if allowed[cmd.entity_id] != cmd.room:
        return _refuse(f"{cmd.entity_id} belongs to room {allowed[cmd.entity_id]!r}, not {cmd.room!r}", cmd)
    if not options.dry_run:
        return _refuse("live control is not implemented in version 0.1.0; only dry runs are allowed", cmd)
    log.info("DRY RUN: would set %s (%s) to %.1f C at %s. Reason: %s. Plan %s",
             cmd.entity_id, cmd.room, cmd.setpoint, cmd.at, cmd.reason, cmd.plan_id)
    return ControlResult(True, "dry_run", "dry run only; nothing was sent to Home Assistant", cmd)
