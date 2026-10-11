# Changelog

## 0.1.0

First version.

* Finds the Hive thermostat, Hive radiator valves, hot water, room
  thermometers (such as Govee) and the weather entity, and proposes a room map
  from Home Assistant Areas, with corrections possible in the settings.
* Records live changes as they happen, with a safety poll, and writes one file
  of readings per day.
* Copies past readings from Home Assistant history on first start, and fills
  gaps after downtime, without writing anything twice.
* Optional upload of readings to an address you choose. Off by default.
* Control is a stub only: it checks commands and refuses them, or logs them in
  dry run. Nothing is ever sent to the heating.
