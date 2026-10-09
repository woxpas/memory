"""MCP transport adapters — stdio (desktop) and HTTP (remote).

Uses the MCP SDK's built-in transport implementations. The Transport ABC
documents the contract; concrete implementations route to the SDK's own
StdioServerTransport, SseServerTransport, and StreamableHTTPServerTransport.

v0.1: stdio + streamable HTTP + SSE (legacy)
v0.2: AuthResolver ABC for OAuth
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID


class Transport(ABC):
    """Abstract MCP transport."""

    @abstractmethod
    async def serve(
        self,
        handler: Callable[[Any, Any], Awaitable[Any]],
    ) -> None:
        """Bind handler + run until parent disconnects / SIGTERM."""


class AuthResolver(ABC):
    """Abstract tenant-resolution at session-init time.

    v0.1: env-var tenant binding (single-tenant per process).
    v0.2: OAuth token resolver (multi-tenant).
    """

    @abstractmethod
    async def resolve(self, handshake_metadata: dict[str, Any]) -> UUID:
        """Return tenant_id for this MCP session. Raise on auth failure."""


# ── StdioTransport ──────────────────────────────────────────────────────────

class StdioTransport(Transport):
    """Stdio transport (Claude Desktop, Cursor, any local MCP agent)."""

    async def serve(
        self,
        handler: Callable[[Any, Any], Awaitable[Any]],
    ) -> None:
        from mcp.server.stdio import stdio_server

        async with stdio_server() as (reader, writer):
            await handler(reader, writer)


# ── HttpTransport ──────────────────────────────────────────────────────────

class HttpTransport(Transport):
    """HTTP transport for remote MCP clients.

    Modes (WOXPAS_MCP_HTTP_MODE):
      streamable (default, MCP 2025-03-26+):
        Single /mcp endpoint. POST → JSON-RPC, GET → SSE stream.
        Recommended for Claude Mobile, Claude Web, modern clients.

      sse (legacy, MCP 2024-11-05):
        Separate /sse (GET) + /messages (POST) endpoints.

    Auth modes (WOXPAS_MCP_AUTH_MODE):
      env (default) — single-tenant, tenant_id from env
      jwt — multi-tenant, resolve tenant from Bearer JWT 'sub' claim
    """

    def __init__(
        self, *,
        host: str = "127.0.0.1",
        port: int = 7801,
        http_mode: str = "streamable",
        auth_mode: str = "env",
        default_tenant_id: str = "",
        jwt_public_key: str = "",
        jwt_audience: str = "woxpas",
        jwt_issuer: str = "",
    ) -> None:
        self._host = host
        self._port = port
        self._http_mode = http_mode
        self._auth_mode = auth_mode
        self._default_tenant_id = default_tenant_id
        self._jwt_public_key = jwt_public_key
        self._jwt_audience = jwt_audience
        self._jwt_issuer = jwt_issuer

    async def serve(
        self,
        handler: Callable[[Any, Any], Awaitable[Any]],
    ) -> None:
        if self._http_mode == "streamable":
            await self._serve_streamable(handler)
        else:
            await self._serve_sse(handler)

    async def _serve_streamable(self, handler) -> None:
        import asyncio as _asyncio

        import uvicorn
        from mcp.server.streamable_http import StreamableHTTPServerTransport
        from starlette.responses import JSONResponse, PlainTextResponse

        host = self._host
        port = self._port

        # Create a persistent transport — the MCP session state must survive
        # across multiple HTTP requests (initialize → tools/list → tool calls).
        transport = StreamableHTTPServerTransport(
            mcp_session_id="woxpas",
            is_json_response_enabled=True,
        )

        # Start a background task running the MCP server handler.
        # The handler (server.run) processes incoming messages from the
        # transport and sends responses back. This task runs for the
        # lifetime of the server.
        async def _run_handler():
            async with transport.connect() as (read_stream, write_stream):
                await handler(read_stream, write_stream)

        handler_task = _asyncio.create_task(_run_handler())

        async def asgi_app(scope, receive, send):
            if scope["type"] != "http":
                response = PlainTextResponse("Not found", status_code=404)
                await response(scope, receive, send)
                return

            path = scope["path"]
            method = scope["method"]

            if path == "/healthz" and method == "GET":
                response = JSONResponse({
                    "ok": True, "transport": "http-streamable",
                    "auth_mode": self._auth_mode,
                })
                await response(scope, receive, send)
                return

            # JWT auth: resolve tenant before routing
            if self._auth_mode == "jwt":
                try:
                    scope["woxpas_tenant_id"] = await self._resolve_tenant(scope)
                except _AuthError as e:
                    response = JSONResponse(
                        {"error": e.reason}, status_code=e.status_code,
                    )
                    await response(scope, receive, send)
                    return

            if path == "/mcp":
                await transport.handle_request(scope, receive, send)
                return

            response = PlainTextResponse("Not found", status_code=404)
            await response(scope, receive, send)

        config = uvicorn.Config(asgi_app, host=host, port=port, log_level="warning")
        server = uvicorn.Server(config)

        try:
            await server.serve()
        finally:
            if not handler_task.done():
                handler_task.cancel()
                try:
                    await handler_task
                except (_asyncio.CancelledError, Exception):  # noqa: S110  # best-effort await of a cancelled handler task during teardown
                    pass

    async def _serve_sse(self, handler) -> None:
        import uvicorn
        from mcp.server.sse import SseServerTransport
        from starlette.responses import JSONResponse, PlainTextResponse

        sse_transport = SseServerTransport("/messages")
        host = self._host
        port = self._port

        async def asgi_app(scope, receive, send):
            if scope["type"] != "http":
                response = PlainTextResponse("Not found", status_code=404)
                await response(scope, receive, send)
                return

            path = scope["path"]
            method = scope["method"]

            if path == "/healthz" and method == "GET":
                response = JSONResponse({"ok": True, "transport": "http-sse"})
                await response(scope, receive, send)
                return

            if path == "/sse" and method == "GET":
                async with sse_transport.connect_sse(scope, receive, send) as streams:
                    await handler(streams[0], streams[1])
                return
            elif path == "/messages" and method == "POST":
                await sse_transport.handle_post_message(scope, receive, send)
                return

            response = PlainTextResponse("Not found", status_code=404)
            await response(scope, receive, send)

        config = uvicorn.Config(asgi_app, host=host, port=port, log_level="warning")
        server = uvicorn.Server(config)
        await server.serve()

    async def _resolve_tenant(self, scope) -> str:
        """Resolve tenant_id from auth mode. Returns tenant_id string."""
        if self._auth_mode == "jwt":
            headers = dict(scope.get("headers", []))
            auth = headers.get(b"authorization", b"").decode()
            if not auth.lower().startswith("bearer "):
                raise _AuthError("missing Bearer token", 401)
            token = auth[7:].strip()
            import jwt
            try:
                claims = jwt.decode(
                    token,
                    key=self._jwt_public_key,
                    algorithms=["RS256", "ES256"],
                    audience=self._jwt_audience,
                    issuer=self._jwt_issuer or None,
                )
            except jwt.PyJWTError as e:
                raise _AuthError(f"invalid token: {e}", 401) from e
            tenant_id = claims.get("sub", "")
            if not tenant_id:
                raise _AuthError("token missing sub claim", 401)
            return tenant_id
        return self._default_tenant_id


class _AuthError(Exception):
    def __init__(self, reason: str, status_code: int = 401) -> None:
        self.reason = reason
        self.status_code = status_code
