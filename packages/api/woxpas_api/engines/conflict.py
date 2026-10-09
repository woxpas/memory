"""ConflictEngine — Phase 5a.5.0 Group E.

Conflict list / get / resolve. Honors the four resolution shapes from
project_conflict_resolution_shapes.md:

  pick         — one value correct, the other rejected (winner+loser)
  coexistence  — both correct in different contexts (discriminators)
  progression  — temporal evolution A → B → C (valid_times on each side)
  dismissed    — false-positive detection or "agree to disagree"

The repo's ConflictRepo.resolve already handles the state-machine
mapping; we just shape the input + add audit logging. v_conflicts_open
is the read view for list/get.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

import structlog

log = structlog.get_logger(__name__)


# ─── Result types ──────────────────────────────────────────────────────────

@dataclass(slots=True)
class ConflictListItem:
    conflict_id:        UUID
    kind:               str
    severity:           str
    predicate:          str | None
    notes:              str | None
    detected_at:        str
    subject_entity_ids: list[UUID]
    doc_refs:           list[UUID]


@dataclass(slots=True)
class ConflictDetail:
    conflict_id:        UUID
    kind:               str
    severity:           str
    predicate:          str | None
    notes:              str | None
    detected_at:        str
    resolved_state:     str
    resolved_value:     dict[str, Any] | None
    resolved_at:        str | None
    subject_entity_ids: list[UUID]
    doc_refs:           list[UUID]
    mention_refs:       list[dict[str, Any]]
    suggested_shapes:   list[str] = field(default_factory=list)
    """Hint to the agent — which resolution shapes are most likely
    appropriate for THIS conflict. Heuristic based on type/severity."""


@dataclass(slots=True)
class ConflictResolutionResult:
    conflict_id: UUID
    kind:        str
    new_state:   str


# ─── Engine ────────────────────────────────────────────────────────────────

class ConflictEngine:
    """List / get / resolve conflicts. Tenant-isolated via tenant_session."""

    def __init__(self, *, pool) -> None:                      # type: ignore[no-untyped-def]
        self._pool = pool

    async def list_conflicts(
        self, *,
        tenant_id:    UUID,
        state:        str = "open",
        severity:     str | None = None,
        kind:         str | None = None,
        limit:        int = 50,
    ) -> list[ConflictListItem]:
        """List conflicts. Default state='open'. Other states:
        auto_resolved | human_resolved | living | dismissed. Pass
        state='all' to see history."""
        async with self._pool.tenant_session(tenant_id) as conn:
            clauses = ["tenant_id = $1"]
            params: list[Any] = [tenant_id]
            if state != "all":
                params.append(state)
                clauses.append(f"resolved_state = ${len(params)}")
            if severity is not None:
                params.append(severity)
                clauses.append(f"severity = ${len(params)}")
            if kind is not None:
                params.append(kind)
                clauses.append(f"type = ${len(params)}")
            params.append(limit)
            sql = f"""
                SELECT conflict_id, type AS kind, severity, predicate,
                       notes, detected_at, subject_entity_ids, doc_refs
                  FROM conflicts
                 WHERE {' AND '.join(clauses)}
                 ORDER BY
                   CASE severity
                     WHEN 'critical' THEN 0
                     WHEN 'high'     THEN 1
                     WHEN 'medium'   THEN 2
                     WHEN 'low'      THEN 3
                     ELSE 4
                   END,
                   detected_at DESC
                 LIMIT ${len(params)}
            """
            rows = await conn.fetch(sql, *params)

        return [
            ConflictListItem(
                conflict_id=r["conflict_id"],
                kind=r["kind"],
                severity=r["severity"],
                predicate=r["predicate"],
                notes=r["notes"],
                detected_at=r["detected_at"].isoformat(),
                subject_entity_ids=list(r["subject_entity_ids"] or []),
                doc_refs=list(r["doc_refs"] or []),
            )
            for r in rows
        ]

    async def get_conflict(
        self, *, tenant_id: UUID, conflict_id: UUID,
    ) -> ConflictDetail:
        """Full detail with mention_refs (per-side claims with chunk
        provenance) + suggested resolution shapes hint."""
        async with self._pool.tenant_session(tenant_id) as conn:
            row = await conn.fetchrow(
                """
                SELECT conflict_id, type AS kind, severity, predicate,
                       notes, detected_at,
                       resolved_state, resolved_value, resolved_at,
                       subject_entity_ids, doc_refs, mention_refs
                  FROM conflicts
                 WHERE tenant_id  = $1
                   AND conflict_id = $2
                """,
                tenant_id, conflict_id,
            )
        if row is None:
            raise ValueError(f"conflict not found: {conflict_id}")

        # Heuristic suggested_shapes:
        # - all kinds: dismissed is always offered (false-positive escape)
        # - pick: when one side has stronger source confidence (defer; offer all 4)
        # - coexistence: hint when subjects involve scope or location
        # - progression: hint when severity is low (often temporal change)
        suggested = ["pick", "coexistence", "progression", "dismissed"]

        return ConflictDetail(
            conflict_id=row["conflict_id"],
            kind=row["kind"],
            severity=row["severity"],
            predicate=row["predicate"],
            notes=row["notes"],
            detected_at=row["detected_at"].isoformat(),
            resolved_state=row["resolved_state"],
            resolved_value=dict(row["resolved_value"]) if row["resolved_value"] else None,
            resolved_at=row["resolved_at"].isoformat() if row["resolved_at"] else None,
            subject_entity_ids=list(row["subject_entity_ids"] or []),
            doc_refs=list(row["doc_refs"] or []),
            mention_refs=list(row["mention_refs"] or []),
            suggested_shapes=suggested,
        )

    async def resolve_conflict(
        self, *,
        tenant_id:       UUID,
        conflict_id:     UUID,
        kind:            Literal["pick", "coexistence", "progression", "dismissed"],
        winner:          UUID | None = None,
        loser:           UUID | None = None,
        discriminators:  dict[str, str] | None = None,
        valid_times:     dict[str, str] | None = None,
        note:            str | None = None,
        resolved_by:     str = "agent",
    ) -> ConflictResolutionResult:
        """Apply a resolution to a conflict. Maps to ConflictRepo.resolve;
        validates that the required fields per shape are present."""
        # Per-shape validation
        if kind == "pick" and (winner is None or loser is None):
            raise ValueError("pick resolution requires both winner and loser")
        if kind == "coexistence" and not discriminators:
            raise ValueError(
                "coexistence resolution requires at least one discriminator "
                "(e.g., {'context': 'home_address'} or {'time_window': '2020-2022'})"
            )
        if kind == "progression" and not valid_times:
            raise ValueError(
                "progression resolution requires valid_times "
                "(e.g., {'old': '2020-01-01..2022-06-30', 'new': '2022-07-01..'})"
            )

        # Verify the conflict exists + is currently 'open' (idempotency
        # could allow re-resolving but we'd need to handle state
        # transitions — for v0.1 we require open state)
        async with self._pool.tenant_session(tenant_id) as conn:
            row = await conn.fetchrow(
                """
                SELECT resolved_state FROM conflicts
                 WHERE tenant_id = $1 AND conflict_id = $2
                """,
                tenant_id, conflict_id,
            )
            if row is None:
                raise ValueError(f"conflict not found: {conflict_id}")
            if row["resolved_state"] != "open":
                raise ValueError(
                    f"conflict already resolved (state={row['resolved_state']}); "
                    "v0.1 does not support re-resolution"
                )

            from woxpas_storage import ConflictRepo
            repo = ConflictRepo(conn)
            await repo.resolve(
                tenant_id, conflict_id,
                kind=kind,
                winner=winner, loser=loser,
                discriminators=discriminators,
                valid_times=valid_times,
                note=note,
                resolved_by=resolved_by,
                resolution_method="manual",
            )

            # Read back the new state
            new_state_row = await conn.fetchrow(
                "SELECT resolved_state FROM conflicts "
                " WHERE tenant_id = $1 AND conflict_id = $2",
                tenant_id, conflict_id,
            )

        new_state = new_state_row["resolved_state"] if new_state_row else "unknown"
        log.info("conflict_engine.resolved",
                 tenant_id=str(tenant_id), conflict_id=str(conflict_id),
                 kind=kind, new_state=new_state)
        return ConflictResolutionResult(
            conflict_id=conflict_id, kind=kind, new_state=new_state,
        )
