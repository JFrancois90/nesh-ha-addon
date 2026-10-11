"""A fake Home Assistant: just enough websocket and REST to exercise the agent."""

from __future__ import annotations

import copy
import gzip
from datetime import datetime, timezone
from urllib.parse import unquote

from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer

from nesh_agent.contract import iso_utc, parse_time


def _iso_full(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class FakeHA:
    def __init__(self, token: str = "fake-token-abcdef123456"):
        self.token = token
        self.states: dict[str, dict] = {}
        self.entities: list[dict] = []
        self.devices: list[dict] = []
        self.areas: list[dict] = []
        self.floors: list[dict] = []
        self.history: dict[str, list[dict]] = {}
        self.ws_clients: list[web.WebSocketResponse] = []
        self.subscribers: set[web.WebSocketResponse] = set()
        self.ws_connections = 0
        self.auth_failures = 0
        self.history_requests: list[dict] = []
        self.uploads: list[dict] = []
        self.upload_failures_left = 0
        self.state_polls = 0
        self.app = web.Application()
        self.app.router.add_get("/api/websocket", self._ws)
        self.app.router.add_get("/api/states", self._states)
        self.app.router.add_get("/api/config", self._config)
        self.app.router.add_get("/api/history/period/{start}", self._history)
        self.app.router.add_post("/upload", self._upload)
        self.server: TestServer | None = None

    # ------------------------------------------------------------ server

    async def start(self) -> str:
        self.server = TestServer(self.app)
        await self.server.start_server()
        return str(self.server.make_url("")).rstrip("/")

    async def close(self) -> None:
        await self.drop_connections()
        if self.server:
            await self.server.close()

    def _authorised(self, request: web.Request) -> bool:
        return request.headers.get("Authorization") == f"Bearer {self.token}"

    # ------------------------------------------------------------ state helpers

    def set_state(self, entity_id: str, state, attributes: dict | None = None,
                  when: datetime | None = None) -> dict:
        when = when or datetime.now(timezone.utc)
        old = self.states.get(entity_id)
        attrs = copy.deepcopy(attributes if attributes is not None else (old or {}).get("attributes", {}))
        new = {
            "entity_id": entity_id,
            "state": str(state),
            "attributes": attrs,
            "last_changed": _iso_full(when) if not old or old["state"] != str(state) else old["last_changed"],
            "last_updated": _iso_full(when),
            "context": {"id": "x", "parent_id": None, "user_id": None},
        }
        self.states[entity_id] = new
        return new

    async def push_state(self, entity_id: str, state, attributes: dict | None = None,
                         when: datetime | None = None) -> None:
        old = copy.deepcopy(self.states.get(entity_id))
        new = self.set_state(entity_id, state, attributes, when)
        msg = {"type": "event", "event": {
            "event_type": "state_changed",
            "data": {"entity_id": entity_id, "old_state": old, "new_state": copy.deepcopy(new)},
            "origin": "LOCAL", "time_fired": new["last_updated"]}}
        for ws in list(self.subscribers):
            sub_id = getattr(ws, "_nesh_sub_id", None)
            if sub_id is None or ws.closed:
                continue
            await ws.send_json({**msg, "id": sub_id})

    async def push_raw_event(self, payload: dict) -> None:
        for ws in list(self.subscribers):
            if not ws.closed:
                await ws.send_json({"id": getattr(ws, "_nesh_sub_id", 0), "type": "event", "event": payload})

    def add_history(self, entity_id: str, state, attributes: dict, when: datetime) -> None:
        self.history.setdefault(entity_id, []).append({
            "entity_id": entity_id, "state": str(state), "attributes": copy.deepcopy(attributes),
            "last_changed": _iso_full(when), "last_updated": _iso_full(when)})

    async def drop_connections(self) -> None:
        for ws in list(self.ws_clients):
            if not ws.closed:
                await ws.close()
        self.ws_clients.clear()
        self.subscribers.clear()

    # ------------------------------------------------------------ websocket

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required", "ha_version": "2026.10.0"})
        msg = await ws.receive()
        if msg.type != WSMsgType.TEXT:
            return ws
        data = msg.json()
        if data.get("type") != "auth" or data.get("access_token") != self.token:
            self.auth_failures += 1
            await ws.send_json({"type": "auth_invalid", "message": "Invalid access token or password"})
            await ws.close()
            return ws
        await ws.send_json({"type": "auth_ok", "ha_version": "2026.10.0"})
        self.ws_connections += 1
        self.ws_clients.append(ws)
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            req = msg.json()
            mid, typ = req.get("id"), req.get("type")
            result = None
            if typ == "get_states":
                result = list(copy.deepcopy(list(self.states.values())))
            elif typ == "subscribe_events":
                ws._nesh_sub_id = mid
                self.subscribers.add(ws)
            elif typ == "config/entity_registry/list":
                result = copy.deepcopy(self.entities)
            elif typ == "config/device_registry/list":
                result = copy.deepcopy(self.devices)
            elif typ == "config/area_registry/list" and self.areas is not None:
                result = copy.deepcopy(self.areas)
            elif typ == "config/floor_registry/list":
                result = copy.deepcopy(self.floors)
            else:
                await ws.send_json({"id": mid, "type": "result", "success": False,
                                    "error": {"code": "unknown_command", "message": "Unknown command."}})
                continue
            await ws.send_json({"id": mid, "type": "result", "success": True, "result": result})
        self.subscribers.discard(ws)
        return ws

    # ------------------------------------------------------------ REST

    async def _states(self, request: web.Request) -> web.Response:
        if not self._authorised(request):
            return web.json_response({"message": "Unauthorized"}, status=401)
        self.state_polls += 1
        return web.json_response(list(self.states.values()))

    async def _config(self, request: web.Request) -> web.Response:
        if not self._authorised(request):
            return web.json_response({"message": "Unauthorized"}, status=401)
        return web.json_response({"unit_system": {"temperature": "°C"}, "version": "2026.10.0"})

    async def _history(self, request: web.Request) -> web.Response:
        if not self._authorised(request):
            return web.json_response({"message": "Unauthorized"}, status=401)
        start = parse_time(unquote(request.match_info["start"]))
        end = parse_time(request.query["end_time"])
        ids = request.query["filter_entity_id"].split(",")
        self.history_requests.append({
            "start": iso_utc(start), "end": iso_utc(end), "ids": ids,
            "minimal_response": "minimal_response" in request.query,
            "no_attributes": "no_attributes" in request.query,
            "significant_changes_only": request.query.get("significant_changes_only"),
        })
        out = []
        for eid in ids:
            rows = sorted(self.history.get(eid, []), key=lambda r: r["last_updated"])
            before = [r for r in rows if parse_time(r["last_updated"]) < start]
            inside = [r for r in rows if start <= parse_time(r["last_updated"]) < end]
            series = ([before[-1]] if before else []) + inside  # HA includes the state at start time
            if series:
                out.append(series)
        return web.json_response(out)

    async def _upload(self, request: web.Request) -> web.Response:
        body = await request.read()
        if self.upload_failures_left > 0:
            self.upload_failures_left -= 1
            return web.Response(status=503)
        try:  # aiohttp may already have undone the Content-Encoding
            raw = gzip.decompress(body)
        except OSError:
            raw = body
        self.uploads.append({"headers": dict(request.headers), "lines": raw.decode().splitlines(),
                             "wire_bytes": len(body)})
        return web.Response(status=204)
