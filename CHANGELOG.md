# Changelog

All notable changes to Woxpas are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Team memory (multi-member tenants).** A tenant can now be a shared team:
  many members, each with their own passphrase-wrapped copy of the one shared
  DEK (no plaintext key ever persisted), roles (`viewer`/`member`/`admin`/
  `owner`), per-member attribution (`captured_by_member_id` / `actor_member_id`
  on documents, observations, prepared actions, and audit rows), per-member
  preferences & "self" identity, and private scopes (owner-only, RLS-enforced).
  New CLI: `add-member` / `list-members` / `remove-member`, and
  `create-scope --private`. New API: `GET/PATCH/DELETE /members`. New env:
  `WOXPAS_MCP_MEMBER_ID` (bind an MCP server to a member) and
  `API_REQUIRE_MEMBERSHIP` (reject non-member tokens in jwt mode). Role
  enforcement and attribution apply in `jwt` mode; a solo install is a team of
  one and behaves exactly as before. Migrations 032–035, 037. See
  `docs/GETTING_STARTED.md` §13b and `docs/design/team-memory.md` (with
  follow-up plans `me-resolution.md`, `private-content.md`, `dek-rotation.md`).
  Verified live end-to-end over the HTTP API (jwt) and the MCP stdio server.
- Open-source release scaffolding: `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`,
  `NOTICE`, issue/PR templates, and a GitHub Actions CI workflow (lint + type
  check).
- `docker-compose.override.yml.example` so a fresh clone has the local-dev
  port-exposure template.
- **Simpler setup.** `woxpas-cli up` / `woxpas-cli down` start and stop the whole
  instance (datastores + worker + API + dreaming worker) with one command —
  no more three always-on terminals; logs land in `~/.woxpas/logs/`.
  `woxpas-cli install-claude-desktop` merges the MCP server into Claude Desktop's
  config safely (OS-path detection, backup, leaves other servers untouched).
  Bootstrap's final screen and the README now point at this 3-command path.
- **Docker-only install** (no `uv`, no host Python 3.12): `./install.sh "Your
  Name"` builds one image for all app processes, generates secrets
  (`woxpas-cli init-env` via the `cli` compose service), starts the stack, creates
  the tenant, and wires Claude Desktop (stdlib `scripts/install_claude_desktop.py`).
  The app runs entirely in containers — verified end-to-end (migrations,
  worker + dreaming worker boot clean, DEK loaded from `.env`). `docker compose`
  profiles: default = datastores + migrations; `app` = workers; `api` = the
  HTTP gateway (off by default; pinned to `API_AUTH_MODE=jwt` with no published
  port); `mcp`/`cli` for one-shot runs.
- `install-claude-desktop --docker` points Claude at the containerized MCP server
  (`docker compose run`) so no host `uv`/Python is needed to use it.
- Cross-device access from the Docker install: a `mcp-http` service (opt-in
  `remote` profile) runs the HTTP MCP transport on `127.0.0.1:7801` for a TLS
  tunnel (Tailscale/Cloudflare) to front — `docker compose --profile remote up -d`
  (see docs/mcp/mobile.md).

### Changed
- Curated the `ruff` configuration to reflect the project's house style
  (documented per-rule ignores and per-file scopes); `ruff check .` is now
  green and CI-enforced.
- `mypy --strict` is now **green** across the whole workspace (was 324 errors)
  and CI-enforced. Enabled the pydantic mypy plugin; scoped stub-less
  third-party libraries in `[tool.mypy.overrides]`.

### Fixed
- The `woxpas-cli` no longer dumps a raw `asyncpg` traceback when it can't
  authenticate to or reach Postgres; it prints a clean, actionable message
  (wrong credentials vs. stack not running) and exits non-zero.
- Docker-only install (caught by a clean-clone test): `woxpas-cli init-env` now
  fills every key from `.env.example` even when `.env` already exists as an empty
  file (install.sh `touch`es it for the bind-mount), so the generated `.env` is
  complete; `woxpas-cli init-tenant --write-env` writes the tenant id + DEK to
  `.env` so the containers can decrypt (standalone init-tenant previously only
  created the row); `install.sh` runs `init-env` with `-T` so the TTY stays free
  for the interactive passphrase prompt.
- Bootstrap no longer prints a contradictory "leave empty for a no-passphrase
  install" hint and then rejects an empty passphrase; the prompt is now honest
  (a passphrase is required).
- `docker-compose.yml` referenced `packages/*/Dockerfile` files that don't exist
  (a plain `docker compose up` would fail to build); the app services now build
  from the real root `Dockerfile` and are profile-gated so the default
  `docker compose up` brings up only the datastores.
- `resurface_build_identity`: persona-tone JSON is now built with `json.dumps`
  instead of string concatenation, so a tone value containing quotes or braces
  can no longer corrupt the stored profile or silently fail the write; the
  write path now logs on failure instead of swallowing it.
- `memory_get_chunks` MCP tool: fixed a crash — it passed `surrounding=` to
  `recall_chunk_with_context`, whose parameter is `context_window`, so any call
  with `context_window > 0` raised `TypeError`. (Caught by the type checker.)
- `woxpas-dreaming-worker`: fixed a wrong import
  (`build_llm_backend` from `woxpas_synthesis.engine`; it lives in
  `woxpas_runtime`) that would `ImportError` when the predicted-question cache
  used a local model.

### Security
- Hardened `.gitignore` to exclude model weights and HuggingFace caches at any
  depth (a stray multi-GB cache under `packages/` was otherwise un-ignored).

## [0.1.0] — unreleased

Initial implementation of all four pipelines (ingestion, extraction,
retrieval, synthesis) plus the resurfacing/cognition layer, a 28-tool MCP
surface, bitemporal + RLS data model, envelope encryption at rest, and the
passphrase → Argon2id KEK → DEK boot daemon. See [README.md](README.md) and
[SECURITY.md](SECURITY.md).

[Unreleased]: https://github.com/woxpas/memory/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/woxpas/memory/releases/tag/v0.1.0
