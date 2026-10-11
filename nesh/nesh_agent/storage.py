"""Local storage: daily NDJSON observation files, de-duplication, and state.json."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable

from .contract import Observation, observation_from_dict, parse_time, validate_observation

if TYPE_CHECKING:
    from .upload import Spool

log = logging.getLogger("nesh.storage")


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class ObsWriter:
    """Appends Observations to <data>/obs/YYYY-MM-DD.ndjson, one file per UTC day.

    Files are opened, appended and closed on every batch, so a new day simply
    starts a new file and nothing is held open across a crash. Rows inside a
    file are in arrival order, not strictly sorted by ts.
    """

    def __init__(self, data_dir: Path, spool: "Spool | None" = None):
        self.obs_dir = Path(data_dir) / "obs"
        self.obs_dir.mkdir(parents=True, exist_ok=True)
        self.spool = spool
        self.rows_written = 0

    def day_path(self, day: str) -> Path:
        return self.obs_dir / f"{day}.ndjson"

    def write(self, observations: Iterable[Observation]) -> int:
        by_day: dict[str, list[str]] = {}
        for obs in observations:
            errors = validate_observation(obs)
            if errors:
                log.warning("Dropped invalid observation from %s: %s", obs.entity_id, "; ".join(errors))
                continue
            by_day.setdefault(obs.ts[:10], []).append(obs.to_json())
        count = 0
        for day, lines in sorted(by_day.items()):
            try:
                with open(self.day_path(day), "a", encoding="utf-8") as fh:
                    fh.write("\n".join(lines) + "\n")
                count += len(lines)
            except OSError as exc:
                log.error("Could not write %s: %s", self.day_path(day), exc)
                continue
            if self.spool is not None:
                self.spool.append(lines)
        self.rows_written += count
        return count

    def latest_rows(self, days: int = 2) -> list[Observation]:
        """Last row per series from the most recent day files."""
        files = sorted(self.obs_dir.glob("????-??-??.ndjson"))[-days:]
        latest: dict[tuple, Observation] = {}
        for path in files:
            try:
                with open(path, encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            o = observation_from_dict(json.loads(line))
                        except (ValueError, KeyError, TypeError):
                            continue
                        k = (o.room, o.kind, o.entity_id)
                        if k not in latest or o.ts >= latest[k].ts:
                            latest[k] = o
            except OSError as exc:
                log.warning("Could not read %s: %s", path, exc)
        return list(latest.values())

    def existing_keys(self, day: str) -> set[tuple]:
        keys: set[tuple] = set()
        path = self.day_path(day)
        if not path.exists():
            return keys
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                    keys.add((d["ts"], d["room"], d["kind"], d["entity_id"]))
                except (ValueError, KeyError, TypeError):
                    continue
        return keys


@dataclass
class _Last:
    value: float
    written_at: float  # monotonic seconds


class Recorder:
    """Writes a reading only when its value changed, plus a heartbeat row every
    heartbeat_seconds per series when the value has stayed the same."""

    def __init__(self, writer: ObsWriter, heartbeat_seconds: float | None = 900,
                 clock: Callable[[], float] = time.monotonic):
        self.writer = writer
        self.heartbeat_seconds = heartbeat_seconds
        self.clock = clock
        self.last: dict[tuple, _Last] = {}

    def seed(self, rows: Iterable[Observation], now_utc: datetime) -> None:
        """Prime the last-written values from rows already on disk, keeping their
        age so heartbeats stay on schedule after a restart."""
        mono = self.clock()
        for r in rows:
            try:
                age = max(0.0, (now_utc - parse_time(r.ts)).total_seconds())
            except ValueError:
                continue
            self.last[self.series(r)] = _Last(round(r.value, 3), mono - age)

    @staticmethod
    def series(obs: Observation) -> tuple:
        return (obs.room, obs.kind, obs.entity_id)

    def observe(self, readings: Iterable[Observation], heartbeat_ts: str | None = None,
                skip_keys: set[tuple] | None = None) -> list[Observation]:
        """Record readings. heartbeat_ts is set by the poller: an unchanged value
        that has not been written for heartbeat_seconds is written again at that ts."""
        now = self.clock()
        out: list[Observation] = []
        for r in readings:
            key = self.series(r)
            value = round(r.value, 3)
            prev = self.last.get(key)
            if prev is None or prev.value != value:
                row = r
            elif (heartbeat_ts is not None and self.heartbeat_seconds is not None
                  and now - prev.written_at >= self.heartbeat_seconds):
                row = Observation(heartbeat_ts, r.home_id, r.room, r.kind, r.value, r.source, r.entity_id)
            else:
                continue
            self.last[key] = _Last(value, now)
            if skip_keys is not None and row.key() in skip_keys:
                continue
            out.append(row)
        if out:
            self.writer.write(out)
        return out


class StateStore:
    """Small JSON document at <data>/state.json, written atomically."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                log.warning("state.json unreadable (%s); starting fresh", exc)
                self.data = {}
        self.data.setdefault("version", 1)

    def save(self) -> None:
        try:
            atomic_write_text(self.path, json.dumps(self.data, indent=2, sort_keys=True))
        except OSError as exc:
            log.error("Could not save state.json: %s", exc)
