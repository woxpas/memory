<!-- Thanks for contributing to Woxpas! -->

## What and why

<!-- What does this change do, and what problem does it solve? -->

## Four-pillar check

- [ ] **Speed** — no blocking I/O added to the API/worker request path; no new latency on a default path.
- [ ] **Accuracy** — no LLM trusted alone for a security signal; errors are surfaced, not silently swallowed.
- [ ] **Security** — no secrets committed; tenant scoping preserved (RLS + repo filters); untrusted content stays wrapped.
- [ ] **Sovereignty** — no non-permissive runtime dependency; no cloud call added to a default path.

If this trades one pillar against another, name the trade here:

## Checks

- [ ] `uv run ruff check .` passes
- [ ] `uv run mypy packages` introduces no new errors
- [ ] `uv run pytest packages` passes
- [ ] Relevant `tests/smoke/` battery run locally (if behaviour changed) — which: 
- [ ] Docs updated (README / docs/ / migration comments) if behaviour or config changed

## Notes for reviewers

<!-- Anything that needs a careful look, or a commitment this trades against. -->
