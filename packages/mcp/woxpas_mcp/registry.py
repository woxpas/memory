"""Tool registry — decorator-based, JSON Schema generated from pydantic.

Each tool registers a (name, description, input_model, handler) tuple.
At server startup we materialize the registry into MCP-protocol Tool
descriptors that the SDK serves to clients.

Why this pattern:
  - JSON Schema is auto-generated from pydantic via .model_json_schema()
    so input contracts never drift from validation logic
  - Adding a new tool is a single decorator + handler function — no
    forgetting to register or to validate input
  - Handlers receive a typed pydantic instance, not a raw dict

The registry is global (module-level) — fine because each MCP server
process is single-tenant and constructed once.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar, cast

from pydantic import BaseModel

# Identity-preserving bound so `@tool(...)` keeps the handler's own type
# (otherwise every decorated handler is seen as untyped).
_Handler = TypeVar("_Handler", bound=Callable[..., Awaitable[Any]])


@dataclass(slots=True)
class ToolDef:
    name:        str
    description: str
    input_model: type[BaseModel]
    handler:     Callable[..., Awaitable[Any]]
    """Async function: handler(*, ctx: ToolContext, input: <input_model>) → Any"""
    min_role:    str = "member"
    """Minimum team-member role to call this tool (team memory, Phase A3):
    viewer < member < owner. DEFAULTS to 'member' so a tool that forgets to
    declare is over-restricted (viewers can't call it) rather than leaking a
    write — read-only tools must opt down to 'viewer' explicitly. Enforced at
    the dispatch chokepoint in server.py; ignored when the server is bound to
    no member (single-person install)."""


_REGISTRY: dict[str, ToolDef] = {}


def tool(
    name: str,
    *,
    description: str,
    input_model: type[BaseModel],
    min_role: str = "member",
) -> Callable[[_Handler], _Handler]:
    """Decorator that registers an async handler.

    Usage:
        class RecallInput(BaseModel):
            query_text: str
            limit: int = 20

        @tool(
            name="memory_recall",
            description="Retrieve chunks for a query.",
            input_model=RecallInput,
        )
        async def memory_recall(*, ctx: ToolContext, input: RecallInput):
            ...
    """
    def decorator(fn: _Handler) -> _Handler:
        if name in _REGISTRY:
            raise RuntimeError(f"tool already registered: {name!r}")
        if not inspect.iscoroutinefunction(fn):
            raise TypeError(f"tool handler must be async: {name!r}")
        _REGISTRY[name] = ToolDef(
            name=name,
            description=description,
            input_model=input_model,
            handler=fn,
            min_role=min_role,
        )
        # inspect.iscoroutinefunction narrows fn to a bare coroutine-function
        # type above; re-assert the original _Handler bind for the caller.
        return cast(_Handler, fn)
    return decorator


def all_tools() -> list[ToolDef]:
    """Snapshot of registered tools (insertion order; Python 3.7+
    dict ordering is stable)."""
    return list(_REGISTRY.values())


def get_tool(name: str) -> ToolDef | None:
    return _REGISTRY.get(name)


def reset_registry() -> None:
    """Test helper — clears the registry. Production server never calls
    this; the registry is populated once at module import time."""
    _REGISTRY.clear()
