"""Synthesis-specific cloud-policy gate.

Differs from the pipeline gate (single doc → single profile/sensitivity)
because synthesis aggregates a BAG of chunks with potentially mixed
sensitivities and processing profiles. The gate uses worst-case across
the bag — the strictest signal wins.

Two-flag composition (AND semantics — both tenant policy AND request
must allow):

  effective_personal = tenant.allow_cloud_for_personal_data
                       AND request.allow_cloud_for_personal_data
  effective_sensitive = tenant.allow_cloud_for_sensitive_data
                        AND request.allow_cloud_for_sensitive_data

Where request flag is None (default), tenant value is used as-is.
Where request flag is False, the call is blocked regardless of tenant.
Where request flag is True, the call still requires tenant=True (request
can't grant beyond tenant ceiling — security boundary).

On deny, an audit row is written via the shared pipeline.cloud_policy
audit emitter so synthesis denials show up alongside pipeline denials in
the same audit_log table — operators have one place to look.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

import structlog
from woxpas_pipeline.cloud_policy import (
    GATED_PROFILES,
    CloudPolicyDenied,
)
from woxpas_pipeline.cloud_policy import (
    audit_cloud_egress as _audit_egress_pipeline,
)

if TYPE_CHECKING:
    from woxpas_runtime import LlmBackend, TokenUsage
    from woxpas_storage import PostgresPool

    from woxpas_synthesis.schema import CitationSource

log = structlog.get_logger(__name__)


# Sensitivity-class ordinals — higher = stricter.
_SENS_RANK = {"standard": 0, "personal_data": 1, "sensitive": 2}


# ─── Public API ─────────────────────────────────────────────────────────────

async def assert_cloud_allowed_for_synthesis(
    pool: PostgresPool,
    *,
    tenant_id:                       UUID,
    sources:                         list[CitationSource],
    backend:                         LlmBackend,
    request_allow_personal_data:     bool | None,
    request_allow_sensitive_data:    bool | None,
) -> None:
    """Bag-of-chunks pre-call gate. Local backends pass through silently.

    Resolves effective sensitivity (max across all sources) + effective
    personal-data flag (any source has a gated profile). Then composes
    tenant + request flags via AND semantics:

      tenant.flag    request.flag    effective.flag
      ───────────    ────────────    ──────────────
      True           None             True
      True           True             True
      True           False            False (request denies)
      False          None             False (tenant denies)
      False          True             False (can't upgrade beyond tenant)
      False          False            False
    """
    if not backend.is_cloud:
        return

    # Empty source set — nothing sensitive to leak. Pass.
    if not sources:
        return

    # Tenant policy
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
    tenant_allow_personal  = bool(row["allow_cloud_for_personal_data"])
    tenant_allow_sensitive = bool(row["allow_cloud_for_sensitive_data"])

    # Compose: tenant value when request is None; AND otherwise.
    effective_allow_personal = (
        tenant_allow_personal
        if request_allow_personal_data is None
        else (request_allow_personal_data and tenant_allow_personal)
    )
    effective_allow_sensitive = (
        tenant_allow_sensitive
        if request_allow_sensitive_data is None
        else (request_allow_sensitive_data and tenant_allow_sensitive)
    )

    # Aggregate sensitivity + profile across the chunk set.
    max_sensitivity = "standard"
    has_gated_profile = False
    gated_chunk_id_sample: str | None = None
    for s in sources:
        rank = _SENS_RANK.get(s.sensitivity_class, 0)
        if rank > _SENS_RANK[max_sensitivity]:
            max_sensitivity = s.sensitivity_class
        if s.processing_profile in GATED_PROFILES:
            has_gated_profile = True
            if gated_chunk_id_sample is None:
                gated_chunk_id_sample = str(s.chunk_id)[:8]

    # Effective profile name for audit/logs.
    effective_profile = "personal" if has_gated_profile else "mixed"

    # Sensitive gate fires first (strictest).
    if max_sensitivity == "sensitive" and not effective_allow_sensitive:
        await _audit_egress_pipeline(
            pool,
            tenant_id=tenant_id,
            processing_profile=effective_profile,
            sensitivity_class=max_sensitivity,
            backend=backend, usage=None,
            outcome="denied", error_reason="sensitive_policy_off",
        )
        log.warning(
            "synthesis.cloud_policy.denied",
            tenant_id=str(tenant_id),
            sensitivity=max_sensitivity, profile=effective_profile,
            backend=backend.name, gate="sensitive_data",
            tenant_flag=tenant_allow_sensitive,
            request_flag=request_allow_sensitive_data,
            sources_count=len(sources),
        )
        raise CloudPolicyDenied(
            f"cloud LLM call to {backend.name} blocked: at least one "
            f"retrieved chunk has sensitivity_class='sensitive' and the "
            f"effective policy denies cloud egress for sensitive data "
            f"(tenant={tenant_allow_sensitive}, "
            f"request={request_allow_sensitive_data})",
            gate="sensitive_data",
        )

    # Personal-data gate.
    if has_gated_profile and not effective_allow_personal:
        await _audit_egress_pipeline(
            pool,
            tenant_id=tenant_id,
            processing_profile=effective_profile,
            sensitivity_class=max_sensitivity,
            backend=backend, usage=None,
            outcome="denied", error_reason="personal_policy_off",
        )
        log.warning(
            "synthesis.cloud_policy.denied",
            tenant_id=str(tenant_id),
            sensitivity=max_sensitivity, profile=effective_profile,
            backend=backend.name, gate="personal_data",
            tenant_flag=tenant_allow_personal,
            request_flag=request_allow_personal_data,
            sources_count=len(sources),
            sample_gated_chunk=gated_chunk_id_sample,
        )
        raise CloudPolicyDenied(
            f"cloud LLM call to {backend.name} blocked: at least one "
            f"retrieved chunk has a personal-data profile and the "
            f"effective policy denies cloud egress for personal data "
            f"(tenant={tenant_allow_personal}, "
            f"request={request_allow_personal_data})",
            gate="personal_data",
        )


async def audit_synthesis_cloud_egress(
    pool: PostgresPool,
    *,
    tenant_id:    UUID,
    sources:      list[CitationSource],
    backend:      LlmBackend,
    usage:        TokenUsage | None,
    outcome:      str = "success",
    error_reason: str | None = None,
) -> None:
    """Record a synthesis cloud egress audit row.

    Same audit table as pipeline egress so operators see all cloud calls
    in one place. Profile + sensitivity are the AGGREGATED values across
    the source set (max sensitivity, "personal" if any gated profile).
    """
    if not backend.is_cloud:
        return

    max_sensitivity = "standard"
    has_gated_profile = False
    for s in sources:
        rank = _SENS_RANK.get(s.sensitivity_class, 0)
        if rank > _SENS_RANK[max_sensitivity]:
            max_sensitivity = s.sensitivity_class
        if s.processing_profile in GATED_PROFILES:
            has_gated_profile = True
    effective_profile = "personal" if has_gated_profile else "mixed"

    await _audit_egress_pipeline(
        pool,
        tenant_id=tenant_id,
        processing_profile=effective_profile,
        sensitivity_class=max_sensitivity,
        backend=backend, usage=usage,
        outcome=outcome, error_reason=error_reason,
    )
