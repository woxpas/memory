# Changelog

All notable changes to Woxpas are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Open-source release scaffolding: `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`,
  `NOTICE`, issue/PR templates, and a GitHub Actions CI workflow (lint + type
  check).
- `docker-compose.override.yml.example` so a fresh clone has the local-dev
  port-exposure template.

### Changed
- Curated the `ruff` configuration to reflect the project's house style
  (documented per-rule ignores and per-file scopes); `ruff check .` is now
  green and CI-enforced.
- `mypy --strict` is now **green** across the whole workspace (was 324 errors)
  and CI-enforced. Enabled the pydantic mypy plugin; scoped stub-less
  third-party libraries in `[tool.mypy.overrides]`.

### Fixed
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
retrieval, synthesis) plus the resurfacing/cognition layer, a 27-tool MCP
surface, bitemporal + RLS data model, envelope encryption at rest, and the
passphrase → Argon2id KEK → DEK boot daemon. See [README.md](README.md) and
[SECURITY.md](SECURITY.md).

[Unreleased]: https://github.com/OWNER/woxpas/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/OWNER/woxpas/releases/tag/v0.1.0
