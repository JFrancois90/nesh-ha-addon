"""Options handling. In the add-on, Supervisor writes /data/options.json."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .contract import SLUG_RE

log = logging.getLogger("nesh.config")


@dataclass
class Options:
    home_id: str = ""
    room_map: list[dict] = field(default_factory=list)
    thermostat_entity: str = ""
    hot_water_entity: str = ""
    weather_entity: str = ""
    poll_seconds: float = 60
    backfill_days: int = 30
    backfill_now: bool = False
    upload_url: str = ""
    upload_token: str = ""
    upload_interval_seconds: float = 300
    control_enabled: bool = False
    dry_run: bool = True
    safety_floor: float = 14.0
    heartbeat_seconds: float = 900
    rediscover_seconds: float = 6 * 3600
    log_level: str = "info"

    def problems(self) -> list[str]:
        out = []
        if not self.home_id:
            out.append("home_id is not set. Open the add-on Configuration tab and enter a short name such as myhome.")
        elif not SLUG_RE.match(self.home_id):
            out.append(f"home_id {self.home_id!r} must use only lower case letters, digits and underscores.")
        if self.poll_seconds <= 0:
            out.append("poll_seconds must be above zero.")
        if self.backfill_days < 0:
            out.append("backfill_days cannot be negative.")
        return out


_BOOL = {"control_enabled", "dry_run", "backfill_now"}
_FLOAT = {"poll_seconds", "safety_floor", "heartbeat_seconds", "rediscover_seconds", "upload_interval_seconds"}
_INT = {"backfill_days"}
_STR = {"home_id", "thermostat_entity", "hot_water_entity", "weather_entity", "upload_url", "upload_token", "log_level"}


def options_from_dict(raw: dict | None) -> Options:
    opts = Options()
    for key, value in (raw or {}).items():
        if value is None:
            continue
        try:
            if key in _BOOL:
                setattr(opts, key, bool(value))
            elif key in _FLOAT:
                setattr(opts, key, float(value))
            elif key in _INT:
                setattr(opts, key, int(value))
            elif key in _STR:
                setattr(opts, key, str(value).strip())
            elif key == "room_map":
                if not isinstance(value, list):
                    log.warning("room_map option ignored: expected a list")
                    continue
                opts.room_map = [dict(item) for item in value if isinstance(item, dict)]
            else:
                log.warning("Unknown option %r ignored", key)
        except (TypeError, ValueError):
            log.warning("Option %r has an invalid value and was ignored", key)
    return opts


def load_options(path: str | Path | None) -> Options:
    if not path:
        return Options()
    p = Path(path)
    if not p.exists():
        log.warning("Options file %s not found, using defaults", p)
        return Options()
    text = p.read_text(encoding="utf-8")
    raw = json.loads(text) if p.suffix == ".json" else yaml.safe_load(text)
    return options_from_dict(raw or {})
