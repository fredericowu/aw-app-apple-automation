"""The ``aw-apple-automation`` MCP server, over Streamable HTTP (``POST
/mcp``).

Same shape as aw-app-android-studio and aw-app-google-maps: this app's own
already-authenticated route serves MCP directly, so aw-mcp-gateway's app-scan
just makes an HTTP call rather than spawning a stdio child that would have
neither this app's code nor its config/secret store.

A tool call can supply enough via per-call overrides (remote_host_id, udid)
to work even when the app's own default config is incomplete, so there is no
single "configured or not" gate here — ``remote_host.NotConfigured`` /
``HostOffline`` / ``HostNotFound`` / ``HostCommandFailed`` are all caught
below and reported as a tool error, naming exactly what's wrong. This is
where the offline-resilience acceptance criterion actually lands: a 409 from
an offline Mac.Home surfaces here as readable text in seconds, never a hang
and never an unhandled 500.
"""
from __future__ import annotations

import logging

from fastapi.concurrency import run_in_threadpool

from . import remote_host, sim_tools

log = logging.getLogger("aw_apps.apple-automation")

SERVER_NAME = "aw-apple-automation"
SERVER_VERSION = "1.0.0"

TOOLS_SCHEMA = sim_tools.TOOLS

_KNOWN_ERRORS = (
    remote_host.NotConfigured,
    remote_host.HostOffline,
    remote_host.HostNotFound,
    remote_host.HostCommandFailed,
)


def _result(req_id, text: str, is_error: bool) -> dict:
    return {"jsonrpc": "2.0", "id": req_id,
            "result": {"content": [{"type": "text", "text": text}],
                       "isError": is_error}}


async def handle_request(request: dict) -> dict | None:
    method = request.get("method", "")
    req_id = request.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS_SCHEMA}}
    if method != "tools/call":
        return {"jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32601, "message": f"Unknown method: {method}"}}

    params = request.get("params") or {}
    name = params.get("name", "")
    args = params.get("arguments") or {}

    handler = sim_tools.HANDLERS.get(name)
    if not handler:
        return _result(req_id, f"Unknown tool: {name}", True)

    try:
        # Handlers use blocking urllib against aw-backend, so they must not
        # run on the event loop — a slow remote-host round trip would stall
        # every other app route in this process.
        text = await run_in_threadpool(handler, args)
    except _KNOWN_ERRORS as exc:
        return _result(req_id, str(exc), True)
    except Exception as exc:  # noqa: BLE001 — last resort, must not 500 the route
        log.exception("apple-automation MCP tool %s failed", name)
        return _result(req_id, f"{name} failed: {exc}", True)

    return _result(req_id, text, False)
