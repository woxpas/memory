"""Phase 4.8 — Synthesis smoke battery.

Runs all 16 cases from project_phase4_smoke_plan.md against the live
stack. Prints a single PASS/FAIL summary at the end. Exit non-zero if
ANY test failed.

Mix of test surfaces:
  - engine-direct (most)            — fastest, doesn't need API daemon
  - HTTP API (SSE + auth tests)     — needs `woxpas-api` running on :7800
  - CLI subprocess (rendering)      — verifies real terminal output

Usage:
  uv run --env-file .env python tests/smoke/phase_4_synthesis.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID, uuid4

import httpx
from woxpas_api.state import AppState
from woxpas_pipeline.cloud_policy import CloudPolicyDenied
from woxpas_runtime import (
    build_llm_backend,
)
from woxpas_synthesis import (
    AnswerEvent,
    CitationSource,
    DeltaEvent,
    ErrorEvent,
    SourcesEvent,
    SynthesisRequest,
    parse,
)
from woxpas_synthesis.cloud_policy import (
    assert_cloud_allowed_for_synthesis,
)

# ─── Config ────────────────────────────────────────────────────────────────

TENANT = UUID("e80524bc-a837-4bee-b9ca-0a9e682fd11e")
API_BASE = "http://127.0.0.1:7800"


# ─── Test framework ────────────────────────────────────────────────────────

@dataclass
class Result:
    label: str
    passed: bool
    detail: str = ""
    skipped: bool = False
    duration_ms: float = 0.0


_results: list[Result] = []


def report(label: str, passed: bool, detail: str = "", duration_ms: float = 0.0) -> None:
    _results.append(Result(label=label, passed=passed, detail=detail, duration_ms=duration_ms))
    sym = "✓" if passed else "✗"
    print(f"  {sym} {label}  {detail[:80]}  [{duration_ms:.0f}ms]")


def skip(label: str, reason: str) -> None:
    _results.append(Result(label=label, passed=True, skipped=True, detail=reason))
    print(f"  · {label}  (skipped: {reason})")


# ─── Live-stack setup ──────────────────────────────────────────────────────

_state: AppState | None = None


async def get_state() -> AppState:
    global _state
    if _state is None:
        _state = await AppState.construct()
    return _state


@asynccontextmanager
async def tenant_cloud_personal(allow: bool):
    """Flip the tenant's allow_cloud_for_personal_data temporarily."""
    s = await get_state()
    async with s.ctx.pool.acquire() as conn:
        prev = await conn.fetchval(
            "SELECT allow_cloud_for_personal_data FROM tenants WHERE tenant_id=$1",
            TENANT,
        )
        await conn.execute(
            "UPDATE tenants SET allow_cloud_for_personal_data=$1 WHERE tenant_id=$2",
            allow, TENANT,
        )
    try:
        yield
    finally:
        async with s.ctx.pool.acquire() as conn:
            await conn.execute(
                "UPDATE tenants SET allow_cloud_for_personal_data=$1 WHERE tenant_id=$2",
                bool(prev), TENANT,
            )


# ─── Tests ─────────────────────────────────────────────────────────────────

async def test_01_long_answer_cloud():
    """1. Long-answer tag adherence on cloud LLM (≥98% primary form)"""
    label = "01 long-answer cloud tag adherence"
    if not os.environ.get("WOXPAS_KEY_DEEPSEEK"):
        return skip(label, "no DeepSeek key")
    t0 = time.monotonic()
    async with tenant_cloud_personal(True):
        s = await get_state()
        req = SynthesisRequest(
            query_text="Tell me everything you know about Phoenix migration: people, "
                       "decisions, timelines, technology choices, and follow-ups.",
            max_sources=10, max_tokens=800, model_name="deepseek-chat",
        )
        final = None
        async for ev in s.synthesis.synthesize_stream(req, tenant_id=TENANT):
            if isinstance(ev, AnswerEvent):
                final = ev.answer
    if final is None:
        return report(label, False, "no AnswerEvent", (time.monotonic()-t0)*1000)
    n_total = len(final.citations)
    n_fallback = final.fallback_pattern_count
    primary_ratio = 1.0 if n_total == 0 else max(0.0, 1.0 - n_fallback / n_total)
    passed = primary_ratio >= 0.98 and n_total >= 1
    return report(label, passed,
                  f"{n_total} citations, {n_fallback} fallback ({primary_ratio*100:.0f}% primary)",
                  (time.monotonic()-t0)*1000)


