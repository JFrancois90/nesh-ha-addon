"""Entry point.

Add-on:   python3 -m nesh_agent            (uses SUPERVISOR_TOKEN and /data/options.json)
On a PC:  python3 -m nesh_agent --ha-url http://homeassistant.local:8123 --token <token> --data ./data
          The token can also come from the NESH_HA_TOKEN environment variable or --token-file.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

from . import __version__
from .agent import Agent, ConfigError
from .config import load_options
from .control import apply_setpoint
from .ha import Endpoints


class RedactFilter(logging.Filter):
    """Belt and braces: replace any secret that slips into a log line."""

    def __init__(self, secrets: list[str]):
        super().__init__()
        self.secrets = [s for s in secrets if s and len(s) >= 6]

    def filter(self, record: logging.LogRecord) -> bool:
        if self.secrets:
            msg = record.getMessage()
            for s in self.secrets:
                msg = msg.replace(s, "[redacted]")
            record.msg, record.args = msg, None
        return True


def setup_logging(level: str, secrets: list[str]) -> None:
    handler = logging.StreamHandler(sys.stdout)
    fmt = logging.Formatter("%(asctime)sZ %(levelname)s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    fmt.converter = time.gmtime  # log times in UTC, like the data
    handler.setFormatter(fmt)
    handler.addFilter(RedactFilter(secrets))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="nesh_agent", description="Nesh heating logger for Home Assistant")
    p.add_argument("--ha-url", help="Home Assistant URL, e.g. http://homeassistant.local:8123. "
                                    "Leave out inside the add-on.")
    p.add_argument("--token", help="Long-lived access token (or set NESH_HA_TOKEN)")
    p.add_argument("--token-file", help="File containing the token")
    p.add_argument("--data", help="Data folder (default /data in the add-on, ./data otherwise)")
    p.add_argument("--options", help="Options file, JSON or YAML (default /data/options.json in the add-on)")
    p.add_argument("--home-id", help="Overrides home_id from the options file")
    p.add_argument("--poll-seconds", type=float, help="Overrides poll_seconds")
    p.add_argument("--backfill-days", type=int, help="Overrides backfill_days")
    p.add_argument("--run-seconds", type=float, help="Stop after this many seconds (for debugging)")
    p.add_argument("--dry-run-command", help="Check a setpoint command JSON file against the control stub and exit")
    p.add_argument("--log-level", help="debug, info, warning or error")
    p.add_argument("--version", action="version", version=f"nesh_agent {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    addon_mode = not args.ha_url and bool(os.environ.get("SUPERVISOR_TOKEN"))

    if addon_mode:
        token = os.environ["SUPERVISOR_TOKEN"]
        endpoints = Endpoints.supervisor()
        data_dir = Path(args.data or "/data")
        options_path = args.options or "/data/options.json"
    else:
        token = args.token or os.environ.get("NESH_HA_TOKEN", "")
        if args.token_file:
            token = Path(args.token_file).read_text(encoding="utf-8").strip()
        if not args.ha_url or not token:
            print("Give --ha-url and a token (--token, --token-file or NESH_HA_TOKEN).", file=sys.stderr)
            return 2
        endpoints = Endpoints.from_ha_url(args.ha_url)
        data_dir = Path(args.data or "./data")
        options_path = args.options

    options = load_options(options_path)
    if args.home_id:
        options.home_id = args.home_id
    if args.poll_seconds:
        options.poll_seconds = args.poll_seconds
    if args.backfill_days is not None:
        options.backfill_days = args.backfill_days
    setup_logging(args.log_level or options.log_level, [token, options.upload_token])
    log = logging.getLogger("nesh")
    log.info("Nesh agent %s (%s mode)", __version__, "add-on" if addon_mode else "local")

    if args.dry_run_command:
        from .roommap import room_map_from_dict
        import yaml
        rm_path = data_dir / "room_map.yaml"
        rm = room_map_from_dict(yaml.safe_load(rm_path.read_text())) if rm_path.exists() else None
        cmd = json.loads(Path(args.dry_run_command).read_text(encoding="utf-8"))
        result = apply_setpoint(cmd, options, rm)
        log.info("Control stub result: %s (%s)", result.action, result.reason)
        return 0

    agent = Agent(options, endpoints, token, data_dir)

    async def runner() -> int:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):
                pass
        if args.run_seconds:
            loop.call_later(args.run_seconds, stop.set)
        try:
            await agent.run(stop)
        except ConfigError:
            return 1
        return 0

    return asyncio.run(runner())


if __name__ == "__main__":
    sys.exit(main())
