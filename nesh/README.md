# Nesh for Home Assistant

Nesh learns how your home heats up and cools down, room by room, so it can
later work out the least heating needed to have each room warm when you want
it. This add-on is the first step. It quietly records what your heating is
doing. It does not change anything.

## What it reads

* Your Hive thermostat: its temperature, its target, and whether it is calling
  for heat.
* Each Hive radiator valve (TRV): its temperature, its target, and whether it
  is asking for heat.
* Hot water: whether it is on or off.
* Room thermometers, such as Govee Bluetooth sensors: temperature and humidity.
* The weather entity in Home Assistant (met.no by default): outdoor
  temperature, wind and cloud.

It works out which device is in which room from the Areas you have set in Home
Assistant. Where a device has no Area it tries the device name. It writes its
best guess to the log and to a file so you can check it.

## Where the data goes

Everything is saved on your Home Assistant machine, in the add-on's own data
folder, as one file per day (`obs/2026-10-11.ndjson` and so on). Each line is
one reading.

Nothing leaves your machine unless you fill in **Upload address**. If you do,
the add-on sends its readings to that address and nowhere else. It only sends;
it never opens a port or listens for connections.

## Control is off

This version cannot change your heating. The control settings are there for
later versions. Even if you turn on **Allow heating control**, version 0.1.0
only writes to the log what it would have done.

## How to install

1. Put this repository on GitHub. A public repository is simplest; a private
   one needs a GitHub access token inside the address you paste in step 3.
2. In Home Assistant open **Settings**, then **Add-ons**, then **Add-on Store**.
3. Click the three dots menu at the top right, choose **Repositories**, paste
   the GitHub address of the repository, and click **Add**. Close the box.
4. Scroll down to find **Nesh** and open it. Click **Install**. Home Assistant
   builds it on your machine, which can take a few minutes the first time.
5. Open the **Configuration** tab. Set **Home ID** to a short name in lower
   case, for example `myhome`. Click **Save**.
6. Go back to the **Info** tab and click **Start**. Turning on **Start on boot**
   and **Watchdog** is a good idea.
7. Open the **Log** tab. After a few seconds you will see the room map it
   found. Check that each radiator valve and thermometer is in the right room.

## If a room is wrong

The simplest fix is in Home Assistant itself: give the device the right Area
(Settings, Devices and services, Devices, pick the device, then the pencil
icon). Restart the add-on and it will pick up the change.

Or tell Nesh directly. In the **Configuration** tab, open the three dots menu,
choose **Edit in YAML**, and add entries like this:

```yaml
room_map:
  - room: office
    valve: climate.office_trv
    sensor: sensor.h5075_5e6f_temperature
    humidity: sensor.h5075_5e6f_humidity
    floor: 1
```

A room listed here replaces whatever Nesh guessed for that room.

## History

On first start Nesh copies the last 30 days of readings from Home Assistant's
own history, one day at a time, so learning can start straight away. It keeps a
note of what it has copied, so restarting does not copy anything twice. If the
add-on was stopped for a while, it fills the gap on the next start.

Home Assistant normally keeps only 10 days of history unless you have changed
the recorder settings, so you may get fewer than 30 days.

## Running it on a PC (for debugging)

You need Python 3.12 or newer and a long-lived access token from your Home
Assistant profile page (Security tab).

```
pip install -r requirements.txt
export NESH_HA_TOKEN=your-token
python -m nesh_agent --ha-url http://homeassistant.local:8123 --data ./data --home-id myhome
```

The token is never printed. Press Ctrl+C to stop.
