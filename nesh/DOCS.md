# Nesh add-on

Records your heating system's behaviour so Nesh can learn how your home heats
and cools. Read only: it never changes your heating.

## Settings

| Setting | What it does | Default |
|---|---|---|
| Home ID | Short name for this home, lower case, for example `myhome`. Required. | empty |
| Room map corrections | Fix any room Nesh guessed wrongly. See below. | empty |
| Thermostat, hot water, weather entity | Only if Nesh picked the wrong one. | empty |
| Safety poll interval | Extra check every so many seconds in case a live update was missed. | 60 |
| Days of history to copy | How far back to copy from Home Assistant history on first start. | 30 |
| Copy history again now | Turn on and restart to re-read history. Safe to repeat. Turn off afterwards. | off |
| Upload address | Where to send readings. Leave empty to keep everything on this machine. | empty |
| Upload key | Sent with uploads if the address needs one. | empty |
| Allow heating control | Off. Version 0.1.0 never sends commands; at most it logs what it would do. | off |
| Dry run only | Keep on. | on |
| Safety floor | Nesh will never ask for a setpoint below this, in degrees C. | 14 |
| Log detail | How much to write to the log. | info |

## Files it writes

All in the add-on's private data folder:

* `obs/YYYY-MM-DD.ndjson`: the readings, one file per day in UTC, one reading
  per line.
* `room_map.proposed.yaml`: what discovery found, with notes. Rewritten on
  each start.
* `room_map.yaml`: the room map actually in use, after your corrections.
* `state.json`: what history has been copied and when the add-on last ran.
* `upload_queue/`: only when an upload address is set; batches waiting to send.

## Each reading

```json
{"ts":"2026-10-11T05:40:00Z","home_id":"myhome","room":"kitchen","kind":"valve_demand","value":1.0,"source":"valve","entity_id":"climate.kitchen_trv"}
```

Kinds recorded: `room_temp`, `room_rh` (room thermometers); `valve_temp`,
`valve_setpoint`, `valve_demand` (radiator valves); `thermostat_temp`,
`thermostat_setpoint`, `heating_on`, `hot_water_on` (thermostat);
`outdoor_temp`, `wind_speed` (m/s), `cloud_cover` (weather).

A reading is written when its value changes. If a value stays the same, it is
written again every 15 minutes so it is clear the sensor is still alive.

## Room map corrections

Use **Edit in YAML** on the Configuration tab:

```yaml
room_map:
  - room: living
    valve: climate.living_room
    sensor: sensor.h5075_1a2b_temperature
    humidity: sensor.h5075_1a2b_humidity
    floor: 0
```

Room names must be lower case letters, digits and underscores. A room with two
radiator valves can list both, separated by a comma:
`valve: climate.living_front, climate.living_back`.

## Hot water note

When Hive hot water is on its schedule, Home Assistant shows the mode as
`eco` but not whether water is actually heating at that moment. In that mode
Nesh records nothing for hot water rather than guess. When hot water is
switched fully on or off, that is recorded.
