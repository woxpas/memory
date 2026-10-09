"""Cloud-policy gate + egress audit emitter.

Two responsibilities, kept together so the policy and the audit shape stay in lockstep:

  1. assert_cloud_allowed — pre-call gate. Local backends pass through silently.
     Cloud backends are checked against TWO independent tenant switches:
       a. allow_cloud_for_personal_data — gates personal-data profiles
       b. allow_cloud_for_sensitive_data — gates sensitive-class docs (any profile)
     A user opting into (a) does NOT auto-unlock (b). Either deny is hard-fail.

  2. audit_cloud_egress — post-call audit row. Every cloud call (success, denied,
     error) lands in audit_log so an operator can see exactly which doc bytes
     left the box, when, to which provider, and at what token cost.

Personal-data profiles (gated by allow_cloud_for_personal_data):
  - personal      — daily-life thoughts, voice notes, journal entries
  - identity_seed — the user's self-imported identity dump
  - correspondence — emails, messages, chat threads (often personal content)

Other profiles (creative_artefact, contract, reference, code, calendar_event,
agent_log, custom) bypass the personal-data gate. They are still gated by
sensitivity_class — a contract flagged 'sensitive' (e.g., NDA-bound) won't
go to cloud unless allow_cloud_for_sensitive_data is on.

Token counts are recorded for both cost tracking and forensic value: they
bound how much content actually traversed the wire.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

import structlog

if TYPE_CHECKING:
    from woxpas_runtime import LlmBackend, TokenUsage
    from woxpas_storage import PostgresPool

log = structlog.get_logger(__name__)


GATED_PROFILES: frozenset[str] = frozenset({
    "personal", "identity_seed", "correspondence",
})


class CloudPolicyDenied(RuntimeError):
    """Raised when a cloud LLM call is blocked by tenant policy.

    Carries `gate` ('personal_data' | 'sensitive_data') so callers can
    log/route the denial reason precisely.
    """

    def __init__(self, message: str, *, gate: str) -> None:
        super().__init__(message)
        self.gate = gate


async def assert_cloud_allowed(
    pool: PostgresPool,
    *,
    tenant_id:          UUID,
    processing_profile: str,
    sensitivity_class:  str,
    backend:            LlmBackend,
) -> None:
    """Raise CloudPolicyDenied if a cloud call would violate tenant policy.

    Local backends always pass. Cloud backends are evaluated against two
    independent gates:

      Sensitive gate (stricter, evaluated first):
        sensitivity_class == 'sensitive'
        AND allow_cloud_for_sensitive_data == false
        → DENY (gate='sensitive_data')

      Personal-data gate:
        processing_profile in GATED_PROFILES
        AND allow_cloud_for_personal_data == false
        → DENY (gate='personal_data')

    On deny, an audit row is written before the exception is raised — the
    attempt is forensically interesting even when the call never happens.
    """
    if not backend.is_cloud:
        return

    # Single round-trip to fetch both flags.
    # SECURITY: this uses the raw pool (no tenant_session), so Postgres RLS is
    # NOT engaged here. The `WHERE tenant_id = $1` filter is therefore
    # load-bearing — it is the only thing scoping this read to one tenant. Any
    # query added to this block MUST keep an explicit tenant_id predicate, or
    # route through pool.tenant_session() instead.
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT allow_cloud_for_personal_data,
                   allow_cloud_for_sensitive_data
              FROM tenants
             WHERE tenant_id = $1
            """,
            tenant_id,
        )
    if row is None:
        raise CloudPolicyDenied(
            f"tenant {tenant_id} not found", gate="tenant_missing",
        )
    allow_personal  = bool(row["allow_cloud_for_personal_data"])
    allow_sensitive = bool(row["allow_cloud_for_sensitive_data"])

    # Sensitive gate fires first — even non-gated profiles (code, reference,
    # contract, ...) are blocked when the doc itself is flagged sensitive.
    if sensitivity_class == "sensitive" and not allow_sensitive:
        await _emit_audit(
            pool,
            tenant_id=tenant_id,
            outcome="denied",
            backend_name=backend.name,
            backend_kind=backend.backend_kind,
            processing_profile=processing_profile,
            sensitivity_class=sensitivity_class,
            usage=None,
            error_reason="sensitive_policy_off",
        )
        log.warning(
            "cloud_policy.denied",
            tenant_id=str(tenant_id),
            profile=processing_profile,
            sensitivity=sensitivity_class,
            backend=backend.name,
            gate="sensitive_data",
        )
        raise CloudPolicyDenied(
            f"cloud LLM call to {backend.name} blocked: doc is "
            f"sensitivity_class='sensitive' and tenant policy "
            f"allow_cloud_for_sensitive_data=false",
            gate="sensitive_data",
        )

    # Personal-data gate.
    if processing_profile in GATED_PROFILES and not allow_personal:
        await _emit_audit(
            pool,
            tenant_id=tenant_id,
            outcome="denied",
            backend_name=backend.name,
            backend_kind=backend.backend_kind,
            processing_profile=processing_profile,
            sensitivity_class=sensitivity_class,
            usage=None,
            error_reason="personal_policy_off",
        )
        log.warning(
            "cloud_policy.denied",
            tenant_id=str(tenant_id),
            profile=processing_profile,
            sensitivity=sensitivity_class,
            backend=backend.name,
            gate="personal_data",
        )
        raise CloudPolicyDenied(
            f"cloud LLM call to {backend.name} blocked: tenant policy "
            f"allow_cloud_for_personal_data=false for profile={processing_profile!r}",
            gate="personal_data",
        )


async def audit_cloud_egress(
    pool: PostgresPool,
    *,
    tenant_id:          UUID,
    processing_profile: str,
    sensitivity_class:  str,
    backend:            LlmBackend,
    usage:              TokenUsage | None,
    outcome:            str = "success",
    error_reason:       str | None = None,
) -> None:
    """Record a cloud-egress audit row. Local-backend calls don't audit.

    Caller invokes this *after* the call completes (or raises). Token counts
    bound the content that actually left the box. error_reason is free-form
    (e.g., 'http_429', 'transport_timeout') so dashboards can roll up failure
    modes per provider.
    """
    if not backend.is_cloud:
        return
    await _emit_audit(
        pool,
        tenant_id=tenant_id,
        outcome=outcome,
        backend_name=backend.name,
        backend_kind=backend.backend_kind,
        processing_profile=processing_profile,
        sensitivity_class=sensitivity_class,
        usage=usage,
        error_reason=error_reason,
    )


async def _emit_audit(
    pool: PostgresPool,
    *,
    tenant_id:          UUID,
    outcome:            str,
    backend_name:       str,
    backend_kind:       str,
    processing_profile: str,
    sensitivity_class:  str,
    usage:              TokenUsage | None,
    error_reason:       str | None,
) -> None:
    metadata: dict[str, object] = {
        "backend":            backend_name,
        "backend_kind":       backend_kind,
        "processing_profile": processing_profile,
        "sensitivity_class":  sensitivity_class,
    }
    if usage is not None:
        metadata["prompt_tokens"]     = usage.prompt_tokens
        metadata["completion_tokens"] = usage.completion_tokens
        metadata["total_tokens"]      = usage.total_tokens
    if error_reason is not None:
        metadata["error_reason"] = error_reason

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO audit_log (tenant_id, actor, actor_kind, action,
                                   target_kind, outcome, metadata)
            VALUES ($1, 'pipeline.summarize', 'service', 'llm.cloud_egress',
                    'llm_backend', $2, $3)
            """,
            tenant_id,
            outcome,
            metadata,
        )
