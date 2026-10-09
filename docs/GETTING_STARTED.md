# Getting started with Woxpas

A complete, annotated walk-through: from a clean machine to a running memory
layer that any MCP agent can talk to. Every step explains *what* it does and
*why*, so you can adapt it to your environment.

> New here? Read the [README](../README.md) first for the mental model. This
> guide is the operational manual. For an honest account of what is and isn't
> production-ready, read [SECURITY.md](../SECURITY.md).

> **Just want it running?**
> - **Docker only (no uv):** `./install.sh "Your Name"` — builds the image,
>   generates secrets, starts everything in containers, creates your tenant, and
>   wires Claude Desktop. Restart Claude Desktop after.
> - **With uv:** `uv run woxpas-cli bootstrap`, then `woxpas-cli up`, then
>   `woxpas-cli install-claude-desktop`.
>
> This guide below is the *long* form that explains each piece and covers the
> manual service start, the boot daemon, Cursor, and cloud deployment.

---

## Contents

**→ [One-command bootstrap](#3-one-command-bootstrap) — recommended for new users.**

1. [Prerequisites](#1-prerequisites)
2. [Get the code](#2-get-the-code)
3. [One-command bootstrap](#3-one-command-bootstrap) *(or manual steps 4–10 below)*
4. [Configure `.env` (manual)](#4-configure-env-manual)
5. [Start the infrastructure](#5-start-the-infrastructure)
6. [Install the workspace & run migrations](#6-install-the-workspace--run-migrations)
7. [Bootstrap your tenant](#7-bootstrap-your-tenant)
8. [Pre-fetch the models](#8-pre-fetch-the-models)
9. [Run the services](#9-run-the-services)
10. [Ingest content](#10-ingest-content)
11. [Recall and ask](#11-recall-and-ask)
12. [Connect an MCP agent](#12-connect-an-mcp-agent)
13. [Day-2 operations](#13-day-2-operations)
14. [Running the tests](#14-running-the-tests)
15. [Troubleshooting](#15-troubleshooting)
16. [Toward a networked / cloud deployment](#16-toward-a-networked--cloud-deployment)
17. [Next steps](#17-next-steps)

---

## 1. Prerequisites

| Requirement | Notes |
|---|---|
| **Docker Desktop** | Required. Runs the datastores and — in the Docker-only install — the app itself. |
| **`bash` + `python3` (any version)** | Only for `./install.sh` (the Claude Desktop step). Already on macOS/Linux. |
| **[`uv`](https://github.com/astral-sh/uv)** | *Only for the dev path* (`woxpas-cli` on the host). `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| **Python 3.12+** | *Only for the dev path.* `uv` can install it (`uv python install 3.12`). |
| **Disk** | ~6 GB for local models (embedder, reranker, ASR, Qwen LLM) + space for your data volumes. |
| **RAM** | 8 GB minimum; 16 GB comfortable (the local LLM + embedder run in-process). |
| **Accelerator (optional)** | Apple Silicon (Metal), CUDA, or ROCm are auto-detected. CPU-only works — it's the design floor. |

Woxpas is developed on macOS (Darwin) and Linux. Windows is untested; use WSL2.

---

## 2. Get the code

```bash
git clone <your-fork-or-repo-url> woxpas-memory
cd woxpas-memory
```

The repo is a `uv` workspace: eleven packages under `packages/`, infra under
`infra/` and `docker-compose.yml`, and the database schema under `migrations/`.

---

## 3. One-command bootstrap

The bootstrap command runs the entire setup flow: prerequisite checks →
cloud API keys (optional) → Docker infrastructure → workspace install +
migrations → tenant creation → model download → ready. It writes `.env`
automatically — no manual copy-paste.

```bash
uv run woxpas-cli bootstrap
```

**What it asks you:**
- **Cloud API keys** (Anthropic, OpenAI, DeepSeek, Kimi) — all optional. Leave
  blank for a fully local, sovereign install.
- **Display name** — your name, used for the self-entity and identity card.
- **Passphrase** — required; encrypts your data-encryption key (Argon2id).
  You'll re-enter it to unlock after a restart.
- **Model download confirmation** — shows the 4.2 GB budget and asks before
  pulling bytes.

**It writes to `.env` automatically:**
- `WOXPAS_DEV_TENANT_ID_DEFAULT` — your tenant UUID
- `WOXPAS_DEV_DEK_HEX` — the plaintext DEK (dev convenience)
- `POSTGRES_PASSWORD`, `QDRANT_API_KEY`, `MINIO_ROOT_PASSWORD` — if not already set
- `WOXPAS_KEY_ANTHROPIC` / `_OPENAI` / `_DEEPSEEK` / `_KIMI` — whatever you entered
- `API_AUTH_MODE=dev` — safe default for loopback-only use

**Idempotent:** re-running is safe — it detects existing tenants and cached
models, and skips completed steps.

**Scripted install:** `woxpas-cli bootstrap --display-name "Ada" --yes` skips
all prompts.

### After bootstrap: two commands and you're live

```bash
uv run woxpas-cli up                     # start datastores + worker + API + dreaming
uv run woxpas-cli install-claude-desktop # wire Claude Desktop, then restart it
```

`up` runs everything in the background (logs in `~/.woxpas/logs/`); stop it with
`woxpas-cli down`. `install-claude-desktop` merges a `woxpas` entry into Claude
Desktop's config (backing up any existing file, leaving other servers alone).

**That's the whole happy path: `bootstrap` → `up` → `install-claude-desktop`.**

---

> ## Everything below is reference / advanced
>
> If `bootstrap` worked, you don't need sections 4–9 — they're the **manual
> alternative** to `bootstrap` + `up`, for debugging or unusual environments.
> Skip to [§12 Connect an MCP agent](#12-connect-an-mcp-agent),
> [§13 Day-2 operations](#13-day-2-operations), or
> [§15 Troubleshooting](#15-troubleshooting) as needed.

---

## 4. Manual setup (advanced) — configure `.env` by hand

> Sections 4–9 are the **manual alternative** to `woxpas-cli bootstrap` +
> `woxpas-cli up`. You only need them if you're not using those commands.

```bash
cp .env.example .env
```

`.env.example` is heavily commented — open it and read it. The variables you
**must** set for a local install:

```bash
# Generate a strong value for each of these:
POSTGRES_PASSWORD=$(openssl rand -hex 24)
QDRANT_API_KEY=$(openssl rand -hex 24)
MINIO_ROOT_PASSWORD=$(openssl rand -hex 24)
```

Key settings and what they mean:

| Variable | Default | Meaning |
|---|---|---|
| `API_HOST` / `API_PORT` | `127.0.0.1` / `7800` | The HTTP gateway binds here. Keep it loopback. |
| `API_AUTH_MODE` | `dev` | `dev` (header-based, loopback only), `single_tenant_local` (loopback, auto-uses your default tenant), or `jwt` (production). See [SECURITY.md](../SECURITY.md). |
| `LOG_LEVEL` / `LOG_FORMAT` | `INFO` / `json` | Structured JSON logging by default. |
| `ACCEL_BACKEND` | `auto` | `auto` \| `cuda` \| `metal` \| `rocm` \| `cpu`. Override the hardware probe if needed. |
| `SUMMARY_MODEL_NAME` etc. | blank → built-in | Logical model names resolved from the runtime catalog. Leave blank for local defaults. |
| `WOXPAS_KEY_ANTHROPIC` / `_DEEPSEEK` / `_OPENAI` / `_KIMI` | blank | **Only** needed if you point a model at a cloud provider. Leave blank for offline-only. |
| `KDF_TIME_COST` / `KDF_MEMORY_COST_KIB` / `KDF_PARALLELISM` | 4 / 262144 / 2 | Argon2id parameters for the passphrase → KEK derivation. |

You'll add two more values (`WOXPAS_DEV_TENANT_ID_DEFAULT`, `WOXPAS_DEV_DEK_HEX`)
after bootstrapping your tenant in [step 6](#7-bootstrap-your-tenant).

> **Security:** `.env` holds real secrets — including, in dev mode, the plaintext
> data-encryption key (`WOXPAS_DEV_DEK_HEX`). It is `.gitignore`d, but treat the
> file as sensitive: `chmod 600 .env`, never commit it, and rotate anything that
> leaks. To avoid storing the DEK on disk at all, use the boot daemon
> (`woxpas-cli unlock`) — it prompts for your passphrase, derives the KEK, unwraps
> the DEK, and serves it over a local Unix socket. The worker/API/MCP server pick
> it up automatically. See [step 6b](#7b-start-the-boot-daemon-optional-but-recommended).

---

## 5. Start the infrastructure

```bash
docker compose up -d postgres qdrant minio clamav
```

What comes up:

- **postgres** (`pgvector/pgvector:pg17`) — the system of record. RLS, bitemporal
  columns, the transactional outbox. On first boot it runs
  `infra/postgres/init/` (extensions + the `woxpas_app` / `woxpas_audit` roles)
  and can apply migrations.
- **qdrant** (`v1.13.5`) — dense vector search, four collections.
- **minio** — S3-compatible object store, three buckets, browser console off.
- **clamav** — antivirus; scans every ingested file before processing. Its
  signature DB download makes the first `start_period` ~2 minutes.

In production these run on an **internal-only** Docker network with no host
ports. For local development, `docker-compose.override.yml` (gitignored,
auto-loaded) exposes each service on `127.0.0.1` so your host-side CLI/API/worker
processes can reach them. Confirm health:

```bash
docker compose ps        # all services should become healthy
```

ClamAV takes the longest — wait for it to report healthy before ingesting files.

---

## 6. Install the workspace & run migrations

```bash
uv sync --all-packages          # resolves and installs all 10 packages + dev tools
bash scripts/migrate.sh         # applies migrations/versions/*.sql idempotently
```

`scripts/migrate.sh` reads which versions are already recorded in
`schema_migrations` and applies only the missing ones, so it's safe to re-run.
The 31 migrations build the full data model: tenants + crypto envelope, sources,
scopes, documents + canonical blocks, chunks + spans, the entity/relation/idea/
task/conflict graph, timeline + outbox + audit, and the resurfacing layer.

---

## 7. Bootstrap your tenant

A **tenant** is your isolated memory space (one per person on a local install;
the schema supports many for the cloud path). Bootstrapping generates your
data-encryption key (DEK), wraps it under a passphrase-derived KEK, and seeds a
default file source + your "self" entity.

```bash
uv run woxpas-cli init-tenant --display-name "Your Name" --with-inbox --dev
```

- `--with-inbox` creates a default file source so you can ingest immediately.
- `--dev` prints the **plaintext DEK** and tenant id so you can put them in
  `.env` for local development. Omit `--dev` for a real deployment (the boot
  service is meant to supply the DEK instead — see limitations).
- The passphrase is read from `WOXPAS_INIT_PASSPHRASE` or prompted on stdin.
- `--identity-from PATH` optionally imports an identity-export markdown file to
  seed your profile (see `uv run woxpas-cli identity-template`).

Copy the two printed values into `.env`:

```bash
WOXPAS_DEV_TENANT_ID_DEFAULT=<printed tenant uuid>
WOXPAS_DEV_DEK_HEX=<printed dek hex>
```

> The `--dev` DEK print lands in your shell history and scrollback. On a shared
> machine, clear it (`history -c`) or write it to a `0600` file instead.

---

## 7b. Start the boot daemon (optional but recommended)

The boot daemon (`woxpas-unlock`) eliminates the plaintext DEK from your `.env`.
Instead of reading `WOXPAS_DEV_DEK_HEX`, the worker / API / MCP server request
the DEK over a local Unix socket. The daemon prompts for your passphrase once,
derives the KEK via Argon2id, unwraps the DEK, and holds it in memory.

```bash
# Start the daemon (prompts for your passphrase on the TTY)
uv run woxpas-cli unlock

# The daemon detaches and listens on ~/.woxpas/dek.sock
# Worker, API, and MCP server will pick up the DEK automatically.

# Check if the daemon is running
uv run python -c "import asyncio; from woxpas_boot.client import daemon_health; print(asyncio.run(daemon_health()))"

# Explicitly lock (zeroize DEK, shut down daemon)
uv run woxpas-cli lock
```

- **Idle timeout:** the daemon auto-locks after 15 minutes of inactivity
  (configurable with `--idle-lock-sec`).
- **Dev fallback:** if the daemon isn't running, `PipelineContext` falls back to
  `WOXPAS_DEV_DEK_HEX` silently — your dev workflow is unchanged.
- **Production:** run the daemon at boot (systemd launchd, etc.) and omit
  `WOXPAS_DEV_DEK_HEX` from `.env`.

> **Security note:** the daemon's socket is `chmod 600`. Only processes running
> as the same user can request the DEK. OS full-disk encryption (FileVault / LUKS)
> covers the datastore files at rest.

---

## 8. Pre-fetch the models

```bash
uv run woxpas-cli prefetch-models
```

This downloads (~6 GB, one-time) and caches into the `models/` volume:

- **BGE-M3** (ONNX) — embeddings, 1024-dim dense vectors.
- **BGE-reranker-v2-m3** (ONNX, INT8) — cross-encoder reranking.
- **Whisper-small** (CTranslate2) — voice-note transcription.
- **Qwen2.5-1.5B-Instruct** (GGUF, q4_k_m) — default summarization + extraction.

Useful flags:

- `--list-only` — show what *would* download and the size budget, then exit.
- `--with-extraction-model` — also pull Qwen2.5-3B for higher-quality extraction.
- `--yes` — skip the confirmation prompt.
- `uv run woxpas-cli cache-info --live` — inspect what's already cached vs required.

Everything runs offline after this step.

---

## 9. Run the services

> **Easiest:** `uv run woxpas-cli up` starts all three in the background for you
> (logs in `~/.woxpas/logs/`, stop with `woxpas-cli down`). The manual
> three-terminal form below is for when you want each process in the foreground.

Three long-running processes. Use three terminals (or a process manager).

```bash
uv run woxpas-worker            # A — the pipeline/extraction worker
uv run woxpas-api               # B — HTTP gateway, binds 127.0.0.1:7800
uv run woxpas-dreaming-worker   # C — optional background "dreaming" insights
```

- **`woxpas-worker`** subscribes to the Postgres outbox and runs every pipeline
  stage: antivirus → type detect → extract → chunk / summarize → embed → extract
  facts → link → conflict detect. It has no inbound ports; it pulls work.
- **`woxpas-api`** serves ingestion, recall, synthesis, scopes, documents,
  conflicts, and resurfacing over HTTP. Health check: `curl 127.0.0.1:7800/healthz`.
- **`woxpas-dreaming-worker`** periodically (default every 30 min, per active
  tenant) refreshes surface scores, mines patterns, and detects opportunities.
  Fully local unless you opt a tenant into cloud background jobs. Disable with
  `WOXPAS_DREAMING_ENABLED=false`.

Verify the stack:

```bash
curl -s 127.0.0.1:7800/health | jq       # aggregate health incl. each backend
```

---

## 10. Ingest content

Everything you feed Woxpas becomes a **document** that flows through the full
pipeline asynchronously. Ingestion is done over HTTP or via the `memory_ingest`
MCP tool — there is no `woxpas-cli` ingest subcommand in v0.1.

### a. Text via HTTP

```bash
curl -s -X POST 127.0.0.1:7800/ingest/text \
  -H "X-Woxpas-Tenant-Id: $WOXPAS_DEV_TENANT_ID_DEFAULT" \
  -H "Content-Type: application/json" \
  -d '{
        "content_text": "Alpha project kickoff is next Tuesday. Sarah owns the API.",
        "suggested_title": "Alpha kickoff note",
        "suggested_scope_slugs": ["alpha-project"],
        "captured_by": "user",
        "source_channel": "api_custom"
      }'
# → 202 { "doc_id": "...", "status": "received", "deduped": false }
```

### b. File upload via HTTP

```bash
curl -s -X POST 127.0.0.1:7800/ingest/file \
  -H "X-Woxpas-Tenant-Id: $WOXPAS_DEV_TENANT_ID_DEFAULT" \
  -F "file=@/path/to/contract.pdf" \
  -F "suggested_processing_profile=contract" \
  -F "sensitivity=sensitive"
```

Supported extractors in v0.1: **PDF** (pdfplumber), **DOCX** (python-docx),
**plain text**, and **audio** (Whisper ASR). The type is sniffed from the bytes,
not the extension. Files are antivirus-scanned first; the upload cap is 50 MB.

Ingestion returns immediately (`202`). Watch the worker logs to see the document
progress `received → scanned → detected → extracted → chunked → indexed →
extracted_facts → linked`. Once it reaches `indexed` it's retrievable.

Track progress:

```bash
uv run woxpas-cli inspect-doc <doc_id> --tenant-id "$WOXPAS_DEV_TENANT_ID_DEFAULT"
```

---

## 11. Recall and ask

**Recall** returns raw memory (chunks, entity cards, lists). **Ask** synthesizes a
citation-verified answer with an LLM.

```bash
# Fast retrieval — intent auto-classified
uv run woxpas-cli recall "what's happening with the alpha project" \
  --tenant-id "$WOXPAS_DEV_TENANT_ID_DEFAULT"

# Citation-verified synthesis (streamed)
uv run woxpas-cli ask "who owns the alpha project API?" \
  --tenant-id "$WOXPAS_DEV_TENANT_ID_DEFAULT"
```

Over HTTP, `POST /recall` returns a structured `RetrievalResult`, and
`POST /ask` / `POST /synthesize` stream Server-Sent Events (`sources`, `delta`,
`done`, `answer`, `verify`). `/ask` returns a lean citation envelope for agents;
`/synthesize` returns a heavier envelope (full chunk text) for UIs.

Recall intents you can hint (`--intent` / `intent_hint`): `semantic`,
`timeline`, `type_listing`, `file_lookup`, `entity_card`, `graph`, or `auto`.

---

## 12. Connect an MCP agent

The MCP server is spawned **per agent** as a stdio subprocess. It binds to one
tenant via env and exposes all 28 tools. The tenant is never a tool argument.

### Claude Desktop

Add to `claude_desktop_config.json` (see [docs/mcp/claude_desktop.md](mcp/claude_desktop.md)):

```json
{
  "mcpServers": {
    "woxpas": {
      "command": "uv",
      "args": ["run", "woxpas-mcp-server"],
      "cwd": "/absolute/path/to/woxpas-memory",
      "env": {
        "WOXPAS_MCP_TENANT_ID": "<your tenant uuid>",
        "POSTGRES_HOST": "127.0.0.1",
        "POSTGRES_PASSWORD": "<...>",
        "QDRANT_API_KEY": "<...>",
        "MINIO_ROOT_PASSWORD": "<...>",
        "WOXPAS_DEV_DEK_HEX": "<...>"
      }
    }
  }
}
```

> This config file holds your tenant's **raw DEK** and service passwords in
> plaintext. It's how MCP passes environment to the child process — protect the
> file with restrictive filesystem permissions.

### Cursor

See [docs/mcp/cursor.md](mcp/cursor.md) for the equivalent `mcp.json`.

### Any MCP client

Any MCP SDK works over stdio. The server logs to **stderr** (stdout is the
JSON-RPC channel). It **fails closed** if `WOXPAS_MCP_TENANT_ID` is missing.
Error envelopes carry `{code, message, retriable}` — see
[docs/mcp/tools.md](mcp/tools.md) for the code list.

Once connected, a good agent session pattern:

1. `resurface_get_context` once — the "Thinking Brain" background frame.
2. `resurface_get_preferences` — how the user likes answers.
3. `memory_recall` / `memory_ask` for the actual work.
4. `resurface_record_observation` after each tool call so patterns accrue.

---

## 13. Day-2 operations

The `woxpas-cli` covers the common operational tasks. Run any command with
`--help` for its full flags.

**Cloud policy** — control whether a tenant's data may reach cloud LLMs (default
off for everything):

```bash
uv run woxpas-cli set-cloud-policy --tenant-id "$TID" --target personal true
uv run woxpas-cli set-cloud-policy --tenant-id "$TID" --target sensitive false
# Dry-run the gate without an LLM call:
uv run woxpas-cli policy-gate-test --tenant-id "$TID" --model deepseek-chat \
  --profile personal --sensitivity standard --expect deny
```

**Sensitivity** — manually mark a document sensitive (monotonic; can only be
downgraded by an explicit admin action):

```bash
uv run woxpas-cli mark-sensitive --tenant-id "$TID" --doc-id "$DID" \
  --value sensitive --reason "contains medical info"
```

**Inspect a document** — everything about a doc, optionally decrypting payloads
(needs `WOXPAS_DEV_DEK_HEX`):

```bash
uv run woxpas-cli inspect-doc "$DID" --tenant-id "$TID" --with-blocks --with-canonical
```

**Scopes** (virtual folders / projects):

```bash
uv run woxpas-cli create-source --tenant-id "$TID" --type slack --name "Slack import"
uv run woxpas-cli create-scope  --tenant-id "$TID" --name "Alpha Project" --slug alpha-project
```

**Test an LLM backend** (verifies a cloud key / local model round-trips — bypasses
the policy gate, so use only for connectivity checks):

```bash
uv run woxpas-cli llm-test --model qwen2.5-1.5b-instruct-q4km --prompt "ping"
```

**Conflicts** — list, inspect, and resolve knowledge/preference conflicts over
HTTP (`GET /conflicts`, `GET /conflicts/{id}`, `POST /conflicts/{id}/resolve`) or
via the `memory_list_conflicts` / `memory_get_conflict` / `memory_resolve_conflict`
MCP tools. Resolution shapes: `pick`, `coexistence`, `progression`, `dismissed`.

---

## 14. Running the tests

Smoke batteries live in `tests/smoke/`. They spin up real requests against a
running stack (start the infra + worker + API first) and assert pass/fail.

```bash
# Comprehensive MCP coverage (28 tools, RLS canary, parallel recalls)
tests/smoke/phase_5a55_mcp_full.sh

# Resurfacing layer
tests/smoke/phase_5a31_resurfacing.sh

# HTTP ingestion + RLS
tests/smoke/phase_5a50_ingest_text.sh

# Phase 4 synthesis (live LLM — slower)
uv run --env-file .env python tests/smoke/phase_4_synthesis.py
```

> **Do not run smoke batteries in parallel.** They share the local Qwen / BGE
> models and contend for CPU. Run them one at a time. They use a fixed test
> tenant and a "fake" canary tenant to verify cross-tenant isolation.

Static checks:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy            # workspace is mypy --strict
```

---

## 15. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| API returns `401` on every request | `dev`/`single_tenant_local` mode requires the `X-Woxpas-Tenant-Id` header (or a `WOXPAS_DEV_TENANT_ID_DEFAULT` in `.env` for single-tenant). |
| API returns `401` even with a header | These modes are **rejected on non-loopback hosts**. Keep `API_HOST=127.0.0.1`, or switch to `jwt`. |
| Documents stick at `received` | Worker isn't running, or ClamAV isn't healthy yet. Check `docker compose ps` and the worker logs. |
| Ingestion silently accepts a virus | Someone set `CLAMAV_ENABLED=false`. Re-enable it. |
| `DEK unwrap failed` | Wrong passphrase or a tampered envelope. In dev, check `WOXPAS_DEV_DEK_HEX` matches the tenant. |
| MCP server won't start | Missing `WOXPAS_MCP_TENANT_ID` (fail-closed by design). Check the agent's `env` block. Read the server's **stderr** for the reason. |
| Recall returns nothing for a fresh doc | It isn't `indexed` yet. `inspect-doc` to see its status; embedding is the last blocking step before retrieval. |
| Cloud model call is denied | The tenant's cloud policy is off for that profile/sensitivity. Use `policy-gate-test` to see which gate fired, then `set-cloud-policy`. |
| Boot daemon won't start | `woxpas-cli unlock` needs a TTY for the passphrase. Verify the tenant exists in Postgres and isn't locked (`status=active`). If the daemon isn't running, the system falls back to `WOXPAS_DEV_DEK_HEX` silently. |

Logs are structured JSON. `LOG_LEVEL=DEBUG` in `.env` gives per-stage detail.

---

## 16. Toward a networked / cloud deployment

v0.1 is designed for sovereign single-user local use. Before exposing Woxpas
beyond `127.0.0.1`, you **must** do the following (details and rationale in
[SECURITY.md](../SECURITY.md)):

1. **Switch `API_AUTH_MODE=jwt`.** The `dev` / `single_tenant_local` modes trust
   a client-supplied tenant header and are loopback-only. Configure the JWT
   public key, audience, and issuer.
2. **Put the API behind a reverse proxy** that terminates TLS (ideally mTLS) and
   enforces rate limiting — the API has none built in.
3. **Enable the internal Docker network** (`internal: true` in
   `docker-compose.yml`) after the first model fetch, and keep all data services
   port-less.
4. **Use the boot daemon for key management.** The `woxpas-unlock` daemon
   eliminates the plaintext DEK from `.env` — run it at boot (systemd / launchd)
   so the worker/API/MCP pick up the DEK from the Unix socket. Omit
   `WOXPAS_DEV_DEK_HEX` from `.env` in production.
5. **Rotate every secret** that ever lived in a working-tree `.env`.

The topology is intentionally lift-and-shift friendly — workers are pull-based,
the outbox can be swapped for NATS/Kafka, and the object store is S3-compatible —
but the auth + key-management gaps above are load-bearing prerequisites.

---

## 17. Next steps

You have a running memory layer. Here's where to go next:

- **[examples.md](../docs/examples.md)** — concrete scenarios: what Woxpas
  actually does for you. Cross-agent memory, anti-fade, citation verification,
  conflict detection, prepared actions.
- **[docs/mcp/workflows.md](mcp/workflows.md)** — end-to-end MCP session
  walkthroughs. Recall → drill down → verify. Ingest → scope. Conflict
  awareness. Prepared action lifecycle. Session wrap-up.
- **[docs/mcp/system-prompt.md](mcp/system-prompt.md)** — copy-paste system
  instructions for your agent. Tells it to use Woxpas for memory instead of
  its own ephemeral context. Versions for Claude Desktop, Cursor, and any
  MCP-capable agent.
- **[docs/mcp/tools.md](mcp/tools.md)** — full 28-tool inventory with
  session patterns and error codes.
- **[docs/mcp/](mcp/)** — wire up Claude Desktop, Cursor, or any custom
  MCP client.
