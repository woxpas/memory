# Design: Team / collaborator memory

**Status:** Draft for review · **Scope:** Phase A (build now) with Phase B (federation) sketched · **Target:** a future minor release

Woxpas today is one memory space per person. This document designs **multi-collaborator memory** so an organisation (or household, or project team) can share a single sovereign memory that many people read, write, and are attributed in — without weakening the encryption-at-rest or RLS isolation the project is built on.

It is deliberately staged:

- **Phase A — Shared team workspace (this doc's focus).** The team *is* a tenant: one shared memory pool, one shared DEK wrapped once per member, member identities, attribution, roles, and optional private scopes. Reuses the existing RLS boundary; additive and backward-compatible.
- **Phase B — Federated sovereign memories (sketched at the end).** Each person keeps their own tenant/DEK and teams form by selectively sharing scopes across tenants. Phase A is a prerequisite — it builds the member-identity and attribution model that Phase B's cross-tenant sharing needs.

---

## 1. What exists today (the starting point)

| Concern | Current state | File |
|---|---|---|
| Security boundary | `tenant` = one person's entire memory. Everything is RLS-isolated on `app.tenant_id`. | `migrations/versions/009_rls_grants.sql` |
| Encryption | One DEK per tenant, wrapped under a single passphrase (Argon2id → KEK → DEK). `dek_version` already anticipates rotation. | `001_tenants_and_crypto.sql` |
| Auth | Resolves **only** a `tenant_id`. JWT mode reads `woxpas_tenant_id` / `sub`. No concept of an individual user within a tenant. | `packages/api/woxpas_api/auth.py` |
| Attribution | `captured_by ∈ {user, agent, system}` — a coarse *role*, not a person. | `004_documents.sql` |
| Identity | One "self" entity per tenant (`tenants.self_entity_id`); `identity_card(entity_id, …)` is **already parameterized by entity**. | `012_extraction_foundation.sql` |
| Preferences | `resurface_user_preferences` — exactly one row per tenant. | `022_resurfacing_foundation.sql` |
| Unlock | One passphrase per tenant (boot daemon or `WOXPAS_DEV_DEK_HEX`). | `packages/boot/` |

**Key insight:** the isolation machinery for multiple parties already exists — it is simply configured as "one tenant = one human." Phase A re-interprets a tenant as a *team* and adds a membership/identity/attribution layer **inside** the existing boundary. No RLS holes are punched.

---

## 2. Core model change

```
            BEFORE                              AFTER (Phase A)
   ┌───────────────────────┐          ┌──────────────────────────────┐
   │ tenant  = one person   │          │ tenant  = one TEAM            │
   │  ├ one DEK (1 pass)    │          │  ├ one shared DEK             │
   │  ├ one self-entity     │          │  │    wrapped once per member │
   │  ├ one preferences row │          │  ├ tenant_members (N people)  │
   │  └ captured_by: role   │          │  │    ├ role, self-entity     │
   └───────────────────────┘          │  │    ├ own passphrase→KEK     │
                                       │  │    └ own preferences row    │
                                       │  └ actor_member_id everywhere  │
                                       └──────────────────────────────┘
```

A single-person install is just a team of one — the existing behaviour is the degenerate case, which is what keeps the migration backward-compatible.

---

## 3. Schema changes (migrations 032+)

New migrations, each additive and RLS-aware. Numbers are indicative (current head is `031`).

### 032 — `tenant_members` (the membership + per-member key table)

```sql
CREATE TABLE tenant_members (
  member_id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id            uuid NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,

  -- who
  display_name         text NOT NULL,
  external_subject     text,                      -- JWT 'sub' / IdP id; unique per tenant
  self_entity_id       uuid REFERENCES entities(entity_id) ON DELETE SET NULL,

  -- role / access
  role                 text NOT NULL DEFAULT 'member'
                         CHECK (role IN ('owner','admin','member','viewer')),
  status               text NOT NULL DEFAULT 'active'
                         CHECK (status IN ('active','suspended','removed')),

  -- per-member envelope: the SAME tenant DEK, wrapped under THIS member's KEK
  passphrase_verifier  bytea NOT NULL,            -- Argon2id hash
  kdf_params           jsonb NOT NULL,
  encrypted_dek        bytea NOT NULL,            -- tenant DEK wrapped under member KEK
  encrypted_dek_iv     bytea NOT NULL,
  encrypted_dek_tag    bytea NOT NULL,
  dek_version          int  NOT NULL DEFAULT 1,   -- must track tenants.dek_version

  invited_by           uuid REFERENCES tenant_members(member_id),
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),

  UNIQUE (tenant_id, external_subject)
);
-- Standard tenant RLS applies (members only see their own team's roster):
SELECT woxpas_apply_tenant_rls('tenant_members'::regclass);
```

The per-member envelope columns are the crux: **the tenant has one DEK; each member stores their own wrapped copy** of it. Unlock = any member derives their KEK from their passphrase and unwraps the shared DEK. This reuses the exact envelope primitive in `001`, just N times.

### 033 — attribution columns (additive, nullable → backfilled)

```sql
ALTER TABLE documents            ADD COLUMN captured_by_member_id uuid REFERENCES tenant_members(member_id);
ALTER TABLE resurface_observations ADD COLUMN actor_member_id    uuid REFERENCES tenant_members(member_id);
-- decisions, ideas, tasks_deadlines, prepared actions: same additive actor_member_id
```

`captured_by` (the role enum) stays for backward compatibility; `*_member_id` adds the *person*. Views (`v_documents`, etc.) expose the new column at the end of the SELECT list — the same CREATE-OR-REPLACE discipline migration `031` used.

### 034 — per-member preferences & identity

- Re-key `resurface_user_preferences` from `PRIMARY KEY (tenant_id)` to `(tenant_id, member_id)` — each collaborator gets their own answer-length / tone / tool-use profile. A `member_id IS NULL` row remains the team default (for agents acting without a member context).
- `tenants.self_entity_id` stays as the **team's** self-entity (the org itself). Each member's personal self-entity lives on `tenant_members.self_entity_id`. `identity_card()` already takes an `entity_id`, so no function change is needed — the MCP layer just passes the acting member's self-entity.

### 035 — scope visibility (optional private scopes within a team)

```sql
ALTER TABLE scopes ADD COLUMN visibility text NOT NULL DEFAULT 'team'
  CHECK (visibility IN ('team','private'));
ALTER TABLE scopes ADD COLUMN owner_member_id uuid REFERENCES tenant_members(member_id);
```

A `private` scope is visible only to its `owner_member_id` (and admins). This is the bridge toward Phase B *inside* a single tenant: a member can keep some memory personal while sharing the rest with the team. Enforced by an **additional** RLS predicate on scope-joined reads (see §5), not by a new isolation axis.

---

## 4. Crypto: shared DEK lifecycle

The security pillar is load-bearing here, so the key operations are specified explicitly.

- **Create team.** Generate the tenant DEK once (as today). The founding owner's passphrase wraps it into their `tenant_members` row.
- **Add a member.** An existing member who can already unwrap the DEK (owner/admin) does so in memory, then re-wraps it under the new member's freshly derived KEK and writes their `tenant_members` row. The plaintext DEK never leaves the unlocking process. *(Invite flow: admin unlocks → sets a one-time passphrase or accepts the invitee's public wrap — see open questions.)*
- **Unlock.** Any active member unlocks the shared DEK with their own passphrase. The boot daemon (`woxpas-unlock`) gains a `--member` selector; the Unix-socket DEK service is otherwise unchanged.
- **Remove a member.** Delete (or `status='removed'`) their `tenant_members` row — they can no longer unwrap. For **forward secrecy** (so a copied-away wrapped-DEK can't be used after removal), rotate: generate DEK v2, re-encrypt sensitive fields, bump `tenants.dek_version` and every remaining member's wrapped copy. `dek_version` already exists for exactly this; rotation is the expensive-but-bounded path and can be a follow-up job.
- **Recovery.** The existing `encrypted_dek_recovery` columns stay as a tenant-level break-glass, held by the owner.

---

## 5. Auth & access control

### JWT carries both team and user

```
claims = {
  "woxpas_tenant_id": "<team uuid>",   // → app.tenant_id   (RLS, unchanged)
  "sub":              "<user subject>", // → tenant_members.external_subject → member_id
  "aud": ..., "iss": ...
}
```

`_jwt_resolve` (in `auth.py`) is extended to, after resolving the tenant, look up the `tenant_members` row for `(tenant_id, sub)`, assert `status='active'`, and set **two** session vars:

```sql
SET app.tenant_id = <team>;    -- existing RLS key (team isolation — unchanged)
SET app.member_id = <member>;  -- NEW: attribution + private-scope ACL
```

- **RLS stays tenant-scoped.** Team isolation is unchanged; all existing policies keep working verbatim. Members of a team share the team's rows — which is the whole point of Phase A.
- **Private scopes** add one predicate on scope-joined reads: a `private` scope row is visible only when `owner_member_id = current_setting('app.member_id')::uuid` (admins bypass). This is the single new access axis and it lives *inside* the tenant boundary.
- **Roles** gate writes at the API/MCP layer: `viewer` → read-only tools only; `member` → read/write own + team; `admin` → member management + sensitivity downgrades; `owner` → everything incl. recovery and DEK rotation.
- **dev / single_tenant_local** modes are unchanged (still loopback-only, single implicit member = the owner). Teams require `jwt` mode — which the "networked deployment" section of `GETTING_STARTED` already mandates for non-loopback use.

---

## 6. MCP & API surface

- **MCP:** add `WOXPAS_MCP_MEMBER_ID` alongside the existing `WOXPAS_MCP_TENANT_ID`. Each collaborator runs their own MCP server bound to the shared tenant **and** their member id, so observations, prepared actions, and identity cards are attributed to them. Fails closed if the member isn't an active member of that tenant. No tool signatures change — the member, like the tenant, is ambient, never a tool argument.
- **API:** new admin routes (owner/admin only): `POST /members` (invite), `GET /members`, `PATCH /members/{id}` (role/status), `DELETE /members/{id}` (+ optional rotation). Every write route records `actor_member_id` from `app.member_id`.
- **Resurfacing:** the dreaming worker keys preferences/patterns per member where a member context exists, and per team for team-wide signals (open conflicts, dormant ideas).

---

## 7. CLI surface

```bash
woxpas-cli init-team   --display-name "Acme Research"        # create team tenant + founding owner
woxpas-cli add-member  --tenant-id <t> --display-name "Ada" --role member   # admin unlocks + re-wraps DEK
woxpas-cli list-members --tenant-id <t>
woxpas-cli set-role    --tenant-id <t> --member <m> --role admin
woxpas-cli remove-member --tenant-id <t> --member <m> [--rotate-dek]
woxpas-cli unlock      --member <m>                          # per-member unlock
```

`init-tenant` stays as the single-person path (and internally becomes `init-team` with one owner).

---

## 8. Migration path for existing single-tenant installs (must not break)

1. Migrations 032–034 run. `tenant_members` starts empty.
2. A **data backfill** (idempotent, in 032's tail or a one-shot `woxpas-cli backfill-members`) creates one `owner` member per existing tenant, re-using the tenant's existing passphrase envelope: copy `passphrase_verifier`, `kdf_params`, `encrypted_dek*` from `tenants` into the owner's `tenant_members` row, and point `self_entity_id` at the existing `tenants.self_entity_id`.
3. `captured_by_member_id` / `actor_member_id` backfill to that owner for historical rows (or stay NULL = "pre-teams, owner-implied").
4. Existing `dev`/`single_tenant_local` flows keep working untouched — the lone owner is resolved implicitly.

Net effect: every current install silently becomes a team of one, with zero behaviour change until a second member is added.

---

## 9. Phase B — federated sovereign memories (sketch, later)

Phase B keeps each person's **own tenant/DEK** and shares *across* tenants:

- A `shared_scopes` table maps `(owner_tenant_id, scope_id) → grantee_tenant_id` with a role.
- Shared content is encrypted under a **per-share sub-key** (the owner wraps a content key for the grantee's public key), so sharing never exposes the owner's tenant DEK.
- Reads of shared scopes go through a cross-tenant path that checks the grant and decrypts with the share sub-key — the one place the hard tenant-RLS boundary is deliberately, narrowly crossed.

Phase A is the prerequisite: its `tenant_members`, `actor_member_id`, and per-member identity/preferences are exactly the model Phase B needs to answer "who, in which memory, may see this." Phase B is a significantly larger effort (asymmetric crypto, key distribution, a cross-tenant query path) and should be its own design doc.

---

## 10. Risks & open questions

| # | Topic | Question to resolve |
|---|---|---|
| 1 | Invite flow | Admin-set one-time passphrase vs. invitee-supplied public key for the DEK re-wrap? The latter is more secure (admin never sees invitee's secret) but needs a keypair step. |
| 2 | DEK rotation cost | Re-encrypting all sensitive fields on member removal is O(data). Ship removal without rotation first (revoke access) and add rotation as a background job? |
| 3 | Private-scope leakage | Entities/relations extracted from a private-scoped doc could surface in team graph queries. Need extraction-time propagation of `visibility` to derived rows, or exclude private-derived facts from team reads. |
| 4 | Audit | Should `audit_log` gain `actor_member_id` too (recommended — org accountability)? |
| 5 | Per-member cloud policy | Is cloud-LLM policy a team setting or per-member? (Lean: team-level, admin-controlled.) |
| 6 | Quota / cost | Multiple members → more dreaming/LLM load. Out of scope here; note for ops. |

---

## 11. Rollout phases

- **A0** — 032 `tenant_members` + crypto (add/unlock/remove w/o rotation) + backfill. Single-person installs unaffected.
- **A1** — attribution columns (033) + `actor_member_id` wired through write paths + views.
- **A2** — per-member preferences & identity (034); MCP `WOXPAS_MCP_MEMBER_ID`.
- **A3** — roles enforced + member admin API/CLI.
- **A4** — private scopes (035) + the one extra RLS predicate.
- **A5** — DEK rotation job (risk #2) + audit actor (#4).
- **B** — federation, separate design doc.

Each phase is independently shippable and leaves the single-user experience intact.

---

## 12. Operator notes & known limitations (as built, A0–A5)

Surfaced at runtime where noted, so operators aren't caught out:

1. **A member's role is read once, at MCP-server startup.** If an admin changes a
   member's role while that member's `woxpas-mcp-server` is running, the change
   takes effect only after the server restarts. The server logs
   `mcp.server.member_role_fixed_for_session` at startup as a reminder. (The
   HTTP API re-resolves the role every request, so it is unaffected.)
2. **Audit attribution.** `audit_log` rows now carry the acting member in
   `metadata.member_id` when a member is resolved (MCP: from the bound member;
   HTTP: from `request.state`). Rows with no member (dev / single_tenant_local /
   background jobs) simply omit it.
3. **Private scopes need a member.** Creating a `private` scope requires a
   resolved member (jwt mode). In dev / single_tenant_local mode there is no
   member context, so `POST /scopes {visibility:"private"}` returns 400 with an
   explanatory message; use a `team` scope, or the CLI `create-scope --private
   --owner-member <id>`.
4. **Private scopes hide organization, not content** (A4). The scope and its
   membership labels are owner-only, but the underlying documents/chunks/
   entities remain tenant-visible until the content-privacy phase ships — see
   [[private-content]]. The migration 035 header says this too.
5. **Forward secrecy on member removal needs a rotation.** `remove-member`
   revokes access going forward, but true forward secrecy against a copied
   envelope requires a DEK rotation, which is gated on a crypto decision — see
   [[dek-rotation]]. The unlock path already rejects a stale-version envelope,
   so the rails are safe.
