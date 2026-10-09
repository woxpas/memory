"""Graph traversal backend — entity-relation walks + path-finding.

Two operations:
  - traverse(seed)        — subgraph around a single entity (depth-limited BFS)
  - path_between(a, b)    — find the shortest path(s) connecting two entities

Both return a GraphView. traverse populates nodes + edges; path_between also
populates paths + target_entity_id.

Implementation: iterative BFS in Python (one SQL query per level). Easier
to debug than recursive CTEs, easier to apply per-level limits, equivalent
performance at our scale (10K-100K entities, <1M relations).

Gap fixes addressed (vs the v1-min build):
  1. Path output (paths field on GraphView, populated for path_between
     and optionally for traverse via with_paths flag)
  2. path_between(a, b) — bidirectional-friendly forward BFS with parent
     tracking. Returns up to max_paths shortest paths.
  3. Edge dedup helper (consolidate_edges_by_pair) — agent collapses
     A→B with multiple predicates into one display row when wanted.
  4. Per-level truncation surfaced via truncated_at_level dict. No silent
     dropping of neighbors.
  5. entity_types filter — only follow edges to listed types.
  6. Path confidence aggregation — min(edge confidences) along the path.

Pillar 4 — provenance preserved: each edge keeps its relation_id +
claim_text so the agent can quote sources for any connection.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any
from uuid import UUID

import structlog

from woxpas_retrieval.types import GraphEdge, GraphNode, GraphPath, GraphView

log = structlog.get_logger(__name__)


# Depth caps. Bigger = exponential blow-up risk on hub entities.
_MAX_TRAVERSE_DEPTH = 4
_MAX_PATH_DEPTH     = 6      # path_between can go deeper since results
                              # are bounded by the destination, not fan-out

_DEFAULT_LIMIT_PER_LEVEL = 50
_DEFAULT_MAX_PATHS       = 5


class GraphBackend:
    """Recursive entity-relation walk over `relations` table."""

    def __init__(self, pool) -> None:                     # type: ignore[no-untyped-def]
        self._pool = pool

    # ─── traverse(seed) ─────────────────────────────────────────────────────

    async def traverse(
        self,
        *,
        tenant_id:        UUID,
        root_entity_id:   UUID,
        depth:            int = 2,
        predicate_kinds:  list[str] | None = None,
        entity_types:     list[str] | None = None,
        limit_per_level:  int = _DEFAULT_LIMIT_PER_LEVEL,
        with_paths:       bool = False,
    ) -> GraphView | None:
        """Walk outward from `root_entity_id` up to `depth` hops.

        - Direction-agnostic (subject + object edges followed).
        - Cycle-safe via visited set.
        - Merge-aware: COALESCE(merged_into_entity_id, entity_id).
        - `entity_types` filter: only expand into entities of these types.
        - `predicate_kinds` filter: only follow relations of these kinds.
        - Per-level cap with truncation accounting (truncated_at_level).
        - `with_paths`: populate GraphView.paths with each non-root node's
          BFS-shortest path from root.
        """
        depth = max(0, min(depth, _MAX_TRAVERSE_DEPTH))
        cap = max(1, min(limit_per_level, _MAX_TRAVERSE_DEPTH * _DEFAULT_LIMIT_PER_LEVEL))

        async with self._pool.tenant_session(tenant_id) as conn:
            root_row = await conn.fetchrow(
                """
                WITH initial AS (
                  SELECT COALESCE(merged_into_entity_id, entity_id) AS canonical_id
                    FROM entities
                   WHERE tenant_id = $1 AND entity_id = $2
                )
                SELECT e.entity_id AS canonical_id,
                       e.canonical_name, e.type, e.mention_count
                  FROM initial i
                  JOIN entities e
                    ON e.entity_id = i.canonical_id AND e.tenant_id = $1
                """,
                tenant_id, root_entity_id,
            )
        if root_row is None:
            return None

        canonical_root: UUID = root_row["canonical_id"]

        # BFS state. parents[node] = (parent_node, edge_relation_id) so we
        # can reconstruct paths from root for any visited node.
        visited: set[UUID]   = {canonical_root}
        parents: dict[UUID, tuple[UUID, UUID] | None] = {canonical_root: None}
        nodes: dict[UUID, GraphNode] = {
            canonical_root: GraphNode(
                entity_id=canonical_root,
                canonical_name=root_row["canonical_name"],
                type=root_row["type"],
                mention_count=root_row.get("mention_count") or 0,
            ),
        }
        edges: list[GraphEdge] = []
        seen_edge_ids: set[UUID] = set()
        truncated: dict[int, int] = {}

        frontier: list[UUID] = [canonical_root]
        for level in range(depth):
            if not frontier:
                break
            level_edges, new_entities, dropped = await self._expand_one_level(
                tenant_id=tenant_id,
                seeds=frontier,
                predicate_kinds=predicate_kinds,
                entity_types=entity_types,
                visited=visited,
                limit=cap,
            )
            # De-dup by relation_id: a relation between two visited entities
            # gets fetched once at level N (when first endpoint is seed) and
            # again at level N+1 (when second endpoint is seed). Without this
            # dedup, edges list bloats and the agent sees duplicate predicates.
            for e in level_edges:
                if e.relation_id in seen_edge_ids:
                    continue
                seen_edge_ids.add(e.relation_id)
                edges.append(e)
            if dropped > 0:
                truncated[level + 1] = dropped

            next_frontier: list[UUID] = []
            for e in new_entities:
                eid = e["entity_id"]
                if eid in visited:
                    continue
                # Track parent: which edge brought us here.
                parent_id = e.get("via_parent_id")
                edge_id   = e.get("via_edge_id")
                if parent_id is not None and edge_id is not None:
                    parents[eid] = (parent_id, edge_id)
                visited.add(eid)
                next_frontier.append(eid)
                nodes[eid] = GraphNode(
                    entity_id=eid,
                    canonical_name=e["canonical_name"],
                    type=e["type"],
                    mention_count=e.get("mention_count") or 0,
                )
            frontier = next_frontier

        # Sort edges so duplicate-pair edges sit adjacent — makes agent-side
        # consolidation a one-pass scan.
        edges.sort(key=lambda e: (str(e.subject_entity_id),
                                  str(e.object_entity_id),
                                  e.predicate_canonical or ""))

        paths: list[GraphPath] = []
        if with_paths:
            edge_by_id = {e.relation_id: e for e in edges}
            for eid in nodes:
                if eid == canonical_root:
                    continue
                p = _reconstruct_path(canonical_root, eid, parents, edge_by_id)
                if p is not None:
                    paths.append(p)

        log.info("graph.traverse.ok",
                 root=str(canonical_root),
                 depth=depth, nodes=len(nodes), edges=len(edges),
                 truncated=truncated)

        return GraphView(
            root_entity_id=canonical_root,
            depth=depth,
            nodes=list(nodes.values()),
            edges=edges,
            paths=paths,
            truncated_at_level=truncated,
        )

    # ─── path_between(a, b) ────────────────────────────────────────────────

    async def path_between(
        self,
        *,
        tenant_id:        UUID,
        from_entity_id:   UUID,
        to_entity_id:     UUID,
        max_depth:        int = 4,
        max_paths:        int = _DEFAULT_MAX_PATHS,
        predicate_kinds:  list[str] | None = None,
        entity_types:     list[str] | None = None,
    ) -> GraphView:
        """Find shortest path(s) connecting two entities.

        Forward BFS from `from_entity_id` until `to_entity_id` is reached
        (or depth exhausted). Tracks ALL parents at the discovering level
        so multiple paths of the same shortest length surface.

        Returns a GraphView with:
          - root_entity_id    = canonical(from)
          - target_entity_id  = canonical(to)
          - found             = True if at least one path exists within depth
          - paths             = list[GraphPath], up to max_paths
          - nodes/edges       = the union of nodes/edges across the surfaced paths

        If no path exists (or depth too low), returns GraphView with
        found=False, empty paths/nodes/edges. Caller treats as "no connection
        within N hops" — agent can suggest increasing depth.
        """
        max_depth = max(1, min(max_depth, _MAX_PATH_DEPTH))
        max_paths = max(1, min(max_paths, 25))

        # Resolve both endpoints through merge chains.
        async with self._pool.tenant_session(tenant_id) as conn:
            rows = await conn.fetch(
                """
                SELECT e.entity_id AS in_id,
                       COALESCE(e.merged_into_entity_id, e.entity_id) AS canonical_id,
                       can.canonical_name, can.type, can.mention_count
                  FROM entities e
                  JOIN entities can
                    ON can.entity_id = COALESCE(e.merged_into_entity_id, e.entity_id)
                   AND can.tenant_id = $1
                 WHERE e.tenant_id = $1 AND e.entity_id = ANY ($2::uuid[])
                """,
                tenant_id, [from_entity_id, to_entity_id],
            )
        by_in: dict[UUID, dict[str, Any]] = {r["in_id"]: dict(r) for r in rows}
        if from_entity_id not in by_in or to_entity_id not in by_in:
            return GraphView(
                root_entity_id=from_entity_id, depth=max_depth,
                target_entity_id=to_entity_id, found=False,
            )
        canonical_a: UUID = by_in[from_entity_id]["canonical_id"]
        canonical_b: UUID = by_in[to_entity_id]["canonical_id"]

        # Trivial: a == b after merge resolution.
        if canonical_a == canonical_b:
            seed_a = by_in[from_entity_id]
            return GraphView(
                root_entity_id=canonical_a, depth=0,
                target_entity_id=canonical_b, found=True,
                nodes=[GraphNode(
                    entity_id=canonical_a,
                    canonical_name=seed_a["canonical_name"],
                    type=seed_a["type"],
                    mention_count=seed_a.get("mention_count") or 0,
                )],
                edges=[],
                paths=[GraphPath(
                    edge_ids=[], node_ids=[canonical_a], length=0,
                )],
            )

        # BFS with multi-parent tracking. parents[v] = list of (u, edge_id)
        # so we can reconstruct multiple shortest paths.
        parents_multi: dict[UUID, list[tuple[UUID, UUID]]] = defaultdict(list)
        distances: dict[UUID, int] = {canonical_a: 0}
        # Edges discovered during BFS (for the GraphView union).
        discovered_edges: dict[UUID, GraphEdge] = {}
        # Entity rows for the GraphView.nodes list.
        entity_rows: dict[UUID, dict[str, Any]] = {
            canonical_a: by_in[from_entity_id],
        }

        frontier = [canonical_a]
        found_at_level: int | None = None
        for level in range(max_depth):
            if found_at_level is not None and found_at_level < level:
                # We already found b at a shallower level — stop expanding.
                break
            edges, new_entities, _dropped = await self._expand_one_level(
                tenant_id=tenant_id,
                seeds=frontier,
                predicate_kinds=predicate_kinds,
                entity_types=entity_types,
                visited=set(distances),
                limit=_DEFAULT_LIMIT_PER_LEVEL * 4,    # generous for path queries
            )
            for e in edges:
                discovered_edges.setdefault(e.relation_id, e)
                # Direction-agnostic: parent could be either side of the edge.
                # Determine which endpoint is the "new" one (not in distances).
                a_side = e.subject_entity_id
                b_side = e.object_entity_id
                if a_side in distances and b_side not in distances:
                    parent, child = a_side, b_side
                elif b_side in distances and a_side not in distances:
                    parent, child = b_side, a_side
                else:
                    # Both visited or both new — degenerate; skip for parent
                    # tracking but the edge is still in discovered_edges.
                    continue
                if child not in distances:
                    distances[child] = distances[parent] + 1
                # Record this parent option (allows multi-paths of equal length).
                if distances.get(child) == distances[parent] + 1:
                    parents_multi[child].append((parent, e.relation_id))

            for ent in new_entities:
                entity_rows.setdefault(ent["entity_id"], ent)
                if ent["entity_id"] == canonical_b:
                    found_at_level = level + 1

            # Build next frontier from newly-discovered nodes only.
            frontier = [
                eid for eid in (e["entity_id"] for e in new_entities)
                if eid not in {canonical_a}
            ]

        if canonical_b not in distances:
            return GraphView(
                root_entity_id=canonical_a, depth=max_depth,
                target_entity_id=canonical_b, found=False,
                nodes=[GraphNode(
                    entity_id=canonical_a,
                    canonical_name=by_in[from_entity_id]["canonical_name"],
                    type=by_in[from_entity_id]["type"],
                ),
                GraphNode(
                    entity_id=canonical_b,
                    canonical_name=by_in[to_entity_id]["canonical_name"],
                    type=by_in[to_entity_id]["type"],
                )],
            )

        # Enumerate shortest paths (DFS through parent_multi back from b).
        paths = _enumerate_shortest_paths(
            target=canonical_b,
            source=canonical_a,
            parents_multi=parents_multi,
            distances=distances,
            edges=discovered_edges,
            max_paths=max_paths,
        )

        # Union of nodes/edges across discovered paths.
        used_nodes: set[UUID] = set()
        used_edges: set[UUID] = set()
        for p in paths:
            used_nodes.update(p.node_ids)
            used_edges.update(p.edge_ids)

        # Hydrate any used nodes we haven't loaded yet.
        missing = [nid for nid in used_nodes if nid not in entity_rows]
        if missing:
            async with self._pool.tenant_session(tenant_id) as conn:
                rows2 = await conn.fetch(
                    """
                    SELECT entity_id, canonical_name, type, mention_count
                      FROM entities
                     WHERE tenant_id = $1 AND entity_id = ANY ($2::uuid[])
                    """,
                    tenant_id, missing,
                )
            for r in rows2:
                entity_rows[r["entity_id"]] = dict(r)

        nodes_out: list[GraphNode] = []
        for nid in used_nodes:
            er = entity_rows.get(nid)
            if er is None:
                continue
            nodes_out.append(GraphNode(
                entity_id=nid,
                canonical_name=er["canonical_name"],
                type=er["type"],
                mention_count=er.get("mention_count") or 0,
            ))

        edges_out = [
            discovered_edges[eid] for eid in used_edges
            if eid in discovered_edges
        ]

        log.info("graph.path_between.ok",
                 a=str(canonical_a), b=str(canonical_b),
                 paths=len(paths),
                 shortest_length=paths[0].length if paths else None)

        return GraphView(
            root_entity_id=canonical_a,
            depth=found_at_level or max_depth,
            target_entity_id=canonical_b,
            found=True,
            nodes=nodes_out,
            edges=edges_out,
            paths=paths,
        )

    # ─── Per-level expansion (shared) ───────────────────────────────────────

    async def _expand_one_level(
        self,
        *,
        tenant_id:        UUID,
        seeds:            list[UUID],
        predicate_kinds:  list[str] | None,
        entity_types:     list[str] | None,
        visited:          set[UUID],
        limit:            int,
    ) -> tuple[list[GraphEdge], list[dict[str, Any]], int]:
        """One BFS step. Returns (edges, new_entities, dropped_count).
        `dropped_count` is the number of additional candidates beyond `limit`
        that the level had — for the truncated_at_level surface."""
        if not seeds:
            return [], [], 0

        async with self._pool.tenant_session(tenant_id) as conn:
            # First, COUNT how many candidates this level has so we can
            # report truncation accurately. Cheap aggregate.
            count_sql = """
                WITH visible_predicates AS (
                  SELECT canonical, kind FROM predicates
                   WHERE tenant_id IS NULL OR tenant_id = $1
                )
                SELECT COUNT(*) AS n
                  FROM relations r
                  JOIN entities s ON s.entity_id = r.subject_entity_id
                                 AND s.tenant_id = r.tenant_id
                  LEFT JOIN entities o ON o.entity_id = r.object_entity_id
                                      AND o.tenant_id = r.tenant_id
                  LEFT JOIN visible_predicates vp ON vp.canonical = r.predicate_canonical
                 WHERE r.tenant_id = $1
                   AND r.archived_at IS NULL
                   AND r.object_entity_id IS NOT NULL
                   AND (
                     COALESCE(s.merged_into_entity_id, r.subject_entity_id) = ANY ($2::uuid[])
                     OR COALESCE(o.merged_into_entity_id, r.object_entity_id) = ANY ($2::uuid[])
                   )
                   AND ($3::text[] IS NULL OR vp.kind = ANY ($3::text[])
                                           OR r.predicate_kind = 'open')
                   AND ($4::text[] IS NULL
                        OR s.type = ANY ($4::text[])
                        OR o.type = ANY ($4::text[]))
            """
            count_row = await conn.fetchrow(
                count_sql, tenant_id, list(seeds),
                predicate_kinds, entity_types,
            )
            total_candidates = int(count_row["n"]) if count_row else 0

            sql = """
                WITH visible_predicates AS (
                  SELECT canonical, kind FROM predicates
                   WHERE tenant_id IS NULL OR tenant_id = $1
                ),
                level_edges AS (
                  SELECT r.relation_id,
                         COALESCE(s.merged_into_entity_id, r.subject_entity_id) AS subj_id,
                         COALESCE(o.merged_into_entity_id, r.object_entity_id)  AS obj_id,
                         s.type AS subj_type, o.type AS obj_type,
                         r.predicate, r.predicate_canonical, r.predicate_kind,
                         r.claim_text, r.confidence,
                         vp.kind AS canonical_kind
                    FROM relations r
                    JOIN entities s ON s.entity_id = r.subject_entity_id
                                   AND s.tenant_id = r.tenant_id
                    LEFT JOIN entities o ON o.entity_id = r.object_entity_id
                                        AND o.tenant_id = r.tenant_id
                    LEFT JOIN visible_predicates vp ON vp.canonical = r.predicate_canonical
                   WHERE r.tenant_id = $1
                     AND r.archived_at IS NULL
                     AND r.object_entity_id IS NOT NULL
                     AND (
                       COALESCE(s.merged_into_entity_id, r.subject_entity_id) = ANY ($2::uuid[])
                       OR COALESCE(o.merged_into_entity_id, r.object_entity_id) = ANY ($2::uuid[])
                     )
                     AND ($3::text[] IS NULL OR vp.kind = ANY ($3::text[])
                                             OR r.predicate_kind = 'open')
                     AND ($4::text[] IS NULL
                          OR s.type = ANY ($4::text[])
                          OR o.type = ANY ($4::text[]))
                )
                SELECT *
                  FROM level_edges
                 ORDER BY confidence DESC NULLS LAST, relation_id
                 LIMIT $5
            """
            edge_rows = await conn.fetch(
                sql, tenant_id, list(seeds), predicate_kinds, entity_types, limit,
            )

            # Hydrate new endpoints (those NOT in seeds OR visited).
            seed_set = set(seeds)
            new_endpoint_ids: set[UUID] = set()
            for r in edge_rows:
                for col in ("subj_id", "obj_id"):
                    eid = r[col]
                    if eid is None:
                        continue
                    if eid in seed_set or eid in visited:
                        continue
                    new_endpoint_ids.add(eid)

            ent_rows: list[dict[str, Any]] = []
            if new_endpoint_ids:
                rows = await conn.fetch(
                    """
                    SELECT entity_id, canonical_name, type, mention_count
                      FROM entities
                     WHERE tenant_id = $1 AND entity_id = ANY ($2::uuid[])
                    """,
                    tenant_id, list(new_endpoint_ids),
                )
                ent_rows = [dict(r) for r in rows]

        edges_out: list[GraphEdge] = []
        # Track via_edge_id / via_parent_id on each new entity for parent
        # tracking in traverse (lets us reconstruct paths root→node).
        first_via: dict[UUID, tuple[UUID, UUID]] = {}
        for r in edge_rows:
            subj = r["subj_id"]
            obj  = r["obj_id"]
            if subj is None or obj is None:
                continue
            edges_out.append(GraphEdge(
                relation_id=r["relation_id"],
                subject_entity_id=subj,
                object_entity_id=obj,
                predicate_canonical=r["predicate_canonical"],
                predicate_kind=r["canonical_kind"] or r["predicate_kind"],
                claim_text=r["claim_text"],
                confidence=float(r["confidence"]) if r["confidence"] is not None else None,
            ))
            # First edge that reaches a "new" endpoint defines its BFS parent.
            for endpoint, opposite in ((obj, subj), (subj, obj)):
                if endpoint in seed_set or endpoint in visited:
                    continue
                first_via.setdefault(endpoint, (opposite, r["relation_id"]))

        for e in ent_rows:
            via = first_via.get(e["entity_id"])
            if via is not None:
                e["via_parent_id"] = via[0]
                e["via_edge_id"]   = via[1]

        dropped = max(0, total_candidates - len(edge_rows))
        return edges_out, ent_rows, dropped


# ─── Path reconstruction helpers ────────────────────────────────────────────

def _reconstruct_path(
    root: UUID, target: UUID,
    parents: dict[UUID, tuple[UUID, UUID] | None],
    edge_by_id: dict[UUID, GraphEdge],
) -> GraphPath | None:
    """Walk parent pointers from target back to root, returning a GraphPath.
    Returns None if target is unreachable (shouldn't happen in BFS-discovered
    nodes but defensive)."""
    edge_ids: list[UUID] = []
    node_ids: list[UUID] = [target]
    cur = target
    while cur != root:
        p = parents.get(cur)
        if p is None:
            return None
        parent_node, edge_id = p
        edge_ids.append(edge_id)
        node_ids.append(parent_node)
        cur = parent_node
    edge_ids.reverse()
    node_ids.reverse()
    confs = [edge_by_id[eid].confidence for eid in edge_ids
             if eid in edge_by_id and edge_by_id[eid].confidence is not None]
    total_conf = min(confs) if confs else None
    return GraphPath(
        edge_ids=edge_ids, node_ids=node_ids,
        length=len(edge_ids),
        total_confidence=total_conf,
    )


def _enumerate_shortest_paths(
    *,
    target:        UUID,
    source:        UUID,
    parents_multi: dict[UUID, list[tuple[UUID, UUID]]],
    distances:     dict[UUID, int],
    edges:         dict[UUID, GraphEdge],
    max_paths:     int,
) -> list[GraphPath]:
    """DFS through multi-parent map back from target. Yields all shortest
    paths up to `max_paths`. Stops early once cap hit."""
    # Iterative DFS to avoid recursion depth on long paths.
    paths: list[GraphPath] = []

    def dfs(node: UUID, edge_acc: list[UUID], node_acc: list[UUID]) -> None:
        if len(paths) >= max_paths:
            return
        if node == source:
            edge_ids = list(reversed(edge_acc))
            node_ids = list(reversed([*node_acc, source]))
            confs = [edges[eid].confidence for eid in edge_ids
                     if eid in edges and edges[eid].confidence is not None]
            paths.append(GraphPath(
                edge_ids=edge_ids,
                node_ids=node_ids,
                length=len(edge_ids),
                total_confidence=min(confs) if confs else None,
            ))
            return
        for parent, edge_id in parents_multi.get(node, []):
            if len(paths) >= max_paths:
                return
            dfs(parent, [*edge_acc, edge_id], [*node_acc, node])

    dfs(target, [], [])
    paths.sort(key=lambda p: (p.length,
                              -(p.total_confidence or 0.0)))
    return paths


# ─── Edge consolidation helper ──────────────────────────────────────────────

def consolidate_edges_by_pair(
    view: GraphView,
) -> dict[tuple[UUID, UUID], list[GraphEdge]]:
    """Group GraphView.edges by unordered (entity_a, entity_b). Agent UI
    uses this to display "Ada → Alice (3 connections: friend_of, knows,
    collaborates_with)" rather than three separate edges.

    Key is a stable tuple — the lower entity_id (by string) goes first so
    the key is direction-agnostic. Edges within each group preserve their
    original direction in subject_entity_id / object_entity_id.
    """
    groups: dict[tuple[UUID, UUID], list[GraphEdge]] = defaultdict(list)
    for e in view.edges:
        a, b = e.subject_entity_id, e.object_entity_id
        key = (a, b) if str(a) < str(b) else (b, a)
        groups[key].append(e)
    return dict(groups)
