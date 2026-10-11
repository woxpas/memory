# Design: private content (true document privacy)

**Status:** Plan for review · **Depends on:** team-memory A4 (private scopes) · **Addresses:** [[team-memory]] §10 risk #3

## 1. What A4 does and does NOT do

A4 (migration 035) makes a **private scope** and its **membership labels** owner-only: a non-owner can't see the scope exists or that a document is grouped under it. That is *private organization*.

A4 deliberately does **not** hide the underlying content. A document grouped into a private scope still has:
- a visible `documents` row (tenant-wide RLS is scope-agnostic),
- searchable `chunks` + their **Qdrant vectors** (recall/ask will surface them),
- extracted `entities` / `relations` merged into the shared knowledge graph.

So today a non-owner can still reach "private" content via recall, direct document read, or the graph. Hiding the scope label while leaving the content reachable would be **false privacy** — this doc specifies how to make it real, coherently, across every read path. Half-measures are worse than none here.

## 2. The core problem

Privacy must hold across **four** surfaces, two of which are outside Postgres RLS:

| Surface | Mechanism | Hard part |
|---|---|---|
| `documents` rows | Postgres RLS | a doc can be in many scopes → needs an effective-visibility rule |
| `chunks` / `spans` | Postgres RLS | must match the doc's effective visibility |
| **Qdrant vectors** | app-layer filter | Qdrant has no RLS — recall must filter by payload |
| **KG** (`entities`/`relations`/`ideas`) | ? | a private doc's facts merge into a *shared* graph — the deepest issue |

## 3. Proposed model: document-level visibility

Rather than derive privacy from scope membership on every read (expensive, and a doc in both a team and a private scope is ambiguous), **stamp visibility on the document itself**, mirroring scopes:

```sql
ALTER TABLE documents
  ADD COLUMN visibility text NOT NULL DEFAULT 'team' CHECK (visibility IN ('team','private')),
  ADD COLUMN owner_member_id uuid REFERENCES tenant_members(member_id) ON DELETE SET NULL;
```

Effective rule: a document is **private** iff it is assigned to a private scope (propagate at assignment time), or explicitly marked private at capture. `owner_member_id` = the private scope's owner / the capturer. Precedence: once private, assignment to a team scope does **not** auto-downgrade — downgrading is an explicit owner action (monotonic, like `sensitivity` already is in this codebase).

### Enforcement per surface

- **documents / chunks / spans**: the same RLS predicate used for private scopes (owner, admin/owner role, or no-member session), applied to each table. `chunks`/`spans` carry `tenant_id` already; add the predicate via a join to `documents` visibility, or denormalize `visibility`/`owner_member_id` onto chunks for a cheap per-row check (preferred for recall latency — Pillar 1).
- **Qdrant vectors**: write `visibility` + `owner_member_id` into each point's payload at embed time. The retrieval engine adds a filter to every search: `visibility == 'team' OR owner_member_id == <app.member_id> OR <no member / admin>`. This is the one genuinely new code path (vectors are outside RLS); it must be applied in **every** Qdrant query (chunk search, scope centroid, summary search) — centralize it in one filter builder so no call site can forget.
- **KG**: the hardest. Options, in increasing fidelity:
  1. **Exclude private-derived facts from the shared graph** — don't merge entities/relations extracted from a private doc into the tenant graph; keep them in a per-member partition (tag derived rows with origin `visibility`/`owner_member_id`, filter graph reads). Safe, but a member's private knowledge can't enrich their own graph queries unless the reader is them.
  2. **Member-scoped graph overlay** — shared graph for team docs + a private overlay per member merged only for that member at read time. More useful, more complex.
  Lean: start with option 1 (exclude), because a leak here is a privacy breach and option 1 is the conservative default.

## 4. Phasing

- **P1 — documents + chunks/spans RLS.** Add `visibility`/`owner_member_id` to `documents`; propagate on private-scope assignment; denormalize onto `chunks`; RLS predicates. Direct document-read and chunk reads become private-safe.
- **P2 — retrieval (Qdrant) filter.** Payload fields at embed time + a single centralized filter in the retrieval engine, applied to all vector searches. Backfill payloads for existing points. **Recall/ask become private-safe** — this is the surface that matters most.
- **P3 — KG partition (option 1).** Tag derived entity/relation/idea rows with origin visibility; exclude private-derived from shared graph reads. Revisit option 2 if members need private-graph enrichment.
- **P4 — audit + tests.** A cross-member leakage test battery: for each read surface (document read, recall, ask, graph, timeline, resurfacing context), assert a non-owner cannot reach private content.

## 5. Pillar check & risks

- **Security/privacy** — the whole point; the design is conservative (exclude-by-default in the graph). The Qdrant filter is the critical new path — centralize and test it hard, since a missed call site is a silent leak.
- **Speed** — denormalizing `visibility`/`owner_member_id` onto chunks (and Qdrant payload) keeps the hot recall path to a single indexed predicate / payload filter, not a join-per-row.
- **Accuracy** — monotonic private→team downgrade (explicit only) avoids accidental exposure via re-scoping.
- **Sovereignty** — single-user installs: everything is `team` + no member context → all predicates pass → unchanged.

### Open questions
1. **Downgrade semantics** — who may un-private a document, and does it re-expose historical derived facts? (Lean: owner + admin; derived facts stay excluded unless re-extracted.)
2. **Shared-then-private** — a team doc later moved to a private scope: do we retroactively purge its already-merged graph facts? (Hard; likely "no, going forward only" + a documented caveat.)
3. **Backfill** — existing Qdrant points need payload backfill before P2 enforcement turns on, or they're invisible/over-visible during migration. Sequence carefully.
