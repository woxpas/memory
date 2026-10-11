"""Structured error codes for tool responses.

Every error surfaced to an agent carries a `code` (machine-readable) +
`message` (redacted, human-readable). Stack traces are NEVER leaked to
agents; they go to stderr via structlog only.

Codes from project_phase5a5_mcp_plan.md §6.8:
"""

from __future__ import annotations

from dataclasses import dataclass


class ToolError(Exception):
    """Base class. Carries a structured code + retriable flag."""
    code: str = "internal_error"
    retriable: bool = False

    def __init__(self, message: str, *, retriable: bool | None = None) -> None:
        super().__init__(message)
        self.message = message
        if retriable is not None:
            self.retriable = retriable


class TenantNotSet(ToolError):
    code = "tenant_not_set"


class CloudPolicyDenied(ToolError):
    code = "cloud_policy_denied"


class NotFound(ToolError):
    code = "not_found"


class PreconditionFailed(ToolError):
    code = "precondition_failed"


class InvalidState(ToolError):
    code = "invalid_state"


class RateLimited(ToolError):
    code = "rate_limited"
    retriable = True


class RoleDenied(ToolError):
    """The bound member's role is below the tool's minimum (team memory,
    Phase A3). Not retriable — the role must be changed by an admin."""
    code = "role_denied"


class ValidationFailed(ToolError):
    code = "validation_failed"


class IdempotencyKeyConflict(ToolError):
    code = "idempotency_key_conflict"


@dataclass(slots=True)
class StructuredError:
    code:      str
    message:   str
    retriable: bool = False

    @classmethod
    def from_exception(cls, exc: BaseException) -> StructuredError:
        if isinstance(exc, ToolError):
            return cls(code=exc.code, message=exc.message, retriable=exc.retriable)
        # Generic exceptions are redacted to avoid leaking internals.
        return cls(
            code="internal_error",
            message=f"{type(exc).__name__}: {str(exc)[:200]}",
            retriable=False,
        )

    def to_text(self) -> str:
        """Render as the text payload of an MCP CallToolResult.

        MCP responses can include structured error info, but for v0.1 we
        embed structured fields in JSON so any client can parse them.
        """
        import json
        return json.dumps({
            "error": {
                "code": self.code,
                "message": self.message,
                "retriable": self.retriable,
            }
        })
