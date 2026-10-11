# Nesh add-on for Home Assistant

Nesh learns how a home heats and cools, so it can plan heating around comfort times and the weather.
This repository holds the Home Assistant add-on that reads the heating system and room sensors.

## Install

1. In Home Assistant, go to **Settings, Add-ons, Add-on Store**.
2. Open the three dots menu (top right), choose **Repositories**.
3. Paste `https://github.com/JFrancois90/nesh-ha-addon` and click **Add**, then close.
4. Find **Nesh** in the store, open it, click **Install**. It builds on your box; this takes a few minutes.
5. In the **Configuration** tab set **Home ID** (a short name, for example `myhome`) and click **Save**.
6. Click **Start**. Turn on **Start on boot** and **Watchdog**.
7. Open the **Log** tab to see the rooms it found.

## What it does

* Reads your thermostat, radiator valves, hot water, room temperature and humidity sensors, and weather.
* Saves readings on the Home Assistant box, in the add-on's own data folder.
* Sends nothing anywhere unless you set an upload address.
* Does not change your heating. Control is off.

See [nesh/README.md](nesh/README.md) and [nesh/DOCS.md](nesh/DOCS.md) for detail.