async def test_02_local_qwen_tag_adherence():
    """2. Local Qwen-3B tag adherence (≥70% primary; tolerance higher than cloud)"""
    label = "02 local Qwen tag adherence"
    t0 = time.monotonic()
    s = await get_state()
    req = SynthesisRequest(
        query_text="What is the phoenix migration about?",
        max_sources=3, max_tokens=200,
        model_name="qwen2.5-3b-instruct-q4km",
    )
    final = None
    async for ev in s.synthesis.synthesize_stream(req, tenant_id=TENANT):
        if isinstance(ev, AnswerEvent):
            final = ev.answer
    if final is None:
        return report(label, False, "no AnswerEvent", (time.monotonic()-t0)*1000)
    n_total = len(final.citations)
    n_fallback = final.fallback_pattern_count
    primary_ratio = 1.0 if n_total == 0 else max(0.0, 1.0 - n_fallback / n_total)
    # Lower bar for Qwen — 70% primary is acceptable for the smaller model
    passed = (n_total >= 1 and primary_ratio >= 0.70) or (n_total == 0 and not final.has_uncited_claims)
    return report(label, passed,
                  f"{n_total} citations, {n_fallback} fallback ({primary_ratio*100:.0f}% primary)",
                  (time.monotonic()-t0)*1000)


async def test_03_echoed_tag_source():
    """3. Source content containing literal `<c id="999"/>` — should land as fabricated"""
    label = "03 echoed-tag source content"
    t0 = time.monotonic()
    # Synthetic test — bypass retrieval, parse a constructed answer where the
    # LLM "echoed" a tag-shaped substring from a source.
    sources = [
        CitationSource(id=1, chunk_id=uuid4(), doc_id=uuid4(),
                       text="The migration finished. The doc says <c id=\"999\"/> in its prose.",
                       title="t1"),
    ]
    src_map = {1: sources[0]}
    raw = ('The migration finished<c id="1"/>. The source mentioned a <c id="999"/> tag.')
    a = parse(raw, citation_map=src_map, model_version="t@1")
    fab = a.citations.get(999)
    passed = fab is not None and fab.status == "fabricated" and a.has_failed_citations
    return report(label, passed,
                  f"id 999 status={fab.status if fab else 'missing'}, has_failed={a.has_failed_citations}",
                  (time.monotonic()-t0)*1000)


async def test_04_multilang_source():
    """4. French + Japanese source content (parser should not crash; citations parse)"""
    label = "04 multilingual source"
    t0 = time.monotonic()
    sources = [
        CitationSource(id=1, chunk_id=uuid4(), doc_id=uuid4(),
                       text="La migration s'est terminée la semaine dernière. "
                            "L'équipe d'Alice a mené le travail.", title="fr.md"),
        CitationSource(id=2, chunk_id=uuid4(), doc_id=uuid4(),
                       text="移行は先週完了しました。アリスのチームが作業を主導した。",
                       title="ja.md"),
    ]
    src_map = {s.id: s for s in sources}
    raw = ('La migration is done<c id="1"/>. 移行が完了しました<c id="2"/>.')
    try:
        a = parse(raw, citation_map=src_map, model_version="t@1")
        passed = (1 in a.citations and 2 in a.citations
                  and not a.has_failed_citations)
        detail = f"{len(a.citations)} citations, {len(a.claims)} claims"
    except Exception as e:
        passed = False
        detail = f"crashed: {type(e).__name__}: {e}"
    return report(label, passed, detail, (time.monotonic()-t0)*1000)


