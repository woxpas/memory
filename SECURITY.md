# Security

Woxpas is built around four pillars — **speed, accuracy, security, and
sovereignty** — and security is treated as non-negotiable. This document
describes the implemented security model, the known limitations you must
understand before deploying beyond localhost, and how to report a vulnerability.

---

## Security model (implemented)

- **Envelope encryption at rest.** Object-store payloads (raw bytes, canonical
  JSON, summaries) are encrypted client-side with **AES-256-GCM** — per-object
  random 96-bit IVs, 128-bit tags, and **AAD binding** to bucket + object key +
  key version. Per-purpose field keys are derived from the tenant DEK via
  **HKDF-SHA256**. The DEK is wrapped under an **Argon2id**-derived KEK
  (256 MiB, t=4). Passphrase rotation re-wraps the DEK — no data re-encryption.
- **Tenant isolation.** Postgres **row-level security** is `FORCE`d on all 25
  tenant-scoped tables; every session sets `SET LOCAL ROLE woxpas_app` +
  `app.tenant_id`. Repos additionally filter `tenant_id` in every query
  (defense in depth). Vector search mirrors the tenant filter in Qdrant payloads.
  The tenant is never accepted from a request body or MCP tool argument.
- **Prompt-injection defense.** A dedicated module (`woxpas_runtime.prompt_safety`)
  wraps all untrusted content in unambiguous delimiters, reinforces instructions
  before and after source content, blacklists known injection patterns, enforces
  strict output schemas, and **never trusts an LLM alone for a security signal**.
- **Cloud egress is gated and audited.** Three independent, default-*off* tenant
  switches control whether personal / sensitive / background-job data may reach a
  cloud LLM. Every cloud call — allowed, denied, or errored — writes an
  append-only `audit_log` row. Local models always pass through instantly.
- **Citation verification.** Synthesis answers are checked mechanically (lexical
  n-gram overlap + embedding cosine) against source spans — deterministic, not
  an LLM-as-judge. Answer HTML is escaped to prevent XSS from injected content.
- **Hardened deployment.** Data services run on an internal-only Docker network
  with no host ports; only the API is externally bound and only on `127.0.0.1`.
  `cap_drop: ALL`, `no-new-privileges`, `read_only` filesystems on the app
  containers. No `eval`/`exec`/`pickle`/`shell=True` anywhere. SQL is fully
  parameterized. JWT mode verifies signature + audience + issuer and whitelists
  asymmetric algorithms only (no HS256 key-confusion).

---

## Hardening applied for the open-source release

The following were tightened during the pre-release review (all verified against
the four pillars — none add latency, cloud dependencies, or non-permissive deps):

- **HTTP error responses no longer leak internal exception messages.** Every
  `5xx` `detail` now carries only the exception *class name*; the full message
  (which could contain SQL, paths, or row data) stays in server logs. Applies to
  all routes and the synthesis SSE error event (which still surfaces actionable
  `CloudPolicyDenied` messages, since agents must act on those).
- **Disabling ClamAV is now loud.** `ClamAVClient` logs a prominent
  `clamav.disabled` warning at startup when scanning is off, so no operator can
  silently accept unscanned files.
- **Load-bearing security invariants are now documented in code** — the
  raw-pool tenant filter in `cloud_policy.assert_cloud_allowed` (RLS is not
  engaged there; the `WHERE tenant_id` predicate is the only scope) and the
  trusted-identifier interpolation in `pool.tenant_session`.
- **Stale docstrings that mislabeled live controls as stubs were corrected** —
  the MCP rate limiter and audit writer are *enforced*, not "v0.1 stubs"; the
  synthesis cloud-policy gate *refuses*, it doesn't merely audit.

---

## Known limitations (read before any non-local deployment)

v0.1 targets **sovereign single-user local** use. The following are real,
deliberate limitations — not bugs — and must be handled before exposing Woxpas
beyond `127.0.0.1`:

1. **Key management: passphrase-based DEK protection is available via the boot
   daemon.** The full passphrase → Argon2id KEK → AES-256-GCM unwrap DEK chain
   is implemented and working in `woxpas-unlock` (Python, see `packages/boot/`).
   Start it with `woxpas-cli unlock` — the DEK is served over a `chmod 600` Unix
   socket and never touches `.env` or disk. For dev convenience, `WOXPAS_DEV_DEK_HEX`
   is still accepted as a fallback, but any deployment beyond localhost should use
   the daemon. OS full-disk encryption (FileVault / LUKS) covers the datastore
   files at rest. The Go `woxpasd` supervisor (encrypted volume mount, service
   orchestration) remains a skeleton — the Python daemon closes the critical
   key-management gap.
2. **Only `jwt` auth mode is safe on a network.** `dev` and `single_tenant_local`
   trust a client-supplied `X-Woxpas-Tenant-Id` header and are hard-rejected on
   non-loopback hosts. For anything network-reachable, use `jwt` and set real
   `audience` / `issuer` / public key.
3. **The HTTP API has no built-in rate limiting.** This is by design — enforce it
   at a reverse proxy (with TLS/mTLS). The MCP server *does* have a per-process
   token bucket.
4. **Large files are processed in memory.** Hashing and encryption load the whole
   file into RAM in v0.1; streaming is a v0.2 task.

See the [README](README.md#security-model) and
[docs/GETTING_STARTED.md](docs/GETTING_STARTED.md#15-toward-a-networked--cloud-deployment)
for the networked-deployment checklist.

---

## Before you publish this repository

If you are forking/publishing Woxpas as open source:

- **Rotate every secret that has ever lived in a working-tree `.env`** — the DB /
  Qdrant / MinIO passwords, the plaintext DEK (`WOXPAS_DEV_DEK_HEX`), and any
  cloud LLM API keys (`WOXPAS_KEY_*`). Treat them as compromised.
- **Ship only `.env.example`, never `.env`.** `.env` is `.gitignore`d; if it ever
  reached git history, scrub it (`git filter-repo`) before going public.
- **Add a secret-scan** to CI (grep for `sk-`, `sk-ant-`, non-empty
  `WOXPAS_KEY_*`, `WOXPAS_DEV_DEK_HEX`).
- **License is Apache-2.0** — all `pyproject.toml` files and the top-level
  `LICENSE` file are set. Confirm before publishing.

---

## Reporting a vulnerability

Please **do not** open a public issue for security vulnerabilities. Instead,
report privately to admin@woxpas.ai. Include steps to reproduce and the
affected version. You can expect an acknowledgement within a few business days.
