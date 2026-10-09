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
from typing import Any

from pydantic import BaseModel


@dataclass(slots=True)
class ToolDef:
    name:        str
    description: str
    input_model: type[BaseModel]
    handler:     Callable[..., Awaitable[Any]]
    """Async function: handler(*, ctx: ToolContext, input: <input_model>) → Any"""


_REGISTRY: dict[str, ToolDef] = {}


def tool(
    name: str,
    *,
    description: str,
    input_model: type[BaseModel],
):
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
    def decorator(fn: Callable[..., Awaitable[Any]]):
        if name in _REGISTRY:
            raise RuntimeError(f"tool already registered: {name!r}")
        if not inspect.iscoroutinefunction(fn):
            raise TypeError(f"tool handler must be async: {name!r}")
        _REGISTRY[name] = ToolDef(
            name=name,
            description=description,
            input_model=input_model,
            handler=fn,
        )
        return fn
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
