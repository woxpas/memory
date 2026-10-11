# Design: per-member "me" resolution

**Status:** Plan for review · **Depends on:** team-memory Phase A0–A2 (shipped in branch) · **Belongs to:** the A-series follow-ups ([[team-memory]])

## 1. The problem

When the extraction pipeline turns a document into facts, first-person references — **"me" / "I" / "my"**, plus the owner's name aliases — are resolved to a single self-entity so that "I live in Berlin" becomes a durable identity fact about *the user*, not about a stray new entity.

Today that target is **the tenant's** self-entity (`tenants.self_entity_id`), set in `extract_facts/stage.py` (owner-name aliases) and `extract_facts/prompt.py` ("I"/"my" → the tenant self at persistence).

In a **team** (Phase A), a tenant has many members, each with their own self-entity (`tenant_members.self_entity_id`, created in A0/A2b). So a document Ada captured that says *"I moved to Berlin"* must attach that fact to **Ada's** self-entity — not a shared team self-entity, and certainly not Bob's. Getting this wrong cross-contaminates members' identities, which is both a correctness bug and a privacy problem (Pillar 3).

## 2. Why it isn't already solved by A1/A2

A1/A2 attribute *rows* to a member via the `app.member_id` session GUC, set per request/tool-call. But the **extraction pipeline runs decoupled**: the worker consumes the transactional outbox asynchronously, in its own process, with **no member session context** — so `current_member_id()` is None there. The "me"-target therefore can't come from the ambient session the way A1 attribution does.

The good news: **A1 already persisted the capturer on the document itself** — `documents.captured_by_member_id`. That column is the durable carrier that survives the hop from the ingesting request into the detached pipeline. The pipeline just has to *read* it.

## 3. The design

**Resolve "me" against the capturing member's self-entity, read from the document.**

1. **Thread the capturer into the extraction stage.** When `extract_facts` loads the document it is processing, also load `captured_by_member_id`. Resolve that member's `self_entity_id` via `tenant_members`. Call it the document's **`me_entity_id`**.
2. **Fallback ladder** for `me_entity_id`:
   - the capturing member's `self_entity_id` (the normal team case); else
   - `tenants.self_entity_id` — the team self-entity (single-person installs, agent/system-captured docs, or a member with no self-entity yet).
   This keeps single-user and pre-team behavior identical (the team self *is* the only self).
3. **First-person mapping** ("I"/"me"/"my") → `me_entity_id` instead of the tenant self. This is a one-line change of target in the persistence step; the extraction prompt is unchanged (the model still just marks first-person facts — resolution stays server-side, never model-controlled: Pillar 3).
4. **Owner-name aliases** become **per-member**: the alias set that maps a proper name to `me_entity_id` is built from the **capturing member's** `display_name` (and any future member aliases), not a tenant-wide owner name. Other members' names remain ordinary global entities (resolved by the normal entity resolver), so "Bob shipped the release" in Ada's note links to Bob's *entity*, not a self-fact.

### Data flow

```
ingest (request, has member) ──▶ documents.captured_by_member_id   [A1, done]
                                        │  (durable, survives the async hop)
                                        ▼
outbox ──▶ extract_facts worker: load doc.captured_by_member_id
                                        │
                                        ▼
            me_entity_id = member.self_entity_id  ?? tenant.self_entity_id
                                        │
         "I"/"my" + capturer's name aliases ──▶ me_entity_id
         other proper names ──▶ normal entity resolution (global)
```

## 4. Edge cases & rules

| Case | Resolution |
|---|---|
| Doc captured by a member (normal) | "me" → that member's self-entity |
| Doc with `captured_by_member_id` NULL (single-user, legacy, agent/system) | "me" → team self-entity (unchanged behavior) |
| Member has no self-entity row yet | fall back to team self-entity; log once |
| Member removed after capture (`ON DELETE SET NULL`) | column goes NULL → team-self fallback; historical facts already attached stay put |
| Another member's name appears ("Bob …") | ordinary global entity — never a self-fact |
| Quoted/third-party "I" inside the text (e.g. an email from someone else) | out of scope for v1 — same limitation exists today; note for a later "speaker attribution" pass |
| Shared doc visible to several members (Phase B) | attribution follows `captured_by_member_id` (the capturer), not the viewer |

## 5. Schema & code touch-points

- **No new migration.** `documents.captured_by_member_id` (033) and `tenant_members.self_entity_id` (032) already exist.
- `packages/pipeline/woxpas_pipeline/extract_facts/stage.py` — load `captured_by_member_id`; compute `me_entity_id` with the fallback ladder; build the per-member alias set.
- `packages/pipeline/woxpas_pipeline/extract_facts/prompt.py` / persistence — target `me_entity_id` for first-person facts (currently the tenant self).
- A small resolver helper: `resolve_me_entity(conn, tenant_id, captured_by_member_id) -> UUID` (member self, else tenant self). Unit-testable in isolation.

## 6. Pillar check

- **Accuracy** — identity facts land on the right person; cross-member contamination is impossible by construction (capturer carried on the row, resolved server-side).
- **Security/privacy** — a member's first-person facts never attach to another member; resolution is never model- or client-controlled (same hardening as today's tenant-self mapping).
- **Speed** — one extra indexed lookup per document in a stage that already does many; negligible, and cacheable per worker run.
- **Sovereignty/compat** — single-person installs and existing data are byte-for-byte unchanged (the fallback ladder collapses to the tenant self).

## 7. Phasing

- **M1** — `resolve_me_entity` helper + unit tests (member self / fallback / NULL).
- **M2** — wire first-person target + per-member aliases in `extract_facts`; backfill not required (historical facts keep their existing tenant-self attachment).
- **M3** — (optional, later) in-text speaker attribution for quoted first-person — its own design.

## 8. Open questions

1. **Re-extraction of pre-team docs.** When a single-person install becomes a team, old docs are attached to the (now team) self-entity. Do we offer a one-off "claim my history" that re-points a member's historical self-facts? Lean: no automatic migration; provide an admin tool later if asked.
2. **Agent-captured docs on behalf of a member.** An agent ingesting for Ada sets `captured_by_member_id = Ada` (A1 already does this when her session drives it), so "me" resolves to Ada — correct. Confirm no agent path captures with a member session that isn't the human's.
