# Contributing to Woxpas

Thanks for your interest in Woxpas. This is a young project; contributions,
bug reports, and design discussion are all welcome.

## Design principle: the four pillars

Every change is scored against four pillars — **speed, accuracy, security, and
sovereignty**. A change that regresses one to serve another should say so
explicitly in the PR description and name the trade. A change that violates a
core architecture commitment (see `docs/` and the migration comments) should
name the commitment it's trading against.

Concretely, be skeptical of changes that:

- add network/cloud calls to a default path (sovereignty — cloud is opt-in and
  gated per tenant),
- introduce a non-permissive runtime dependency (sovereignty — the default
  dependency set is MIT / Apache-2.0 / BSD / MPL only),
- trust an LLM for a security signal (accuracy/security — security signals
  always combine non-LLM heuristics with the LLM, never the LLM alone),
- add blocking I/O to the API or worker request path (speed).

## Development setup

Prerequisites: [`uv`](https://github.com/astral-sh/uv), Python 3.12+, Docker +
Docker Compose (for the datastores the smoke tests need).

```bash
uv sync --all-packages        # install the workspace + dev tools
```

## Checks to run before opening a PR

```bash
uv run ruff check .           # lint — required to be green (CI enforces this)
uv run mypy packages          # type check — see "Type checking" below
```

Formatting is **manual** — the codebase uses hand-aligned columns in places,
so `ruff format` is intentionally *not* enforced. Match the style of the file
you're editing. If you want your editor to help, configure a 100-column guide,
but don't run `ruff format` across existing files.

### Type checking

The workspace targets `mypy --strict` (`[tool.mypy] strict = true`). Full
strict coverage is **in progress** — there is known type debt, so `mypy
packages` is currently an advisory signal, not a hard gate, and CI runs it
non-blocking. Please don't *add* new type errors, and fixing existing ones in
files you touch is very welcome. The goal is a fully green `mypy --strict`.

### Smoke tests

The end-to-end behaviour is covered by the batteries under `tests/smoke/`
(bash drivers + Python MCP clients). They need the full local stack running
(Postgres, Qdrant, MinIO, ClamAV, local models), so they are **not** run in CI.

```bash
uv run woxpas-cli bootstrap   # one-time: infra + tenant + models
# then run a battery, e.g.:
bash tests/smoke/phase_5a52_memory_tools.sh
```

They share local models — **run them sequentially, not in parallel.**

### Unit tests

Unit tests live beside the code they cover, under `packages/<pkg>/tests/`, and
run without any infrastructure:

```bash
uv run pytest packages
```

Prefer unit tests for the deterministic pieces (crypto, citation verifier,
intent rules, ID derivation, bitemporal logic). They're fast, they gate in CI,
and they're where regressions are cheapest to catch.

## Reporting bugs and requesting features

Open a GitHub issue using the templates. For anything security-sensitive, do
**not** open a public issue — follow the private process in
[SECURITY.md](SECURITY.md).

## License of contributions

By contributing, you agree that your contributions are licensed under the
Apache License, Version 2.0, the same license as the project (see `LICENSE`).
