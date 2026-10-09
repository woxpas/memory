# Woxpas

**A sovereign personal memory layer.** Your knowledge graph, your data, your
machine — captured once and exposed to *any* AI agent through the Model Context
Protocol (MCP).

Woxpas ingests what you tell it (notes, files, voice notes, conversations agents
save on your behalf), builds a structured knowledge graph out of it (entities,
ideas, tasks, conflicts, relations), and serves it back to whatever agent you
happen to be using — Claude Desktop, Cursor, a custom client, or any MCP-capable
agent — over one stable tool surface.

The point: **switching agents preserves your memory, your preferences, and your
prepared actions.** The intelligence lives with *you*, not with any one vendor's
chat history.

---

## Table of contents

- [What it is / isn't](#what-it-is--isnt)
- [Feature highlights](#feature-highlights)
- [The four pipelines](#the-four-pipelines)
- [Architecture](#architecture)
- [Technology stack](#technology-stack)
- [Repository layout](#repository-layout)
- [Quickstart](#quickstart)
- [The MCP tool surface](#the-mcp-tool-surface)
- [Security model](#security-model)
- [Current status](#current-status)
- [Documentation](#documentation)
- [Contributing](#contributing)
- [License](#license)

---

## What it is / isn't

**It is:**

- A **local-first, offline-by-default** personal memory engine. Embeddings, ASR,
  summarization, and extraction all run on local CPU/GPU models out of the box.
  No account, no cloud dependency to get started.
- An **owner of its entire pipeline** — ingestion → canonicalization → chunking →
  embedding → knowledge-graph extraction → conflict detection → retrieval →
  citation-verified synthesis → resurfacing.
- A **multi-agent memory bus**. It speaks MCP, so every MCP-capable agent shares
  one brain.
- **Encrypted at rest** at the object-store layer (AES-256-GCM envelope
  encryption) with per-tenant data-encryption keys and Postgres row-level
  security for tenant isolation.

**It isn't:**

- A general-purpose RAG library. Woxpas owns the whole pipeline; it isn't a
  drop-in vector store wrapper.
- Cloud-first. Cloud LLMs are strictly opt-in and gated per tenant; the default
  install never sends your data anywhere.
- An agent. Woxpas is the *memory + cognition* layer.

---

## Feature highlights

| Area | What you get |
|---|---|
| **Ingestion** | One `IngestEnvelope` entry point for every channel (file / text / voice / MCP / agent / browser / slack / email / calendar). Antivirus scan → type sniffing → extraction → canonicalization → chunking → embedding, all event-driven. |
| **Canonical Document Model** | Lossless structured JSON persisted in the object store. Extraction always reads the CDM, never the original PDF/DOCX. Precise provenance: doc → block → span → (page+bbox \| timestamp \| char range). |
| **Knowledge graph** | Entities, relations, ideas, tasks, and profile facts extracted per-chunk by a local LLM, then linked and disambiguated. Content-derived UUIDs make re-ingestion idempotent. |
| **Conflict detection** | SQL-driven detection of knowledge / preference / profile-fact conflicts, with resolution shapes: *pick*, *coexistence*, *progression*, *dismissed*. |
| **Bitemporal model** | Every fact carries `system_time` (when known) and `valid_time` (what it's about) as `tstzrange` columns — drives temporal conflicts and time-aware recall. |
| **Retrieval** | Intent-routed hybrid retrieval: a fast rule classifier (with optional LLM fallback) picks between semantic / timeline / type-listing / file-lookup / entity-card / graph strategies over Qdrant + Postgres, with a cross-encoder reranker. |
| **Synthesis** | Streaming, **citation-verified** answers. The LLM cites source chunks inline; a deterministic verifier (lexical n-gram + cosine) checks each citation against the source span. No "LLM-as-judge." |
| **Resurfacing** | A background "dreaming" worker that surfaces what matters — trending/fading/forgotten patterns, overdue tasks, open conflicts, dormant ideas — plus cross-agent preferences and prepared actions. |
| **Sovereignty** | Encrypted at rest, closed-source-safe dependency set, pluggable model backends (local llama.cpp / OpenAI-compatible / Anthropic), cross-platform, offline-first. |

---

## The four pipelines

Woxpas is built as four cooperating pipelines. All four are implemented in v0.1.

1. **Ingestion** — files / voice / MCP / agent / browser / slack / calendar in →
   canonical doc + blocks + chunks + spans + summary + embeddings out.
2. **Extraction** — entities, relations, ideas, tasks, profile facts, and
   conflicts out of the chunks, linked and disambiguated into the graph.
3. **Retrieval** — an agent's query is classified, routed, searched (semantic +
   graph + direct SQL), reranked, and enriched with entity dossiers.
4. **Synthesis** — the LLM composes an answer from retrieved chunks and cites
   spans; every citation is mechanically verified against its source.

Sitting on top is the **resurfacing / cognition layer** — the differentiator.
It runs continuously in the background to make the memory *anti-fade* and
*agent-actionable*.

---

## Architecture

```
┌───────────────────────────────────────────────────────────┐
│  Any MCP agent   (Claude Desktop / Cursor / custom)        │
└───────────────────────────┬───────────────────────────────┘
                            │ MCP protocol · stdio · 27 tools
┌───────────────────────────▼───────────────────────────────┐
│  Woxpas MCP server         memory.*  +  resurface.*        │
├───────────────────────────────────────────────────────────┤
│  Shared engine layer (in-process, no HTTP hop)            │
│   • Pipeline      ingest → extract → knowledge graph      │
│   • Retrieval     intent routing · vector · graph · SQL   │
│   • Synthesis     streaming answers · citation verifier   │
│   • Resurfacing   thinking brain · dormant brain          │
└───────────────────────────┬───────────────────────────────┘
                            │
        ┌───────────────────┼────────────────────┐
        ▼                   ▼                     ▼
┌───────────────┐  ┌────────────────┐  ┌──────────────────┐
│ Postgres 17   │  │ Qdrant         │  │ MinIO / S3       │
│ + pgvector    │  │ 4 collections  │  │ 3 buckets,       │
│ RLS, bitemporal│ │ dense vectors  │  │ AES-256-GCM      │
│ outbox/NOTIFY │  │                │  │ client-side enc. │
└───────────────┘  └────────────────┘  └──────────────────┘
        ▲
        │ ClamAV (antivirus) · localhost-only API gateway
        │ event-driven worker (pull-based via transactional outbox)
```

Two independent runtimes talk to the same engine classes:

- The **HTTP API** (`woxpas-api`, FastAPI, binds `127.0.0.1:7800`) for local
  tools, the CLI, and a future UI.
- The **MCP server** (`woxpas-mcp`, stdio JSON-RPC) spawned per-agent, which
  imports the *same* engine classes in-process — MCP tools and HTTP routes are
  thin wrappers over one shared core.

The **worker** (`woxpas-worker`) has no inbound ports. It pulls work from a
Postgres transactional outbox driven by `LISTEN/NOTIFY`, so every pipeline stage
is decoupled and independently retryable.

---

## Technology stack

Everything in the default path is **local and permissively licensed** (MIT /
Apache-2.0 / BSD / MPL) — a deliberate constraint so the project stays
closed-source-safe and vendor-neutral.

| Layer | Choice |
|---|---|
| Language / tooling | Python 3.12+, [`uv`](https://github.com/astral-sh/uv) workspace, `ruff` + `mypy --strict` (both CI-enforced, green) |
| API | FastAPI + Uvicorn, SSE streaming |
| Datastore | PostgreSQL 17 + pgvector, `pg_trgm`, `btree_gist`; row-level security |
| Vector DB | Qdrant 1.13 (dense vectors, INT8 quantized, HNSW) |
| Object store | MinIO (S3-compatible), client-side AES-256-GCM |
| Antivirus | ClamAV |
| Embeddings | BGE-M3 via **ONNX Runtime** (no torch / no transformers) |
| Reranker | BGE-reranker-v2-m3 (ONNX, INT8) |
| ASR | Whisper-small via faster-whisper / CTranslate2 (no torch) |
| Local LLM | Qwen2.5-1.5B/3B-Instruct GGUF via `llama-cpp-python` |
| Cloud LLM (opt-in) | Anthropic, OpenAI-compatible (DeepSeek / Kimi / OpenAI / vLLM / Ollama …) |
| Crypto | `cryptography` (AES-256-GCM, HKDF-SHA256), `argon2-cffi` (Argon2id) |
| Boot daemon | Python `woxpas-unlock` — passphrase → Argon2id KEK → unwrap DEK → Unix socket |

The ONNX-first choice is intentional: no `torch`, no `transformers`, no
`FlagEmbedding` — smaller install, permissive licenses, `numpy`-native floats.

---

## Repository layout

A `uv` workspace of eleven Python packages plus infra:

```
packages/
  core/         Shared vocabulary: IngestEnvelope, Canonical Document Model,
                content-derived IDs, chunks/spans, processing profiles, events.
  storage/      Postgres pool (+RLS), transactional outbox, repos, Qdrant
                collections, object store, and the crypto envelope (KEK/DEK).
  runtime/      Pluggable model backends: embedder, reranker, ASR, LLM
                (local llama.cpp / OpenAI-compat / Anthropic), hardware probe,
                model catalog, and the prompt-injection defense module.
  adapters/     Source-channel adapters (file adapter today; one per channel).
  pipeline/     The event-driven ingestion + extraction worker and all stages;
                the `woxpas-cli` and `woxpas-worker` entry points.
  api/          FastAPI HTTP gateway, auth (dev / single-tenant / JWT), audit
                middleware, and the engine layer (conflict/document/scope).
  retrieval/    Intent classification, vector/graph/SQL backends, reranking,
                enrichment, cursors, time resolution.
  synthesis/    Streaming answer generation, prompt building, citation parser
                and verifier, cloud-policy gating.
  resurfacing/  The "dreaming" worker, pattern scorers, opportunity detectors,
                score maintainer, scope reassigner; `woxpas-dreaming-worker`.
  mcp/          The stdio MCP server exposing 27 tools over the shared engines.
  boot/         `woxpas-unlock` — passphrase → Argon2id KEK → unwrap DEK → serve
                it over a chmod-600 Unix socket (keeps the DEK off disk).

migrations/     31 ordered SQL migrations — the full data model.
infra/          Postgres init (extensions + roles), Qdrant config.
docs/           MCP integration guides + this project's extended docs.
tests/smoke/    Smoke batteries (bash + Python MCP clients).
scripts/        migrate.sh, install_claude_desktop.py, model-mirror instructions.
Dockerfile      One image for all app processes (API, worker, dreaming, MCP).
install.sh      Docker-only one-command installer.
```

---

## Quickstart

### Simplest — one script, Docker only

**Prerequisites:** [Docker Desktop](https://www.docker.com/products/docker-desktop/)
(running) + ~6 GB free disk + 8 GB RAM. (The script also uses `bash` and a
`python3` of any version for the Claude Desktop step — both already on macOS and
Linux. No `uv`, no Python 3.12.)

```bash
./install.sh "Your Name"
```

It builds the image, generates secrets, starts the datastores and workers,
creates your tenant (you choose a passphrase), and wires Claude Desktop — then
**restart Claude Desktop** and Woxpas shows up as a set of memory tools.
Everything runs in containers; stop it with `docker compose --profile app down`.
Re-running is safe.

### Or — with the uv toolchain (for development)

If you have [`uv`](https://github.com/astral-sh/uv) + Python 3.12+, three commands:

```bash
uv run woxpas-cli bootstrap              # 1. one-time setup (secrets, tenant, models)
uv run woxpas-cli up                     # 2. start everything (one command, no extra terminals)
uv run woxpas-cli install-claude-desktop # 3. wire up Claude Desktop
```

`up` runs the worker/API/dreaming worker in the background (logs in
`~/.woxpas/logs/`; stop with `woxpas-cli down`). `install-claude-desktop` merges
a `woxpas` entry into Claude Desktop's config, backing up any existing file and
leaving your other MCP servers alone (`--print` to preview, `--docker` to point
Claude at the containerized server instead of a host process).

### Try it from the terminal (optional)

Your agent (Claude Desktop) drives Woxpas through the MCP tools — that's the
main interface. The HTTP API below is a convenience for scripting/testing and
runs only on the **uv/host path** (`woxpas-cli up`): the Docker-only install
keeps the API off by default (no exposed port) and uses the MCP server instead.

```bash
curl -X POST 127.0.0.1:7800/ingest/text \
  -H "X-Woxpas-Tenant-Id: $WOXPAS_DEV_TENANT_ID_DEFAULT" \
  -H "Content-Type: application/json" \
  -d '{"content_text":"Alpha kickoff next Tuesday. Sarah owns the API."}'

curl -X POST 127.0.0.1:7800/recall \
  -H "X-Woxpas-Tenant-Id: $WOXPAS_DEV_TENANT_ID_DEFAULT" \
  -H "Content-Type: application/json" \
  -d '{"query_text":"who owns the API?"}'
```

> The full annotated walk-through — manual service start, the boot daemon,
> Cursor / custom MCP clients, troubleshooting, and cloud deployment — lives in
> **[docs/GETTING_STARTED.md](docs/GETTING_STARTED.md)**.

---

## The MCP tool surface

Drop a config snippet into your agent and it immediately gets **27 tools**
(13 `memory.*` + 14 `resurface.*`). Paste the **[system prompt](docs/mcp/system-prompt.md)**
into your agent's instructions so it knows to use Woxpas for memory instead
of its own ephemeral context.

- **System prompt** — [docs/mcp/system-prompt.md](docs/mcp/system-prompt.md) (copy-paste for any agent)
- **Claude Desktop** — [docs/mcp/claude_desktop.md](docs/mcp/claude_desktop.md)
- **Cursor** — [docs/mcp/cursor.md](docs/mcp/cursor.md)
- **Mobile & remote** — [docs/mcp/mobile.md](docs/mcp/mobile.md)
- **Any custom MCP client** — [docs/mcp/README.md](docs/mcp/README.md)
- **Full tool inventory** — [docs/mcp/tools.md](docs/mcp/tools.md)
- **Session workflows** — [docs/mcp/workflows.md](docs/mcp/workflows.md)
- **What Woxpas does for you** — [docs/examples.md](docs/examples.md)

**`memory.*`** — recall, ask (citation-verified synthesis), get_chunks, ingest,
get_document_full, create_scope, assign_to_scope, unassign_from_scope,
archive_document, unarchive_document, list_conflicts, get_conflict,
resolve_conflict.

**`resurface.*`** — get_context (Thinking Brain), get_patterns (Dormant Brain),
record_observation, get_preferences, get_tool_suggestions, get_predicted_actions,
get_predicted_questions, post_prepared_action, list_prepared_actions,
approve_prepared_action, reject_prepared_action, mark_executed, upsert_persona,
record_decision.

The MCP server is **fail-closed**: it refuses to start without a
`WOXPAS_MCP_TENANT_ID`, and the tenant is *never* accepted as a tool argument —
it's bound at process spawn. Postgres RLS is the second line of defense.

---

## Security model

Woxpas is designed around four pillars — **speed, accuracy, security, and
sovereignty** — and security is treated as non-negotiable. What's actually
implemented today:

**Strong, implemented:**

- **Envelope encryption at rest.** Object-store payloads (raw bytes, canonical
  JSON, summaries) are encrypted client-side with **AES-256-GCM**. Per-object
  random 96-bit IVs, 128-bit tags, and **AAD binding** to bucket + object key +
  key version. Per-purpose field keys are derived from the tenant DEK via
  **HKDF-SHA256**. The DEK itself is wrapped under an **Argon2id**-derived KEK
  (256 MiB, t=4). Passphrase rotation re-wraps the DEK — no data re-encryption.
- **Tenant isolation.** Postgres **row-level security** is `FORCE`d on all 25
  tenant-scoped tables; every session sets `SET LOCAL ROLE woxpas_app` +
  `app.tenant_id`. Repos additionally filter `tenant_id` in every query
  (defense in depth). Vector search mirrors the tenant filter in Qdrant payloads.
- **Prompt-injection defense.** A dedicated module wraps all untrusted content in
  unambiguous delimiters, reinforces instructions before *and* after source
  content, blacklists known injection patterns, enforces a strict output schema,
  and — critically — **never trusts an LLM alone for a security signal**
  (sensitivity classification combines a manual flag, non-LLM heuristics, and the
  LLM judgment).
- **Cloud egress is gated and audited.** Three independent, default-*off* tenant
  switches control whether personal / sensitive / background-job data may reach a
  cloud LLM. Every cloud call — allowed, denied, or errored — writes an append-only
  `audit_log` row with token counts. Local models always pass through instantly.
- **Citation verification.** Synthesis answers are checked *mechanically*
  (lexical n-gram overlap + embedding cosine) against source spans — deterministic,
  not dependent on the model's good behavior. XSS-safe HTML rendering of answers.
- **Hardened deployment.** Data services run on an internal-only Docker network
  with no host ports; only the API is externally bound and only on `127.0.0.1`.
  `cap_drop: ALL`, `no-new-privileges`, and `read_only` filesystems on the app
  containers. No `eval`/`exec`/`pickle`/`shell=True` anywhere. JWT mode verifies
  signature + audience + issuer and whitelists asymmetric algorithms only.

**Honest limitations you should know before deploying (v0.1):**

- **Passphrase-based DEK protection requires the boot daemon.** Object payloads
  are encrypted with AES-256-GCM, and the full passphrase → Argon2id KEK → unwrap
  DEK chain is implemented and working. In dev mode, `WOXPAS_DEV_DEK_HEX` is still
  accepted as a convenience fallback. For any deployment beyond localhost, run
  `woxpas-cli unlock` to start the boot daemon — the DEK is then served over a
  `chmod 600` Unix socket and never touches `.env` or disk. OS full-disk encryption
  (FileVault / LUKS) covers the datastore files.
- **`dev` / `single_tenant_local` auth modes trust an `X-Woxpas-Tenant-Id` header**
  and are only safe on loopback (enforced by a host guard). **Only `jwt` mode is
  safe for any network-reachable deployment.**
- **The HTTP API has no built-in rate limiting.** Put it behind a reverse proxy
  (mTLS + throttling) for anything beyond localhost.
- **`CLAMAV_ENABLED=false` silently disables virus scanning** (files pass as
  clean). Leave AV on.

The full, itemized security review — with file:line references and severities —
is in **[SECURITY.md](SECURITY.md)**. Read it before
any non-local deployment.

---

## Current status

All four pipelines plus the resurfacing layer are implemented, covered by 21
smoke batteries, and expose a stable 27-tool MCP surface. The passphrase → KEK
→ DEK boot daemon (`woxpas-unlock`) closes the plaintext-key gap.

Known limitations (see [SECURITY.md](SECURITY.md) for details):

- **Hybrid retrieval** — three-head vector search (dense + sparse + ColBERT) is
  implemented and active for newly ingested documents. Existing chunks remain
  dense-only until re-ingested; search degrades gracefully.
- **Streaming crypto & large files** — v0.1 loads whole files into RAM for hashing
  and encryption.
- **API hardening** — rate limiting and idempotency keys (HTTP 5xx error detail
  is redacted; see [SECURITY.md](SECURITY.md)).
- **Channel adapters** — only file/text ingestion is routed; voice / browser /
  slack / calendar adapters are designed but not yet wired to routes.

---

## Documentation

- **[docs/GETTING_STARTED.md](docs/GETTING_STARTED.md)** — the full, annotated
  how-to: install, configure, bootstrap, ingest, recall, connect an agent,
  operate, and troubleshoot.
- **[SECURITY.md](SECURITY.md)** — honest gap +
  security assessment with a pre-release checklist.
- **[docs/mcp/](docs/mcp/)** — MCP integration (Claude Desktop, Cursor, custom),
  the tool inventory, session workflows, and error codes.
- **[docs/examples.md](docs/examples.md)** — concrete scenarios: what Woxpas
  actually does for you day-to-day.

---

## Contributing

Contributions are welcome. See **[CONTRIBUTING.md](CONTRIBUTING.md)** for the
full guide. In short, before opening a PR:

- `uv sync --all-packages`
- `uv run ruff check .` — lint, required to be green (CI enforces it). Formatting
  is manual; `ruff format` is intentionally not enforced.
- `uv run mypy packages` — the workspace is `mypy --strict` and green; keep it that way.
- `uv run pytest packages` — the infra-free unit tests.
- Run the relevant smoke battery under `tests/smoke/` if behaviour changed (they
  share local models — **run them sequentially, not in parallel**).

Design principle: every change is scored against the four pillars — **speed,
accuracy, security, sovereignty**. A change that violates a core architecture
commitment (see `docs/` and the migration comments) should name the commitment
it's trading against.

---

## License

Woxpas is licensed under the **Apache License, Version 2.0** ([LICENSE](LICENSE)).
Apache-2.0 was chosen for its permissive terms, explicit patent grant, and
trademark protection — consistent with the project's goal of maximal adoption
while protecting the Woxpas name.

Every runtime dependency is permissively licensed (MIT / Apache-2.0 / BSD / MPL)
by design, so embedding, forking, and commercial use are all unencumbered.
