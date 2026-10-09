"""woxpas-mcp-server entry point.

Standalone process spawned by an MCP-capable agent (Claude Desktop,
Cursor, etc.). The agent's MCP config block sets the env vars
including WOXPAS_MCP_TENANT_ID; this process inherits them, constructs
engines, then talks JSON-RPC over stdin/stdout until the parent closes
the pipe.

Logs go to STDERR exclusively (stdout is reserved for the protocol).
"""

from __future__ import annotations

import asyncio
import signal
import sys
from typing import Any

import structlog


def _configure_logging(level: str) -> None:
    """Send all structlog output to stderr — stdout is the MCP protocol."""
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(_resolve_level(level)),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
    )


def _resolve_level(level: str) -> int:
    import logging
    return getattr(logging, level.upper(), logging.INFO)


async def _amain() -> int:
    # Defer logging config until after we've at least tried to import
    # so a syntax error surfaces clearly.
    from woxpas_mcp.server import WoxpasMcpServer
    from woxpas_mcp.settings import McpSettings

    try:
        # Probe settings early so a missing tenant fails fast + clear.
        settings = McpSettings()
    except Exception as e:
        print(
            f"woxpas-mcp-server: refusing to start: {type(e).__name__}: {e}\n"
            "Hint: set WOXPAS_MCP_TENANT_ID in the agent's MCP config env block.",
            file=sys.stderr,
        )
        return 1

    _configure_logging(settings.log_level)
    log = structlog.get_logger("woxpas_mcp.main")
    log.info("mcp_server.starting", tenant_id=str(settings.tenant_id))

    server = await WoxpasMcpServer.construct()

    # Select transport. stdio = local agents (Claude Desktop, Cursor).
    # http = remote agents (Claude Mobile, web clients) behind a reverse
    # proxy that terminates TLS. Auth is the proxy's responsibility.
    from woxpas_mcp.adapters import Transport
    transport: Transport
    if settings.transport == "http":
        from woxpas_mcp.adapters import HttpTransport
        transport = HttpTransport(
            host=settings.http_host, port=settings.http_port,
            http_mode=settings.http_mode,
            auth_mode=settings.auth_mode,
            default_tenant_id=str(settings.tenant_id),
            jwt_public_key=settings.jwt_public_key,
            jwt_audience=settings.jwt_audience,
            jwt_issuer=settings.jwt_issuer,
        )
        log.info("mcp_server.transport", kind="http",
                 host=settings.http_host, port=settings.http_port,
                 http_mode=settings.http_mode,
                 auth_mode=settings.auth_mode)
    else:
        from woxpas_mcp.adapters import StdioTransport
        transport = StdioTransport()
        log.info("mcp_server.transport", kind="stdio")

    loop = asyncio.get_event_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    # Wrap server.run so transports always get (reader, writer) → run(reader, writer, opts)
    def _run(reader: Any, writer: Any) -> Any:
        return server._mcp.run(reader, writer, server._mcp.create_initialization_options())

    serve_task = asyncio.create_task(transport.serve(_run))
    stop_task  = asyncio.create_task(stop.wait())
    done, _ = await asyncio.wait(
        {serve_task, stop_task},
        return_when=asyncio.FIRST_COMPLETED,
    )

    if stop_task in done and not serve_task.done():
        log.info("mcp_server.shutdown_signal")
        serve_task.cancel()
        try:
            await serve_task
        except (asyncio.CancelledError, Exception):  # noqa: S110  # best-effort await of the cancelled serve task on shutdown
            pass

    try:
        await server.close()
    except Exception as e:
        log.error("mcp_server.close_failed",
                  error_class=type(e).__name__, error=str(e)[:300])

    log.info("mcp_server.stopped")
    return 0


def main() -> None:
    """Console-script entry. Loads .env (dev convenience) then runs."""
    try:
        from woxpas_pipeline.boot import load_env
        load_env()
    except Exception:  # noqa: S110  # .env is a dev convenience; production supplies env directly
        pass
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
