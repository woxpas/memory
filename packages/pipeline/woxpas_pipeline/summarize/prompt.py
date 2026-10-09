"""Summarize-stage prompt + response parser + tiny-doc shortcut.

Lifted out of woxpas_runtime when we adopted the LlmBackend abstraction. The
runtime no longer knows about prompts — the stage that consumes the LLM owns
its own prompt schema. Local llama.cpp, DeepSeek, Anthropic, etc. all run the
same `Message` list; the backend handles model-specific formatting.

v0.1 used one generic prompt; v0.2.0 extends it with sensitivity classification
(Tier 2 of the 3-tier sensitivity system). The LLM that's already reading the
doc adds a is_sensitive boolean and a short reason to its JSON output — no
extra LLM call, no extra latency.

v0.3.0 wraps the document body with prompt-injection delimiters and uses the
shared hardened-prompt template. Note: Tier 2's is_sensitive flag is ONE
signal among three — Tier 1 (manual flag at upload) and Tier 3a (heuristic
in extract) cover the case where a malicious doc tries to flip Tier 2 via
prompt injection. See feedback_no_shortcuts + project_v02_followups for the
schema-mimicry caveat on small local models.

Tiny-doc shortcut: docs under ~200 chars are their own summary. Skips the
LLM call entirely. Tier 2 obviously can't run on tiny docs (no LLM); they
fall through to Tier 3a (already done) + Tier 3 refine for sensitivity.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from woxpas_runtime import (
    Message,
    build_hardened_system_prompt,
    expect_json_object,
    wrap_source,
)

# Bumped on prompt change → triggers targeted re-summary on docs that ran
# under an older prompt. Treated like a model_version stamp.
#
#   0.1.0 — generic summarizer (just summary + topics)
#   0.2.0 — adds Tier 2 sensitivity classification (is_sensitive + reason)
#   0.3.0 — adds prompt-injection hardening (delimiters + schema rules)
SUMMARY_PROFILE_VERSION = "0.3.0"


_SUMMARY_TASK = """\
Read the document supplied as the source and produce two things in your JSON
output:
  1. A concise summary + topic list of what the document is about.
  2. A privacy classification (Tier 2): does the document contain content
     the user would clearly want kept off third-party clouds?

Privacy classification (is_sensitive=true) MUST cover any of:
  - medical records / diagnoses / prescriptions / patient data
  - financial account details / bank statements / tax documents / payslips
  - government IDs (SSN, passport number, driver's licence)
  - legal documents naming the user personally (NDA, contract, settlement)
  - other content the user would clearly want kept off third-party clouds.
Return is_sensitive=false for general personal notes, fitness/journal
entries, work content, fiction, public knowledge, news, code.

Field shapes:
  summary             — 1-3 sentences capturing what this document is about.
  topics              — 3-5 short noun phrases (1-3 words each).
  is_sensitive        — boolean per the rules above.
  sensitivity_reason  — 1 short phrase (≤80 chars) when is_sensitive=true,
                        empty string otherwise."""

_SUMMARY_SCHEMA = (
    '{"summary": "...", "topics": ["...", ...], '
    '"is_sensitive": true|false, "sensitivity_reason": "..."}'
)


@dataclass(slots=True)
class SummaryResult:
    """Stage-shaped result. Distinct from any backend-level dataclass."""
    summary:            str
    topics:             list[str]
    raw:                str            # provider response text (for debugging)
    model_version:      str            # logical name@version of the LLM used
    profile_version:    str            # SUMMARY_PROFILE_VERSION at the time of summary
    is_sensitive:       bool = False   # Tier 2 LLM judgment
    sensitivity_reason: str = ""       # Tier 2 reason phrase (empty if not sensitive)


def build_messages(
    body: str,
    *,
    doc_id:    UUID | str | None = None,
    max_chars: int = 24000,
) -> list[Message]:
    """Two-message chat completion: system + user. max_chars caps the body so
    we stay inside any provider's context limit (24K chars ≈ 7.5K tokens).

    The body is wrapped in prompt-injection delimiters via wrap_source, with
    sanitize=True — we'd rather mangle a stray collision (real doc text
    containing `<<<WOXPAS_*` is implausible) than refuse the whole document.
    """
    body = body[:max_chars]
    system = build_hardened_system_prompt(
        role="a careful summarizer AND privacy classifier",
        task=_SUMMARY_TASK,
        output_schema=_SUMMARY_SCHEMA,
    )
    user = wrap_source(body, doc_id=str(doc_id) if doc_id else None, sanitize=True)
    return [
        Message(role="system", content=system),
        Message(role="user",   content=user),
    ]


def tiny_doc_summary(body: str) -> SummaryResult:
    """For docs under ~200 chars, the document IS its own summary. Skip the
    LLM call. Sensitivity for tiny docs falls through to Tier 3a (already
    ran in extract) + Tier 3 refine — Tier 2 has nothing to contribute."""
    summary = body.strip().split("\n", 1)[0][:280] or "(empty)"
    return SummaryResult(
        summary=summary, topics=[],
        raw=summary,
        model_version="tiny-doc-shortcut@1",
        profile_version=SUMMARY_PROFILE_VERSION,
        is_sensitive=False,
        sensitivity_reason="",
    )


def parse_summary(raw: str, *, model_version: str) -> SummaryResult:
    """Robust JSON parse via expect_json_object. Falls back to first non-empty
    line if JSON is malformed. Never raises; a bad parse becomes '(no summary
    produced)' so the pipeline never crashes on flaky output. is_sensitive
    defaults to False on parse failure (Tier 3 refine still gets a chance to
    flag)."""
    summary:            str       = ""
    topics:             list[str] = []
    is_sensitive:       bool      = False
    sensitivity_reason: str       = ""
    try:
        obj = expect_json_object(raw)
    except (ValueError, TypeError):
        obj = {}
    if obj:
        summary = (obj.get("summary") or "").strip()
        topics  = [str(t) for t in (obj.get("topics") or []) if str(t).strip()][:5]
        # Defensive truthiness — accept bool, "true"/"false" strings, 1/0.
        raw_sens = obj.get("is_sensitive", False)
        if isinstance(raw_sens, bool):
            is_sensitive = raw_sens
        elif isinstance(raw_sens, str):
            is_sensitive = raw_sens.strip().lower() in {"true", "yes", "1"}
        elif isinstance(raw_sens, (int, float)):
            is_sensitive = bool(raw_sens)
        sensitivity_reason = str(obj.get("sensitivity_reason") or "").strip()[:80]
    if not summary:
        for line in raw.splitlines():
            if line.strip():
                summary = line.strip()[:280]
                break
    return SummaryResult(
        summary=summary or "(no summary produced)",
        topics=topics,
        raw=raw,
        model_version=model_version,
        profile_version=SUMMARY_PROFILE_VERSION,
        is_sensitive=is_sensitive,
        sensitivity_reason=sensitivity_reason,
    )
