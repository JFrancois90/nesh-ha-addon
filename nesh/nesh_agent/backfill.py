"""Backfill from Home Assistant's recorder history, one UTC day per request.

Idempotent in two ways: state.json records which (entity, day) pairs are done,
and before writing a day the rows already in that day's file are read so the
same (ts, room, kind, entity_id) is never written twice, even after a crash.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .contract import iso_utc, parse_time
from .ha import AuthError, HARest
from .mapping import readings_for_state
from .roommap import RoomMap
from .storage import ObsWriter, Recorder, StateStore

log = logging.getLogger("nesh.backfill")


@dataclass
class BackfillStats:
    days_requested: int = 0
    requests: int = 0
    rows_written: int = 0
    failed_days: int = 0


def day_floor(dt: datetime) -> datetime:
    dt = dt.astimezone(timezone.utc)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


async def run_backfill(rest: HARest, room_map: RoomMap, writer: ObsWriter, state: StateStore, *,
                       start: datetime, end: datetime, force: bool = False,
                       climate_unit: str | None = None, pause: float = 0.2,
                       stop: asyncio.Event | None = None) -> BackfillStats:
    stats = BackfillStats()
    roles = room_map.entity_roles()
    if not roles or end <= start:
        return stats
    bf = state.data.setdefault("backfill", {})
    done: dict[str, list[str]] = bf.setdefault("done", {})
    recorder = Recorder(writer, heartbeat_seconds=None)
    day = day_floor(start)
    log.info("Backfill from %s to %s for %d entities%s", iso_utc(start), iso_utc(end), len(roles),
             " (forced, existing rows are skipped)" if force else "")
    while day < end:
        if stop is not None and stop.is_set():
            break
        day_s = day.date().isoformat()
        chunk_start = max(day, start)
        chunk_end = min(day + timedelta(days=1), end)
        wanted = [e for e in sorted(roles) if force or day_s not in done.get(e, [])]
        day += timedelta(days=1)
        if not wanted or chunk_end <= chunk_start:
            continue
        stats.days_requested += 1
        try:
            stats.requests += 1
            history = await rest.get_history(chunk_start, chunk_end, wanted)
        except AuthError:
            raise
        except Exception as exc:
            stats.failed_days += 1
            log.warning("History for %s not available (%s); will retry on next start", day_s, exc)
            continue
        existing = writer.existing_keys(day_s)
        rows = []
        for series in history or []:
            if not isinstance(series, list) or not series:
                continue
            eid = series[0].get("entity_id")
            if eid not in roles:
                continue
            for st in series:
                if not isinstance(st, dict):
                    continue
                st = dict(st)
                st.setdefault("entity_id", eid)
                raw = st.get("last_updated") or st.get("last_changed")
                try:
                    ts_dt = parse_time(raw) if raw else chunk_start
                except ValueError:
                    continue
                if ts_dt >= chunk_end:
                    continue
                ts_dt = max(ts_dt, chunk_start)  # the start-of-period state may carry an older time
                rows.extend(readings_for_state(st, roles[eid], room_map.home_id,
                                               ts_override=iso_utc(ts_dt), climate_unit=climate_unit))
        rows.sort(key=lambda o: o.ts)
        stats.rows_written += len(recorder.observe(rows, skip_keys=existing))
        for e in wanted:
            days = done.setdefault(e, [])
            if day_s not in days:
                days.append(day_s)
        state.save()
        if pause:
            await asyncio.sleep(pause)

    # keep state.json small: forget days older than a year
    horizon = (day_floor(end) - timedelta(days=400)).date().isoformat()
    for e in list(done):
        done[e] = sorted(d for d in done[e] if d >= horizon)
    state.save()
    log.info("Backfill finished: %d days, %d requests, %d rows written, %d days failed",
             stats.days_requested, stats.requests, stats.rows_written, stats.failed_days)
    return stats
