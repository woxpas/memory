"""Team-memory Phase A3: every MCP tool declares the right minimum role.

This is the drift guard. Write tools must require 'member'; read-only tools opt
down to 'viewer'. A new tool that forgets to classify defaults to 'member'
(over-restricted, not a leak) — but if someone tags a WRITE tool 'viewer', or a
read tool regresses, this test fails. Keep the two sets below authoritative.
"""

from __future__ import annotations

import woxpas_mcp.tools  # noqa: F401  # fires @tool registration
from woxpas_mcp.registry import all_tools

# Read-only tools callable by a viewer.
_VIEWER_TOOLS = {
    "memory_recall",
    "memory_ask",
    "memory_get_chunks",
    "memory_get_document_full",
    "memory_get_conflict",
    "memory_list_conflicts",
    "resurface_get_context",
    "resurface_get_patterns",
    "resurface_get_predicted_actions",
    "resurface_get_predicted_questions",
    "resurface_get_preferences",
    "resurface_get_tool_suggestions",
    "resurface_list_prepared_actions",
}

# Content-mutating tools that require at least 'member'.
_MEMBER_TOOLS = {
    "memory_ingest",
    "memory_create_scope",
    "memory_assign_to_scope",
    "memory_unassign_from_scope",
    "memory_archive_document",
    "memory_unarchive_document",
    "memory_resolve_conflict",
    "resurface_upsert_persona",
    "resurface_record_decision",
    "resurface_record_observation",
    "resurface_post_prepared_action",
    "resurface_approve_prepared_action",
    "resurface_reject_prepared_action",
    "resurface_mark_executed",
    "resurface_build_identity",
}


def test_every_tool_is_classified() -> None:
    names = {t.name for t in all_tools()}
    assert names == _VIEWER_TOOLS | _MEMBER_TOOLS, (
        "tool set changed — classify new tools in this test and tag them"
    )


def test_read_tools_are_viewer() -> None:
    for t in all_tools():
        if t.name in _VIEWER_TOOLS:
            assert t.min_role == "viewer", f"{t.name} should be viewer-callable"


def test_write_tools_require_member() -> None:
    for t in all_tools():
        if t.name in _MEMBER_TOOLS:
            assert t.min_role == "member", f"{t.name} must require member+ (got {t.min_role})"


def test_default_min_role_is_member() -> None:
    # A forgotten classification must fail safe (over-restrict), never leak.
    from pydantic import BaseModel
    from woxpas_mcp.registry import ToolDef

    class _X(BaseModel):
        pass

    async def _h(*, ctx: object, input: _X) -> None: ...
    td = ToolDef(name="x", description="", input_model=_X, handler=_h)
    assert td.min_role == "member"