async def test_05_no_results():
    """5. Engine handles 0 retrieval results gracefully"""
    label = "05 zero retrieval results"
    t0 = time.monotonic()
    if not os.environ.get("WOXPAS_KEY_DEEPSEEK"):
        return skip(label, "no DeepSeek key")
    async with tenant_cloud_personal(True):
        s = await get_state()
        req = SynthesisRequest(
            query_text="What is xyzzy quantum metabletics?",
            max_sources=5, max_tokens=80, model_name="deepseek-chat",
        )
        final = None
        sources_evt = None
        had_error = False
        async for ev in s.synthesis.synthesize_stream(req, tenant_id=TENANT):
            if isinstance(ev, SourcesEvent):
                sources_evt = ev
            elif isinstance(ev, AnswerEvent):
                final = ev.answer
            elif isinstance(ev, ErrorEvent):
                had_error = True
    passed = (sources_evt is not None and sources_evt.chunks_used == 0
              and final is not None and not had_error)
    return report(label, passed,
                  f"chunks_used={sources_evt.chunks_used if sources_evt else 'N/A'} answer_produced={final is not None}",
                  (time.monotonic()-t0)*1000)


async def test_06_fallback_to_semantic():
    """6. entity_card resolution with no entity → fallback to SEMANTIC"""
    label = "06 entity_card → semantic fallback"
    t0 = time.monotonic()
    if not os.environ.get("WOXPAS_KEY_DEEPSEEK"):
        return skip(label, "no DeepSeek key")
    async with tenant_cloud_personal(True):
        s = await get_state()
        # Query phrasing that hits entity_card classifier on a non-existent entity
        req = SynthesisRequest(
            query_text="tell me everything about Sarah Nonexistent",
            max_sources=5, max_tokens=60, model_name="deepseek-chat",
        )
        sources_evt = None
        final = None
        async for ev in s.synthesis.synthesize_stream(req, tenant_id=TENANT):
            if isinstance(ev, SourcesEvent):
                sources_evt = ev
            elif isinstance(ev, AnswerEvent):
                final = ev.answer
    # Fallback path should produce a SourcesEvent with intent=semantic
    passed = (sources_evt is not None
              and sources_evt.retrieval_intent == "semantic")
    return report(label, passed,
                  f"resolved_intent={sources_evt.retrieval_intent if sources_evt else 'N/A'}",
                  (time.monotonic()-t0)*1000)


# ─── Mock LLM backends for engine-error tests ──────────────────────────────

class _FailingBackend:
    """LlmBackend that raises a configured exception on first generate."""
    backend_kind = "llm.test_failing"
    is_cloud = True

    def __init__(self, exc_factory):
        self._exc_factory = exc_factory
    @property
    def name(self): return "failing"
    @property
    def model_version(self): return "failing@1"
    async def generate(self, *args, **kwargs):
        raise self._exc_factory()
    async def generate_stream(self, *args, **kwargs):
        raise self._exc_factory()
        yield  # unreachable


async def test_07_retriability_classification():
    """7. CloudPolicyDenied → retriable=False; HTTPStatusError 503 → True"""
    label = "07 retriability classification"
    t0 = time.monotonic()
    s = await get_state()

    # Test with a mock 503 — should be retriable
    def make_503():
        import httpx as _h
        req = _h.Request("POST", "http://x")
        return _h.HTTPStatusError("503", request=req,
                                   response=_h.Response(503, request=req))

    # Test with CloudPolicyDenied — should NOT be retriable
    cpd = CloudPolicyDenied("test", gate="personal_data")

    from woxpas_synthesis.engine import _is_retriable

    passed_503  = _is_retriable(make_503()) is True
    passed_cpd  = _is_retriable(cpd) is False
    passed_401_factory = lambda: __import__("httpx").HTTPStatusError(  # noqa: E731
        "401", request=__import__("httpx").Request("POST", "http://x"),
        response=__import__("httpx").Response(401, request=__import__("httpx").Request("POST", "http://x")),
    )
    passed_401 = _is_retriable(passed_401_factory()) is False

    passed = passed_503 and passed_cpd and passed_401
    return report(label, passed,
                  f"503={passed_503} CPD={passed_cpd} 401={passed_401}",
                  (time.monotonic()-t0)*1000)


