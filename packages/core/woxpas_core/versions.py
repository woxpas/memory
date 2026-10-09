"""Pipeline + model version constants.

Every artefact stamped with these values. Bumping is what triggers selective
re-runs (e.g., re-summarize all docs at summary_model_version < new). Keep
forward-compatible: never reuse a version string for different content.
"""

PIPELINE_VERSION: str = "0.1.0"

# NOTE: model_version strings are now derived from each backend at runtime
# (see woxpas_runtime.LlamaSummarizer.model_version etc). The constants below
# are kept as fallback labels for tests / record-keeping.
EMBED_MODEL_VERSION:   str = "bge-m3-onnx@1"
RERANK_MODEL_VERSION:  str = "bge-reranker-v2-m3-int8@1"
SUMMARY_MODEL_VERSION: str = "qwen2.5-1.5b-instruct-q4km@1"
ASR_MODEL_VERSION:     str = "whisper-small-ct2@1"

# Per-profile prompt/version. Bump on prompt change to trigger re-summary
# of affected docs without reprocessing the rest. Aligned with the 10
# consolidated processing_profile values from migration 012.
PROFILE_VERSIONS: dict[str, str] = {
    "personal":           "0.1.0",
    "identity_seed":      "0.1.0",
    "creative_artefact":  "0.1.0",
    "contract":           "0.1.0",
    "reference":          "0.1.0",
    "code":               "0.1.0",
    "correspondence":     "0.1.0",
    "calendar_event":     "0.1.0",
    "agent_log":          "0.1.0",
    "custom":             "0.1.0",
}
