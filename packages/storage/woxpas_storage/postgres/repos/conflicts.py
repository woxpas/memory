"""Conflicts repository — Phase 2.3 detection write path + Phase 3 retrieval reads.

The conflicts table comes from migration 007; migrations 019/020 add
dedup_key + the Phase 2.3 type values. This repo handles:

  - find_or_create — idempotent insert keyed on dedup_key, used by detection.
  - list_open      — operator + agent surfaces (not yet wired to UI; ready
                     for Phase 3 retrieval to query alongside claims).
  - resolve_*      — apply a resolution decision (pick / coexistence /
                     progression / dismissed). v0.2 sweep job will call these
                     too; signature kept generic on `resolved_by` / `resolved_via`.

resolved_value jsonb shape (see migrations/versions/019_conflicts.sql for the
canonical doc):

    {
      "kind":       "pick"|"coexistence"|"progression"|"dismissed",
      "winner":     "<id|null>",
      "loser":      "<id|null>",
      "discriminators": {"<id>": "<context>", ...},
      "valid_times":    {"<id>": "[lower,upper)", ...}
    }
"""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID

import asyncpg


def compute_dedup_key(conflict_type: str, ids: list[str]) -> str:
    """Stable dedup key for a (type, sorted-ids) pair. Re-running detection
    on the same conflict produces the same key → ON CONFLICT DO NOTHING
    leaves the existing row untouched."""
    sorted_ids = sorted(str(i) for i in ids)
    raw = f"{conflict_type}|" + "|".join(sorted_ids)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class ConflictRepo:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def find_or_create(
        self,
        tenant_id: UUID,
        *,
        conflict_type:      str,
        severity:           str,
        subject_entity_ids: list[UUID],
        predicate:          str | None,
        mention_refs:       list[dict[str, Any]],
        doc_refs:           list[UUID],
        detector:           str,
        detector_version:   str = "0.1.0",
        dedup_ids:          list[str] | None = None,
    ) -> tuple[UUID, bool]:
        """Insert a new conflict row, or return the existing one if its
        dedup_key matches.

        `dedup_ids` is the ordered list of identifiers (relation_ids,
        fact_ids, doc_ids — caller's choice, type-tagged in mention_refs)
        that uniquely identifies this conflict pair. We sort + hash to
        produce conflicts.dedup_key.

        Returns (conflict_id, was_inserted). was_inserted=False means the
        conflict was already detected on a prior run; no new row created.
        """
        if not dedup_ids:
            # Fall back to extracting IDs from mention_refs if caller didn't
            # provide them — best-effort; supports any of relation_id/fact_id/
            # doc_id keys.
            dedup_ids = [
                str(m.get("relation_id") or m.get("fact_id") or m.get("doc_id") or "")
                for m in mention_refs
            ]
        key = compute_dedup_key(conflict_type, dedup_ids)

        existing = await self._conn.fetchrow(
            "SELECT conflict_id FROM conflicts "
            "WHERE tenant_id = $1 AND dedup_key = $2",
            tenant_id, key,
        )
        if existing is not None:
            return existing["conflict_id"], False

        row = await self._conn.fetchrow(
            """
            INSERT INTO conflicts (
              tenant_id, type, severity, subject_entity_ids, predicate,
              mention_refs, doc_refs,
              resolved_state,
              detector, detector_version, dedup_key
            ) VALUES (
              $1, $2, $3, $4::uuid[], $5,
              $6::jsonb, $7::uuid[],
              'open',
              $8, $9, $10
            )
            ON CONFLICT (tenant_id, dedup_key) WHERE dedup_key IS NOT NULL
                 DO NOTHING
            RETURNING conflict_id
            """,
            tenant_id, conflict_type, severity,
            subject_entity_ids, predicate,
            mention_refs, doc_refs,
            detector, detector_version, key,
        )
        if row is None:
            # Race: another concurrent detection call won the insert. Refetch.
            existing = await self._conn.fetchrow(
                "SELECT conflict_id FROM conflicts "
                "WHERE tenant_id = $1 AND dedup_key = $2",
                tenant_id, key,
            )
            return existing["conflict_id"], False
        return row["conflict_id"], True

    async def list_open(
        self,
        tenant_id: UUID,
        *,
        subject_entity_id: UUID | None = None,
        conflict_types: list[str] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Read API for the agent + Phase 3 retrieval. Returns conflicts
        with resolved_state='open' for the given tenant, optionally
        filtered to a specific subject entity."""
        params: list[Any] = [tenant_id]
        clauses = ["tenant_id = $1", "resolved_state = 'open'"]
        if subject_entity_id is not None:
            params.append(subject_entity_id)
            clauses.append("$$ = ANY (subject_entity_ids)".replace("$$", f"${len(params)}"))
        if conflict_types:
            params.append(conflict_types)
            clauses.append(f"type = ANY (${len(params)}::text[])")
        params.append(limit)
        sql = f"""
            SELECT conflict_id, type, severity, subject_entity_ids,
                   predicate, mention_refs, doc_refs,
                   detected_at, detector, notes
              FROM conflicts
             WHERE {' AND '.join(clauses)}
             ORDER BY severity DESC, detected_at DESC
             LIMIT ${len(params)}
        """
        rows = await self._conn.fetch(sql, *params)
        return [dict(r) for r in rows]

    async def list_by_relation(
        self,
        tenant_id: UUID, relation_id: UUID,
    ) -> list[dict[str, Any]]:
        """Find all conflicts that reference a given relation. Used by
        Phase 3 retrieval: when serving a claim, surface any active conflict
        on it as resolution_provenance."""
        rows = await self._conn.fetch(
            """
            SELECT conflict_id, type, severity, resolved_state,
                   resolved_value, notes, predicate, detected_at, resolved_at
              FROM conflicts
             WHERE tenant_id = $1
               AND mention_refs @> jsonb_build_array(
                     jsonb_build_object('relation_id', $2::text)
                   )
             ORDER BY detected_at DESC
            """,
            tenant_id, relation_id,
        )
        return [dict(r) for r in rows]

    async def resolve(
        self,
        tenant_id: UUID, conflict_id: UUID,
        *,
        kind:              str,                  # 'pick' | 'coexistence' | 'progression' | 'dismissed'
        winner:            UUID | None = None,
        loser:             UUID | None = None,
        discriminators:    dict[str, str] | None = None,
        valid_times:       dict[str, str] | None = None,
        note:              str | None = None,
        resolved_by:       str = "user",        # user | agent | automatic
        resolution_method: str = "manual",       # manual | recency | rule | merge
    ) -> None:
        """Apply a resolution to a conflict. Doesn't itself archive or
        supersede the loser — caller (CLI / agent / sweep) does that as a
        separate operation, since archival is destructive-ish and benefits
        from being explicit.

        Maps `kind` to the existing resolved_state enum:
          pick / progression → human_resolved (or auto_resolved by sweep)
          coexistence        → living
          dismissed          → dismissed
        """
        if kind == "coexistence":
            new_state = "living"
        elif kind == "dismissed":
            new_state = "dismissed"
        else:
            new_state = "auto_resolved" if resolved_by == "automatic" else "human_resolved"

        resolved_value: dict[str, Any] = {"kind": kind}
        if winner is not None:
            resolved_value["winner"] = str(winner)
        if loser is not None:
            resolved_value["loser"] = str(loser)
        if discriminators:
            resolved_value["discriminators"] = discriminators
        if valid_times:
            resolved_value["valid_times"] = valid_times

        await self._conn.execute(
            """
            UPDATE conflicts
               SET resolved_state    = $3,
                   resolved_value    = $4::jsonb,
                   resolution_method = $5,
                   resolved_by       = $6,
                   resolved_at       = now(),
                   notes             = COALESCE($7, notes),
                   updated_at        = now()
             WHERE tenant_id = $1 AND conflict_id = $2
            """,
            tenant_id, conflict_id,
            new_state, resolved_value, resolution_method, resolved_by, note,
        )
