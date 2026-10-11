"""Minimal Home Assistant client: REST and websocket, read only.

In the add-on: REST at http://supervisor/core/api, websocket at
ws://supervisor/core/websocket, token from the SUPERVISOR_TOKEN environment
variable. On a PC: http://host:8123/api and ws://host:8123/api/websocket with a
long-lived access token. The token is never logged.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote

import aiohttp

from .contract import iso_utc

log = logging.getLogger("nesh.ha")


class AuthError(Exception):
    pass


class WSClosed(Exception):
    pass


@dataclass
class Endpoints:
    rest_base: str  # ends with /api
    ws_url: str

    @classmethod
    def supervisor(cls) -> "Endpoints":
        return cls("http://supervisor/core/api", "ws://supervisor/core/websocket")

    @classmethod
    def from_ha_url(cls, url: str) -> "Endpoints":
        base = url.rstrip("/")
        if base.endswith("/api"):
            base = base[:-4]
        ws = "wss://" + base[len("https://"):] if base.startswith("https://") else \
             "ws://" + base[len("http://"):] if base.startswith("http://") else base
        return cls(base + "/api", ws + "/api/websocket")


class HARest:
    def __init__(self, session: aiohttp.ClientSession, endpoints: Endpoints, token: str):
        self._session = session
        self._ep = endpoints
        self._headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    async def _get(self, path: str, timeout: float = 30):
        url = self._ep.rest_base + path
        async with self._session.get(url, headers=self._headers,
                                     timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status == 401:
                raise AuthError("Home Assistant rejected the token (HTTP 401)")
            resp.raise_for_status()
            return await resp.json()

    async def get_config(self) -> dict:
        return await self._get("/config")

    async def get_states(self) -> list[dict]:
        return await self._get("/states")

    async def get_history(self, start: datetime, end: datetime, entity_ids: list[str]) -> list[list[dict]]:
        """History with attributes (needed for climate current_temperature,
        temperature and hvac_action). minimal_response and no_attributes are
        deliberately not sent: HA treats their mere presence as true."""
        path = (f"/history/period/{quote(iso_utc(start), safe='')}"
                f"?filter_entity_id={quote(','.join(entity_ids), safe=',')}"
                f"&end_time={quote(iso_utc(end), safe='')}"
                f"&significant_changes_only=0")
        return await self._get(path, timeout=300)


class HAWebSocket:
    def __init__(self, session: aiohttp.ClientSession, endpoints: Endpoints, token: str):
        self._session = session
        self._ep = endpoints
        self._token = token
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._reader: asyncio.Task | None = None
        self.events: asyncio.Queue = asyncio.Queue()
        self.ha_version: str | None = None

    async def connect(self, timeout: float = 20) -> None:
        self._ws = await self._session.ws_connect(self._ep.ws_url, heartbeat=30,
                                                  max_msg_size=64 * 1024 * 1024)
        msg = await asyncio.wait_for(self._ws.receive_json(), timeout)
        if msg.get("type") != "auth_required":
            raise WSClosed(f"unexpected first message {msg.get('type')!r}")
        await self._ws.send_json({"type": "auth", "access_token": self._token})
        msg = await asyncio.wait_for(self._ws.receive_json(), timeout)
        if msg.get("type") == "auth_invalid":
            raise AuthError("Home Assistant rejected the token (auth_invalid)")
        if msg.get("type") != "auth_ok":
            raise WSClosed(f"unexpected auth reply {msg.get('type')!r}")
        self.ha_version = msg.get("ha_version")
        self._reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for msg in self._ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    if msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                        break
                    continue
                try:
                    data = msg.json()
                except ValueError:
                    continue
                for item in data if isinstance(data, list) else [data]:
                    self._dispatch(item)
        except Exception as exc:
            log.debug("websocket reader stopped: %s", exc)
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(WSClosed("connection closed"))
            self._pending.clear()
            self.events.put_nowait(None)  # tells the consumer the socket is gone

    def _dispatch(self, item: dict) -> None:
        kind = item.get("type")
        if kind == "result":
            fut = self._pending.pop(item.get("id"), None)
            if fut and not fut.done():
                if item.get("success"):
                    fut.set_result(item.get("result"))
                else:
                    err = item.get("error") or {}
                    fut.set_exception(RuntimeError(f"{err.get('code')}: {err.get('message')}"))
        elif kind == "event":
            self.events.put_nowait(item.get("event"))

    async def command(self, type_: str, timeout: float = 60, **payload):
        if self._ws is None or self._ws.closed:
            raise WSClosed("not connected")
        msg_id = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        await self._ws.send_json({"id": msg_id, "type": type_, **payload})
        return await asyncio.wait_for(fut, timeout)

    async def subscribe_state_changed(self) -> None:
        await self.command("subscribe_events", event_type="state_changed")

    async def close(self) -> None:
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        if self._reader is not None:
            try:
                await asyncio.wait_for(self._reader, 5)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                self._reader.cancel()
