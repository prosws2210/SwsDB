"""The MCP server over streamable HTTP: /mcp with the read-only tools, /mcp/rw with the read-write ones.

The tools (swsdb.mcp_server, shared with `swsdb mcp`) call this server's own API with the caller's key and tenant, so an
agent gets exactly that key's tenant and permissions. Requests without a valid key are refused here, before any MCP
session starts, so clients can tell the user to set one.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request
from mcp.server.transport_security import TransportSecuritySettings
from starlette.routing import Route
from starlette.types import Receive, Scope, Send
from swsdb import SwsDB
from swsdb.mcp_server import build

from .settings import Settings


def _client_for(s: Settings):
    def make(ctx: Any) -> SwsDB:
        req = ctx.request_context.request
        auth = req.headers.get("authorization", "")
        key = req.headers.get("api-key") or (auth[7:] if auth.lower().startswith("bearer ") else "")
        return SwsDB(s.local_url, api_key=key, tenant=req.headers.get("swsdb-tenant"), timeout=180,
                     verify=False)  # loopback to this very server: its certificate names the public host
    return make


class _Guarded:
    """Check the API key (and for /mcp/rw the admin role) before handing the request to the MCP transport."""

    def __init__(self, inner, caller, need_admin: bool):
        self.inner, self.caller, self.need_admin = inner, caller, need_admin

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        from .app import _error  # app imports this module, so import late
        try:
            c = await self.caller(Request(scope, receive))
            if self.need_admin and c.role != "admin":
                raise HTTPException(403, "/mcp/rw adds write tools: it needs an admin key (use /mcp for read-only)")
        except HTTPException as e:
            await _error(e.status_code, str(e.detail))(scope, receive, send)
            return
        await self.inner(scope, receive, send)


def servers(s: Settings, caller) -> tuple[list, list[Route]]:
    """The two MCP servers (their session managers run in the app's lifespan) and the routes to add."""
    out, routes = [], []
    for path, rw in (("/mcp", False), ("/mcp/rw", True)):
        mcp = build(_client_for(s), read_write=rw, per_request=True, stateless_http=True, json_response=True,
                    streamable_http_path=path,
                    # Host and Origin are checked for every route by the app itself (see app.same_origin)
                    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
        inner = mcp.streamable_http_app().routes[0].app  # the transport's ASGI handler
        routes.append(Route(path, endpoint=_Guarded(inner, caller, rw), methods=["GET", "POST", "DELETE"]))
        out.append(mcp)
    return out, routes