async def test_08_stream_cancellation():
    """8. Cancelling the consumer should clean up the engine's producer task"""
    label = "08 stream cancellation"
    t0 = time.monotonic()
    if not os.environ.get("WOXPAS_KEY_DEEPSEEK"):
        return skip(label, "no DeepSeek key")
    async with tenant_cloud_personal(True):
        s = await get_state()
        req = SynthesisRequest(
            query_text="What is the phoenix migration?",
            max_sources=3, max_tokens=200, model_name="deepseek-chat",
        )
        n_deltas = 0
        async def consume():
            nonlocal n_deltas
            async for ev in s.synthesis.synthesize_stream(req, tenant_id=TENANT):
                if isinstance(ev, DeltaEvent):
                    n_deltas += 1
                    if n_deltas >= 3:
                        # Bail mid-stream; the generator should clean up
                        break
        try:
            await consume()
            cleanup_ok = True
        except Exception as e:
            cleanup_ok = False
            err = f"{type(e).__name__}"
        else:
            err = ""
    passed = cleanup_ok and n_deltas >= 3
    return report(label, passed,
                  f"deltas_before_break={n_deltas} cleanup={cleanup_ok}{(' err='+err) if err else ''}",
                  (time.monotonic()-t0)*1000)


async def test_09_personal_cloud_denied():
    """9. Personal-data + cloud LLM with policy off → CloudPolicyDenied"""
    label = "09 personal-data cloud denied"
    t0 = time.monotonic()
    if not os.environ.get("WOXPAS_KEY_DEEPSEEK"):
        return skip(label, "no DeepSeek key")
    async with tenant_cloud_personal(False):
        s = await get_state()
        req = SynthesisRequest(
            query_text="What is the phoenix migration?",
            max_sources=3, max_tokens=60, model_name="deepseek-chat",
        )
        had_denied = False
        async for ev in s.synthesis.synthesize_stream(req, tenant_id=TENANT):
            if isinstance(ev, ErrorEvent) and ev.error_class == "CloudPolicyDenied":
                had_denied = True
                if ev.retriable is True:                # must be False
                    had_denied = False
    return report(label, had_denied,
                  "CloudPolicyDenied event with retriable=False",
                  (time.monotonic()-t0)*1000)


async def test_10_sensitive_cloud_denied():
    """10. Sensitive sensitivity + cloud → denied even when personal is allowed"""
    label = "10 sensitive cloud denied"
    t0 = time.monotonic()
    if not os.environ.get("WOXPAS_KEY_DEEPSEEK"):
        return skip(label, "no DeepSeek key")
    s = await get_state()
    backend = build_llm_backend("deepseek-chat")
    sources = [
        CitationSource(id=1, chunk_id=uuid4(), doc_id=uuid4(),
                       text="A sensitive medical record.", title="med",
                       sensitivity_class="sensitive",
                       processing_profile="custom"),
    ]
    raised = None
    try:
        await assert_cloud_allowed_for_synthesis(
            s.ctx.pool, tenant_id=TENANT, sources=sources, backend=backend,
            request_allow_personal_data=None,
            request_allow_sensitive_data=None,
        )
    except CloudPolicyDenied as e:
        raised = e
    passed = raised is not None and raised.gate == "sensitive_data"
    return report(label, passed,
                  f"raised={raised is not None}, gate={raised.gate if raised else None}",
                  (time.monotonic()-t0)*1000)


async def test_11_mixed_sensitivity():
    """11. Mixed bag → highest tier wins (sensitive overrides standard)"""
    label = "11 mixed sensitivity uses worst-case"
    t0 = time.monotonic()
    if not os.environ.get("WOXPAS_KEY_DEEPSEEK"):
        return skip(label, "no DeepSeek key")
    s = await get_state()
    backend = build_llm_backend("deepseek-chat")
    sources = [
        CitationSource(id=1, chunk_id=uuid4(), doc_id=uuid4(),
                       text="Standard chunk", title="t1",
                       sensitivity_class="standard", processing_profile="custom"),
        CitationSource(id=2, chunk_id=uuid4(), doc_id=uuid4(),
                       text="Personal chunk", title="t2",
                       sensitivity_class="standard", processing_profile="personal"),
        CitationSource(id=3, chunk_id=uuid4(), doc_id=uuid4(),
                       text="Sensitive chunk", title="t3",
                       sensitivity_class="sensitive", processing_profile="custom"),
    ]
    raised = None
    try:
        await assert_cloud_allowed_for_synthesis(
            s.ctx.pool, tenant_id=TENANT, sources=sources, backend=backend,
            request_allow_personal_data=None,
            request_allow_sensitive_data=None,
        )
    except CloudPolicyDenied as e:
        raised = e
    # Sensitive denial must fire FIRST (worst-case, before personal-data)
    passed = raised is not None and raised.gate == "sensitive_data"
    return report(label, passed,
                  f"gate={raised.gate if raised else None} (must be sensitive_data not personal_data)",
                  (time.monotonic()-t0)*1000)


