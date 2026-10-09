"""Health endpoints. Unauthenticated; no tenant context required."""

from __future__ import annotations

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(request: Request) -> JSONResponse:
    """Aggregate liveness + dependency probe.

    HTTP 200 when all backends ok; 503 if any backend reports failure. The
    response body always carries per-backend detail for debugging.
    """
    state = request.app.state.app
    results = await state.ctx.health()
    ok = all(r.get("ok") for r in results.values())
    return JSONResponse(
        status_code=status.HTTP_200_OK if ok else status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"ok": ok, "backends": results,
                 "pipeline_version": state.ctx.pipeline.pipeline_version},
    )


@router.get("/healthz")
async def healthz() -> dict[str, bool]:
    """Liveness only — does not touch any backend. Always 200 if process is alive."""
    return {"ok": True}
