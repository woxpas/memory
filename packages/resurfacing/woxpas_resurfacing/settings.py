"""Resurfacing-side settings — env-var driven, Pydantic-validated.

Tunable knobs for the dreaming worker. Defaults sized for a single-user
sovereign install; multi-tenant cloud deploys can crank up by raising
the interval ceilings + enabling parallel tenant processing (v0.2)."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class DreamingSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="WOXPAS_DREAMING_",
        extra="ignore",
    )

    enabled: bool = True
    """Master switch. When false, the worker process exits cleanly at
    startup. Useful for ops who run the API + ingestion worker but
    want to skip resurfacing computation on their box."""

    tenant_interval_sec: int = 1800
    """Cadence between tenant-scoring passes (default 30 min). At each
    tick the worker iterates active tenants and runs ScoreMaintainer +
    PatternScorer for each. Tighten for fresh signals, loosen for low
    compute. Hard floor of 60s; below that we'd be hammering the DB."""

    matview_interval_sec: int = 1800
    """Cadence for v_mention_velocity matview refresh (default 30 min).
    Refresh is global (all tenants in one query) so this is decoupled
    from per-tenant scoring."""

    active_tenant_window_hours: int = 24
    """A tenant is "active" if it has any timeline event in the last N
    hours. Scoring is skipped for dormant tenants to keep compute O(active)
    not O(total). Crank up for cloud deploys with bursty users; default
    24h covers most sovereign installs."""

    heartbeat_interval_sec: int = 300
    """How often the worker emits a status log line (default 5 min).
    Gives ops a "still alive" signal even when no tenants are active."""

    max_runtime_per_tenant_sec: float = 60.0
    """Per-tenant scoring is bounded — if it exceeds this, log + skip to
    the next tenant. Protects against a stuck tenant blocking the loop.
    Default 60s; should be plenty for tenants up to ~100K active items."""

    enable_question_cache: bool = False
    """Phase 5a.3.1 — construct synthesis stack and run the predicted-
    question pre-cache loop. Disabled by default because it pulls in
    embedder/retrieval/LLM dependencies and is only useful for tenants
    that have set allow_cloud_for_background_jobs=true. Flip to true
    once cloud egress is configured + a tenant has opted in."""

    question_cache_model: str = "deepseek-chat"
    """Cloud LLM used for predicted-question synthesis when
    enable_question_cache=true. Cheap-cloud default. v0.2: per-tenant
    override + budget-aware down-shifting."""