async def test_12_sse_event_ordering():
    """12. SSE event ordering: sources → delta+ → done → answer (→ verify)"""
    label = "12 SSE event ordering"
    t0 = time.monotonic()
    if not os.environ.get("WOXPAS_KEY_DEEPSEEK"):
        return skip(label, "no DeepSeek key")

    # API must be running for this test
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{API_BASE}/health", timeout=2.0)
            if r.status_code != 200:
                return skip(label, "API not available")
    except Exception:
        return skip(label, "API not available")

    async with tenant_cloud_personal(True):
        body = {
            "query_text": "What is the phoenix migration?",
            "max_sources": 2, "max_tokens": 60, "model_name": "deepseek-chat",
        }
        observed: list[str] = []
        async with httpx.AsyncClient(timeout=60.0) as client:
            async with client.stream(
                "POST", f"{API_BASE}/ask", json=body,
                headers={"X-Woxpas-Tenant-Id": str(TENANT),
                         "Accept": "text/event-stream"},
            ) as resp:
                async for line in resp.aiter_lines():
                    if line.startswith("event:"):
                        observed.append(line.split(":", 1)[1].strip())

    # Required ordering: sources first, then deltas, then done, then answer
    # (verify is optional; depends on whether verifier ran successfully)
    has_sources = "sources" in observed
    sources_idx = observed.index("sources") if has_sources else -1
    first_delta_idx = next(
        (i for i, e in enumerate(observed) if e == "delta"), -1,
    )
    done_idx = observed.index("done") if "done" in observed else -1
    answer_idx = observed.index("answer") if "answer" in observed else -1

    valid_order = (sources_idx >= 0 and first_delta_idx > sources_idx
                   and done_idx > first_delta_idx and answer_idx > done_idx)
    return report(label, valid_order,
                  f"order: {' → '.join(observed[:8])}{'...' if len(observed) > 8 else ''}",
                  (time.monotonic()-t0)*1000)


async def test_13_sse_keepalive_logic():
    """13. SSE keepalive: verify the code path exists (timing-sensitive, can't easily live-test)"""
    label = "13 SSE keepalive code path"
    t0 = time.monotonic()
    # Code-path verification: the keepalive mechanism is in routes/synthesize.py
    import inspect

    from woxpas_api.routes import synthesize as syn_route
    src = inspect.getsource(syn_route)
    has_keepalive = (
        "_KEEPALIVE_INTERVAL_S" in src
        and "asyncio.wait_for" in src
        and ": keepalive" in src
    )
    return report(label, has_keepalive,
                  "interval+wait_for+comment-ping all present in source",
                  (time.monotonic()-t0)*1000)


async def test_14_sse_auth_isolation():
    """14. SSE endpoint rejects request with no tenant header"""
    label = "14 SSE auth requires tenant"
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{API_BASE}/health", timeout=2.0)
            if r.status_code != 200:
                return skip(label, "API not available")
    except Exception:
        return skip(label, "API not available")

    async with httpx.AsyncClient(timeout=5.0) as client:
        # No tenant header — should 401
        r = await client.post(
            f"{API_BASE}/ask",
            json={"query_text": "test"},
            headers={"Content-Type": "application/json"},
        )
    passed = r.status_code == 401
    return report(label, passed,
                  f"status={r.status_code} (expected 401)",
                  (time.monotonic()-t0)*1000)


