"""Aggregate health check across storage backends.

Used by the API gateway's /health endpoint and by the boot supervisor's
readiness gate. All checks run in parallel with bounded timeout.
"""

from __future__ import annotations

import asyncio
from typing import Any

from woxpas_storage.objstore import ObjectStore
from woxpas_storage.postgres import PostgresPool
from woxpas_storage.qdrant import QdrantClient

CheckResult = dict[str, Any]


async def aggregate_health(
    *,
    pg: PostgresPool | None = None,
    qd: QdrantClient | None = None,
    obj: ObjectStore | None = None,
    timeout_s: float = 3.0,
) -> dict[str, CheckResult]:
    """Run all available checks in parallel; return per-backend dict.

    Each check is wrapped in a bounded timeout — a slow backend doesn't block
    the overall response. Missing clients are reported as 'absent'.
    """
    async def _wrapped(name: str, coro: Any) -> tuple[str, CheckResult]:
        try:
            res = await asyncio.wait_for(coro, timeout=timeout_s)
            return name, res
        except TimeoutError:
            return name, {"ok": False, "error": "timeout"}
        except Exception as e:
            return name, {"ok": False, "error": str(e)}

    tasks: list[asyncio.Task[tuple[str, CheckResult]]] = []
    if pg is not None:
        tasks.append(asyncio.create_task(_wrapped("postgres", pg.health())))
    if qd is not None:
        tasks.append(asyncio.create_task(_wrapped("qdrant", qd.health())))
    if obj is not None:
        tasks.append(asyncio.create_task(_wrapped("objstore", obj.health())))

    results: dict[str, CheckResult] = {}
    for fut in asyncio.as_completed(tasks):
        name, res = await fut
        results[name] = res
    for k, v in (("postgres", pg), ("qdrant", qd), ("objstore", obj)):
        if v is None:
            results[k] = {"ok": False, "error": "absent"}
    return results
