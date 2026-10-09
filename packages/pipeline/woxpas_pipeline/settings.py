"""Pipeline settings (separate from storage settings to keep concerns isolated)."""

from __future__ import annotations

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ClamAVSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CLAMAV_", extra="ignore")
    host: str = "clamav"
    port: int = 3310
    timeout_s: float = 30.0
    enabled: bool = True


class PipelineSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    pipeline_version: str = "0.1.0"
    accel_backend:    str = "auto"        # cuda | metal | rocm | cpu | auto

    # Model selection — resolved via the runtime catalog. Override per install
    # via env vars (SUMMARY_MODEL_NAME, EMBED_MODEL_NAME) or programmatically
    # via PipelineSettings(summary_model_name=...). UI-driven model swapping
    # in the future writes to the same settings.
    summary_model_name:    str | None = None         # None → runtime default (DEFAULT_SUMMARY_MODEL_NAME)
    extraction_model_name: str | None = None         # defaults to summary model if unset
    embed_model_name:      str | None = None         # None → runtime default
    asr_model_name:        str | None = None         # None → runtime default
    reranker_model_name:   str | None = None         # None → runtime default; v0.2 swap to v2-m3 here

    # Chunk parameters (configurable per install)
    chunk_target_tokens:     int = 768
    chunk_hard_max_tokens:   int = 1024
    chunk_min_tokens:        int = 64    # coalesce trailing remainders shorter than this

    # Stage concurrency (per worker process). 0 = "auto" — derived from hwprobe
    # at worker startup. Explicit non-zero values are honored as-is.
    av_concurrency:        int = 0
    detect_concurrency:    int = 0
    chunk_concurrency:     int = 0
    summarize_concurrency: int = 0
    embed_concurrency:     int = 0

    # Thread detection — burst captures from the same source/author within this
    # window get auto-threaded together (same thread_id).
    thread_window_seconds: int = 60

    # Supersession candidate window (Type-1 conflict pre-pass)
    supersession_window_seconds: int = 600
    supersession_similarity_threshold: float = 0.85

    # Scope auto-assignment — match summary embedding against scope centroids
    # / description embeddings; assign top match if cosine > threshold.
    # BGE-M3 dense cosines between a doc summary and a scope description
    # typically land 0.55–0.85 even for clearly-related content; 0.65 is a
    # tested sweet spot (catches "Squat 5x5..." → 'Workout' scope at 0.70,
    # rejects unrelated content at 0.46). Adjust per install if desired.
    scope_assign_enabled: bool = True
    scope_assign_threshold: float = 0.65
    # Phase 5a.3.4 Track 2a: lower threshold for the second pass when the
    # ingesting source is captured_by='agent'. Agents have conversation
    # context; a near-miss at the normal threshold is highly likely to
    # be a real fit. Same threshold as the dreamer's ScopeReassigner so
    # behavior is consistent across the two timing windows.
    scope_assign_threshold_agent: float = 0.55


class DevDekSettings(BaseSettings):
    """Bridge to the boot service. In production, the boot service injects
    DEK material via a Unix socket and this env var is unset.

    For dev/testing only. Boot service replaces this in M1.
    """
    model_config = SettingsConfigDict(env_prefix="WOXPAS_DEV_", extra="ignore")
    dek_hex: SecretStr | None = None
    tenant_id_default: str | None = None  # single-tenant dev installs