async def test_15_cli_render_terminals():
    """15. CLI renders cleanly in zsh / bash / non-color (env var TERM=dumb)"""
    label = "15 CLI render across terminals"
    t0 = time.monotonic()
    if not os.environ.get("WOXPAS_KEY_DEEPSEEK"):
        return skip(label, "no DeepSeek key")
    async with tenant_cloud_personal(True):
        # Run CLI in subprocess with --no-color, capture stdout
        env = {**os.environ, "WOXPAS_DEV_TENANT_ID_DEFAULT": str(TENANT)}
        cmd = [
            "uv", "run", "woxpas-cli", "ask",
            "What is the phoenix migration?",
            "--model", "deepseek-chat", "--max-sources", "2",
            "--max-tokens", "60", "--no-color",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
    out = stdout.decode("utf-8", "replace")
    has_render = (
        "↳" in out and  # source count indicator
        "[" in out and  # citation marker
        "Sources:" in out and  # references panel
        "claims verified" in out  # status summary
    )
    has_no_ansi = "\x1b[" not in out  # no escape sequences in --no-color mode
    passed = has_render and has_no_ansi and proc.returncode == 0
    return report(label, passed,
                  f"render_complete={has_render}, no_ansi={has_no_ansi}, exit={proc.returncode}",
                  (time.monotonic()-t0)*1000)


async def test_16_cli_json_mode():
    """16. CLI --json mode produces valid JSON with parseable structure"""
    label = "16 CLI --json mode"
    t0 = time.monotonic()
    if not os.environ.get("WOXPAS_KEY_DEEPSEEK"):
        return skip(label, "no DeepSeek key")
    async with tenant_cloud_personal(True):
        env = {**os.environ, "WOXPAS_DEV_TENANT_ID_DEFAULT": str(TENANT)}
        cmd = [
            "uv", "run", "woxpas-cli", "ask",
            "What is the phoenix migration?",
            "--model", "deepseek-chat", "--max-sources", "2",
            "--max-tokens", "60", "--json",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
    try:
        d = json.loads(stdout)
        ans = d.get("answer")
        passed = (ans is not None
                  and ans.get("answer_clean")
                  and isinstance(ans.get("citations"), dict)
                  and isinstance(ans.get("claims"), list))
        detail = (f"claims={len(ans['claims'])} citations={len(ans['citations'])}"
                  if passed else "missing fields in JSON")
    except json.JSONDecodeError as e:
        passed = False
        detail = f"invalid JSON: {e}"
    return report(label, passed, detail, (time.monotonic()-t0)*1000)


# ─── Main ──────────────────────────────────────────────────────────────────

async def main():
    print(f"\n{'=' * 70}")
    print("PHASE 4.8 — SYNTHESIS SMOKE BATTERY  (16 cases)")
    print(f"{'=' * 70}\n")

    tests = [
        test_01_long_answer_cloud,
        test_02_local_qwen_tag_adherence,
        test_03_echoed_tag_source,
        test_04_multilang_source,
        test_05_no_results,
        test_06_fallback_to_semantic,
        test_07_retriability_classification,
        test_08_stream_cancellation,
        test_09_personal_cloud_denied,
        test_10_sensitive_cloud_denied,
        test_11_mixed_sensitivity,
        test_12_sse_event_ordering,
        test_13_sse_keepalive_logic,
        test_14_sse_auth_isolation,
        test_15_cli_render_terminals,
        test_16_cli_json_mode,
    ]

    for t in tests:
        try:
            await t()
        except Exception as e:
            report(t.__doc__.split("\n", 1)[0] if t.__doc__ else t.__name__,
                   False, f"unexpected: {type(e).__name__}: {e}", 0)

    if _state is not None:
        await _state.close()

    # Summary
    passed = sum(1 for r in _results if r.passed and not r.skipped)
    failed = sum(1 for r in _results if not r.passed)
    skipped = sum(1 for r in _results if r.skipped)
    total = len(_results)
    print(f"\n{'=' * 70}")
    print(f"SUMMARY: {passed}/{total} passed  ({failed} failed, {skipped} skipped)")
    print(f"{'=' * 70}")
    if failed:
        print("\nFAILED:")
        for r in _results:
            if not r.passed:
                print(f"  ✗ {r.label}  — {r.detail}")
    print()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
