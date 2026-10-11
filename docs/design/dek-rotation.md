# Design: DEK rotation & forward secrecy

**Status:** Plan for review — **a decision is required** (§4) · **Addresses:** [[team-memory]] §4 "Remove a member" + §10 risk #2 · **Rails shipped:** Phase A5

## 1. Goal

When a member is removed, revoke their access **and** achieve *forward secrecy*: a copy they kept of their wrapped-DEK envelope (plus their passphrase) must stop being useful. Today's `remove-member` flips their status to `removed` so they can't unlock through the system — but their envelope still mathematically unwraps the **unchanged** tenant DEK, which still decrypts all data. Closing that gap needs a **DEK rotation**.

## 2. What rotation must do (and why it's expensive)

A rotation is three coupled steps:

1. **New DEK.** Generate DEK v(N+1).
2. **Re-encrypt everything encrypted under the old DEK** — this is the O(data) cost. In this codebase the DEK (via HKDF field keys) protects:
   - object-store blobs (raw payloads, the bulk),
   - `source_credentials` ciphertext,
   - any at-rest field listed in `tenants.crypto_policy.sensitive_fields` (`text`, `summary`, `raw_uri`).
   Without this step the old DEK still decrypts data, so a kept envelope keeps working — **re-wrapping alone is false forward secrecy.**
3. **Re-wrap** DEK v(N+1) for every *remaining* member and bump `tenants.dek_version`.

Step 3 is the blocker (§4).

## 3. Rails shipped in A5 (decision-free, fail-safe)

These make rotation *safe the day it lands*, and are correct under every scheme below:

- **`needs_rekey` member status** (migration 037) — the state a survivor is parked in until re-keyed.
- **Unlock dek_version guard** — if a member's envelope `dek_version` is behind `tenants.dek_version`, unlock refuses and tells them to re-key, instead of silently returning the stale (old) DEK. (`_unlock_tenant_dek_via_member`.)
- **Status gating** — only `active` members' envelopes are ever considered for unlock, so `removed` / `suspended` / `needs_rekey` can't unlock.

What A5 does **not** ship: a live `rotate-dek` command — because correct rotation needs the §4 decision *and* the step-2 re-encryption job, and a half-rotation would be dangerously misleading.

## 4. The decision: how to re-wrap for absent members

With the current **symmetric** model (passphrase → Argon2id → KEK → unwrap DEK), re-wrapping the new DEK for member X requires X's KEK, which requires **X's passphrase** — not available when an admin rotates. Three ways out:

| Scheme | How re-wrap works | Cost / UX | Verdict |
|---|---|---|---|
| **A. All-passphrases** | Every member supplies their passphrase during the rotation | Impractical for async teams | ✗ |
| **B. Keypair (asymmetric)** | Each member has a keypair; the DEK is wrapped under their **public** key, so anyone can re-wrap for everyone without secrets | One-time keypair infra + migration; also cleanly solves the invite flow ([[team-memory]] §10 #1) | ✓ **long-term** |
| **C. Pending re-key** | Rotate + re-encrypt now; re-wrap only for the admin; set all other survivors `needs_rekey`; they're re-admitted via `add-member` (new DEK wrapped under a fresh passphrase) | No new crypto; survivors need one re-invite after a rotation | ✓ **now** |

**Recommendation:** ship **C (pending re-key)** first — it needs no new crypto and A5's rails already support it (`needs_rekey` + version guard). Adopt **B (keypairs)** when the invite-flow decision is made; B then makes rotation seamless (no re-invites) and unifies with the invite design. These aren't mutually exclusive: C is the interim, B is the destination.

This is the open decision — C now vs. wait for B — deferred earlier under "no commitments."

## 5. Build plan (once a scheme is chosen)

- **R1 — re-encryption job.** A streaming, resumable re-encrypt of object-store blobs + `source_credentials` old-DEK→new-DEK. Resumable because it's O(data) and must survive interruption; track progress so a crash doesn't leave a half-rotated tenant. Run with the stack quiesced or with a dual-key read window (old+new) during migration.
- **R2 — `rotate-dek` command (scheme C).** Admin passphrase → unwrap old DEK → generate new → run R1 → bump `tenants.dek_version` → re-wrap for the admin → set other `active` members to `needs_rekey`. Atomic at the metadata layer; R1 idempotent.
- **R3 — auto-trigger.** Optional `remove-member --rotate` that chains R2, so forward secrecy is one command.
- **R4 — scheme B migration** (if/when chosen): add member keypairs, wrap DEK under public keys, make rotation re-wrap for all without `needs_rekey`.

## 6. Pillar check

- **Security** — the whole point; A5's version guard guarantees no stale key is ever served, so even a partial/aborted future rotation fails safe. Re-encryption (step 2) is mandatory and called out as the thing that actually delivers forward secrecy.
- **Speed** — rotation is a rare admin op; R1 is O(data) but resumable and off the hot path. The unlock guard adds one cheap `dek_version` read.
- **Accuracy** — `dek_version` is the single source of truth for staleness, compared on every unlock.
- **Sovereignty** — single-user installs never rotate; version always matches; guard is a no-op.

## 7. Open questions

1. **C vs. B timing** — adopt pending-rekey now, or wait and do keypairs once (avoiding a two-step migration)? Depends on how soon real multi-member teams need member removal with forward secrecy.
2. **Re-encryption window** — quiesce the tenant during R1, or support dual-key reads (old+new) so the instance stays live? Dual-key is more code but no downtime.
3. **Partial-rotation recovery** — exact resume semantics for R1 if it crashes mid-run (per-object version tag in the blob payload?).
