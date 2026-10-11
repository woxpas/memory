"""FastAPI app factory + uvicorn entry point.

Topology:
  - Binds to 127.0.0.1 by default (cloud lift via reverse proxy with mTLS).
  - No /docs endpoint in production (api_settings.enable_docs).
  - Tenant resolution via dependency selected by auth_mode (dev | jwt).
  - PipelineContext constructed at startup, closed at shutdown.
  - The publisher is NOT started here — that's the worker process's job.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
import uvicorn
from fastapi import FastAPI

from woxpas_api.routes import (
    chunks,
    conflicts,
    documents,
    health,
    ingest,
    members,
    recall,
    resurface,
    scopes,
    synthesize,
)
from woxpas_api.settings import ApiSettings
from woxpas_api.state import AppState

log = structlog.get_logger(__name__)


def _configure_logging(level: str) -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
    )


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    api_settings: ApiSettings = app.state.api_settings
    log.info("api.startup", auth_mode=api_settings.auth_mode,
             host=api_settings.host, port=api_settings.port)

    # Pillar-3 startup guard: dev/single_tenant_local on a non-loopback
    # host is a misconfiguration. Authentication is enforced per-request
    # in auth.py too (defense in depth), but the startup warning surfaces
    # the mistake before the first request arrives.
    _LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
    if (api_settings.auth_mode in {"dev", "single_tenant_local"}
            and api_settings.host not in _LOOPBACK_HOSTS):
        log.warning(
            "api.startup.insecure_config",
            auth_mode=api_settings.auth_mode,
            host=api_settings.host,
            hint="dev/single_tenant_local mode rejects all requests when "
                 "API_HOST is non-loopback. Set API_AUTH_MODE=jwt for "
                 "remote-accessible deployments.",
        )

    state = await AppState.construct()
    app.state.app = state
    try:
        yield
    finally:
        log.info("api.shutdown")
        await state.close()


def create_app(api_settings: ApiSettings | None = None) -> FastAPI:
    api_settings = api_settings or ApiSettings()
    _configure_logging(api_settings.log_level)

    app = FastAPI(
        title="Woxpas API",
        version="0.1.0",
        docs_url="/docs" if api_settings.enable_docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if api_settings.enable_docs else None,
        lifespan=_lifespan,
    )
    app.state.api_settings = api_settings

    # Phase 5a.5+ — HTTP audit middleware. One audit_log row per
    # resolved-tenant request; honors api_settings.audit_level. Skips
    # /health to avoid noise.
    from woxpas_api.middleware import AuditMiddleware
    app.add_middleware(AuditMiddleware)

    # Routes (auth resolution happens inside tenant_id_dep, which reads
    # api_settings.auth_mode at call time).
    app.include_router(health.router)
    app.include_router(ingest.router)
    app.include_router(recall.router)
    app.include_router(synthesize.router)
    app.include_router(chunks.router)
    app.include_router(resurface.router)
    app.include_router(scopes.router)
    app.include_router(documents.router)
    app.include_router(conflicts.router)
    app.include_router(members.router)

    return app


def main() -> None:
    """uvicorn entry point. For container builds use this so signals propagate."""
    from woxpas_pipeline.boot import load_env
    load_env()
    api_settings = ApiSettings()
    config = uvicorn.Config(
        "woxpas_api.main:create_app",
        factory=True,
        host=api_settings.host,
        port=api_settings.port,
        workers=1,                                            # raise with proper LB in cloud
        log_level=api_settings.log_level.lower(),
        access_log=False,                                     # we emit structured logs ourselves
        timeout_keep_alive=30,
    )
    server = uvicorn.Server(config)
    try:
        asyncio.run(server.serve())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
