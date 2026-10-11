"""The Nesh agent: discovery, live logging, backfill and optional upload."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import timedelta
from pathlib import Path

import aiohttp

from .backfill import day_floor, run_backfill
from .config import Options
from .contract import iso_utc, parse_time, utcnow
from .discovery import Registries, propose_room_map
from .ha import AuthError, Endpoints, HARest, HAWebSocket
from .mapping import readings_for_state
from .roommap import RoomMap, apply_overrides
from .storage import ObsWriter, Recorder, StateStore, atomic_write_text
from .upload import Spool, Uploader

log = logging.getLogger("nesh.agent")

REGISTRY_COMMANDS = {
    "entities": "config/entity_registry/list",
    "devices": "config/device_registry/list",
    "areas": "config/area_registry/list",
}


class ConfigError(Exception):
    pass


class Agent:
    def __init__(self, options: Options, endpoints: Endpoints, token: str, data_dir: str | Path,
                 *, reconnect_min: float = 1.0, reconnect_max: float = 60.0, backfill_pause: float = 0.2):
        self.options = options
        self.endpoints = endpoints
        self._token = token
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.reconnect_min = reconnect_min
        self.reconnect_max = reconnect_max
        self.backfill_pause = backfill_pause

        self.state = StateStore(self.data_dir / "state.json")
        self.spool = Spool(self.data_dir) if options.upload_url else None
        self.writer = ObsWriter(self.data_dir, spool=self.spool)
        self.recorder = Recorder(self.writer, heartbeat_seconds=options.heartbeat_seconds)
        # carry de-duplication across restarts: an unchanged value is not written again
        self.recorder.seed(self.writer.latest_rows(days=2), utcnow())

        self.room_map: RoomMap | None = None
        self.roles: dict[str, list[tuple[str, str]]] = {}
        self.climate_unit: str | None = None
        self.discovered = asyncio.Event()
        self.ws_connections = 0
        self.events_seen = 0
        self.backfill_task: asyncio.Task | None = None
        self._last_state_save = 0.0
        self._last_room_yaml = ""

    # ------------------------------------------------------------ lifecycle

    async def run(self, stop: asyncio.Event, session: aiohttp.ClientSession | None = None) -> None:
        problems = self.options.problems()
        if problems:
            for p in problems:
                log.error(p)
            raise ConfigError("; ".join(problems))
        own_session = session is None
        if own_session:
            session = aiohttp.ClientSession()
        self.rest = HARest(session, self.endpoints, self._token)

        now = utcnow()
        prev_live = self.state.data.get("last_live_at")
        self.state.data.setdefault("first_started_at", iso_utc(now))
        self.state.data.setdefault("live_started_at", iso_utc(now))
        self._gap_start = parse_time(prev_live) if prev_live else None
        self.state.data["last_started_at"] = iso_utc(now)
        self.state.save()
        log.info("Nesh agent starting for home %r. Data folder: %s. Control: %s.", self.options.home_id,
                 self.data_dir, "enabled (dry run)" if self.options.control_enabled and self.options.dry_run
                 else "enabled" if self.options.control_enabled else "off")

        tasks = [asyncio.create_task(self._ws_loop(session, stop), name="ws"),
                 asyncio.create_task(self._poll_loop(stop), name="poll")]
        if self.spool is not None:
            uploader = Uploader(self.spool, self.options.upload_url, self.options.home_id,
                                token=self.options.upload_token,
                                interval=self.options.upload_interval_seconds)
            self.uploader = uploader
            tasks.append(asyncio.create_task(uploader.run(session, stop), name="upload"))
            log.info("Upload is on: batches go to the configured upload_url.")
        else:
            log.info("Upload is off: data stays in %s only.", self.data_dir / "obs")
        try:
            await stop.wait()
        finally:
            if self.backfill_task is not None:
                self.backfill_task.cancel()
            for t in tasks:
                if t.get_name() != "upload":
                    t.cancel()
            # let the uploader seal its spool cleanly
            await asyncio.gather(*tasks, *( [self.backfill_task] if self.backfill_task else []),
                                 return_exceptions=True)
            self.state.data["last_live_at"] = iso_utc(utcnow())
            self.state.save()
            if own_session:
                await session.close()
            log.info("Nesh agent stopped. %d rows written this run.", self.writer.rows_written)

    # ------------------------------------------------------------ discovery

    async def _fetch_registries(self, ws: HAWebSocket) -> Registries:
        states = await ws.command("get_states")
        out = {}
        for key, cmd in REGISTRY_COMMANDS.items():
            try:
                out[key] = await ws.command(cmd) or []
            except RuntimeError as exc:  # HA answered with an error: carry on with names only
                log.warning("Could not read %s (%s); rooms will be matched by name only", cmd, exc)
                out[key] = []
        try:
            floors = await ws.command("config/floor_registry/list") or []
        except Exception:
            floors = []  # older Home Assistant without floors
        return Registries(states=states or [], entities=out["entities"], devices=out["devices"],
                          areas=out["areas"], floors=floors)

    def apply_discovery(self, reg: Registries) -> None:
        disc = propose_room_map(self.options.home_id, reg)
        try:
            atomic_write_text(self.data_dir / "room_map.proposed.yaml", disc.proposal_yaml())
        except OSError as exc:
            log.error("Could not write room_map.proposed.yaml: %s", exc)
        effective = apply_overrides(disc.room_map, self.options.room_map,
                                    thermostat=self.options.thermostat_entity,
                                    hot_water=self.options.hot_water_entity,
                                    weather=self.options.weather_entity)
        text = effective.to_yaml()
        try:
            atomic_write_text(self.data_dir / "room_map.yaml", text)
        except OSError as exc:
            log.error("Could not write room_map.yaml: %s", exc)
        if text != self._last_room_yaml:
            log.info("Room map in use (proposal saved to %s):\n%s",
                     self.data_dir / "room_map.proposed.yaml", text.rstrip())
            for n in disc.notes:
                log.info("Discovery note: %s", n)
            for u in disc.unassigned:
                log.info("Not used: %s", u)
            self._last_room_yaml = text
        self.room_map = effective
        self.roles = effective.entity_roles()
        self.discovered.set()

    # ------------------------------------------------------------ live

    def ingest_state(self, state: dict | None, heartbeat_ts: str | None = None) -> None:
        if not state:
            return
        roles = self.roles.get(state.get("entity_id", ""))
        if not roles:
            return
        readings = readings_for_state(state, roles, self.options.home_id, climate_unit=self.climate_unit)
        try:
            self.recorder.observe(readings, heartbeat_ts=heartbeat_ts)
        except Exception as exc:
            log.warning("Could not record %s: %s", state.get("entity_id"), exc)

    async def _ws_loop(self, session: aiohttp.ClientSession, stop: asyncio.Event) -> None:
        delay = self.reconnect_min
        while not stop.is_set():
            ws = HAWebSocket(session, self.endpoints, self._token)
            try:
                if self.climate_unit is None:
                    try:
                        cfg = await self.rest.get_config()
                        self.climate_unit = ((cfg.get("unit_system") or {}).get("temperature")) or None
                    except AuthError:
                        raise
                    except Exception:
                        pass
                await ws.connect()
                self.ws_connections += 1
                log.info("Connected to Home Assistant %s", ws.ha_version or "")
                reg = await self._fetch_registries(ws)
                self.apply_discovery(reg)
                await ws.subscribe_state_changed()
                for st in reg.states:
                    self.ingest_state(st)
                self._maybe_start_backfill(stop)
                delay = self.reconnect_min
                discovered_at = time.monotonic()
                while not stop.is_set():
                    try:
                        event = await asyncio.wait_for(ws.events.get(), timeout=30)
                    except asyncio.TimeoutError:
                        if time.monotonic() - discovered_at > self.options.rediscover_seconds:
                            self.apply_discovery(await self._fetch_registries(ws))
                            discovered_at = time.monotonic()
                        continue
                    if event is None:
                        log.warning("Home Assistant websocket closed; will reconnect")
                        break
                    if event.get("event_type") == "state_changed":
                        self.events_seen += 1
                        self.ingest_state((event.get("data") or {}).get("new_state"))
            except asyncio.CancelledError:
                await ws.close()
                raise
            except AuthError as exc:
                log.error("%s. Check the token; will keep retrying.", exc)
                delay = max(delay, self.reconnect_max)
            except Exception as exc:
                log.warning("Home Assistant connection problem: %s: %s", type(exc).__name__, exc)
            await ws.close()
            if stop.is_set():
                break
            wait = delay * random.uniform(0.8, 1.2)
            try:
                await asyncio.wait_for(stop.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, self.reconnect_max)

    async def _poll_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set() and not self.discovered.is_set():
            try:
                await asyncio.wait_for(self.discovered.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.options.poll_seconds)
                break
            except asyncio.TimeoutError:
                pass
            try:
                states = await self.rest.get_states()
                hb_ts = iso_utc(utcnow())
                for st in states:
                    if st.get("entity_id") in self.roles:
                        self.ingest_state(st, heartbeat_ts=hb_ts)
                self._touch_live()
            except asyncio.CancelledError:
                raise
            except AuthError as exc:
                log.error("Poll failed: %s", exc)
            except Exception as exc:
                log.warning("Poll failed: %s: %s", type(exc).__name__, exc)

    def _touch_live(self) -> None:
        self.state.data["last_live_at"] = iso_utc(utcnow())
        if time.monotonic() - self._last_state_save > 300:
            self.state.save()
            self._last_state_save = time.monotonic()

    # ------------------------------------------------------------ backfill

    def _maybe_start_backfill(self, stop: asyncio.Event) -> None:
        if self.backfill_task is not None:
            return
        self.backfill_task = asyncio.create_task(self._backfill_all(stop), name="backfill")

    async def _backfill_all(self, stop: asyncio.Event) -> None:
        opts = self.options
        try:
            now = utcnow()
            live_start = parse_time(self.state.data["live_started_at"])
            jobs = []
            if opts.backfill_days > 0:
                start = day_floor(now - timedelta(days=opts.backfill_days))
                if opts.backfill_now:
                    log.info("backfill_now is on: re-reading %d days of history. Turn it off again after this run.",
                             opts.backfill_days)
                    jobs.append((start, now, True))
                else:
                    jobs.append((start, live_start, False))
            gap = self._gap_start
            if gap is not None and (now - gap).total_seconds() > max(3 * opts.poll_seconds, 300):
                log.info("The agent was not running from %s; filling that gap from history.", iso_utc(gap))
                jobs.append((gap - timedelta(minutes=1), now, True))
            for start, end, force in jobs:
                if stop.is_set():
                    return
                await run_backfill(self.rest, self.room_map, self.writer, self.state, start=start, end=end,
                                   force=force, climate_unit=self.climate_unit, pause=self.backfill_pause,
                                   stop=stop)
            self.state.data.setdefault("backfill", {})["last_run_at"] = iso_utc(utcnow())
            self.state.save()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Backfill stopped early (%s: %s); it will resume on next start.", type(exc).__name__, exc)
