"""libmagic-based content sniffing.

Don't trust the extension or the client-supplied MIME — sniff the bytes.
Returns (mime, processing_profile_suggestion). The profile suggestion is a
heuristic; the classifier (later session) refines based on content + content
embedding.

python-magic is a thin wrapper over libmagic; the library is permissive (MIT-
ish, BSD-derived) — license-safe for closed source.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass

import structlog

log = structlog.get_logger(__name__)

# Lazy imports so test environments without libmagic still load this module.
_magic = None


def _load_magic():  # type: ignore[no-untyped-def]
    global _magic
    if _magic is None:
        import magic  # type: ignore[import]
        _magic = magic.Magic(mime=True)
    return _magic


@dataclass(slots=True)
class DetectionResult:
    mime: str
    profile_suggested: str | None
    extension_hint: str | None = None


# MIME → processing_profile heuristic. Conservative; classifier overrides.
# Aligned with the 10 consolidated processing_profile values from migration 012.
_MIME_TO_PROFILE: dict[str, str] = {
    "application/pdf":              "reference",
    "application/msword":           "correspondence",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
                                    "correspondence",
    "text/markdown":                "personal",
    "text/x-markdown":              "personal",
    "text/plain":                   "personal",
    "text/html":                    "reference",
    "message/rfc822":               "correspondence",
    "audio/mpeg":                   "personal",
    "audio/wav":                    "personal",
    "audio/x-wav":                  "personal",
    "audio/mp4":                    "personal",
    "audio/x-m4a":                  "personal",
    "audio/ogg":                    "personal",
    "audio/flac":                   "personal",
    "audio/webm":                   "personal",
    "text/calendar":                "calendar_event",
    "application/x-ipynb+json":     "code",
    "application/json":             "code",
    "text/x-python":                "code",
    "text/x-go":                    "code",
    "text/x-rust":                  "code",
}


def detect_bytes(content: bytes, filename: str | None = None) -> DetectionResult:
    """Run libmagic on the first 4KB and return mime + a profile hint.

    For very small payloads (e.g., a 30-word thought), libmagic may return
    'application/octet-stream' — fall back to filename extension and the
    'thought' default.
    """
    head = content[:4096] if len(content) > 4096 else content
    try:
        mime = _load_magic().from_buffer(head)
    except Exception as e:                                    # pragma: no cover
        log.warning("detect.libmagic_failed", error=str(e))
        mime = "application/octet-stream"

    if mime == "application/octet-stream" and filename:
        mime = _by_extension(filename) or mime

    profile = _MIME_TO_PROFILE.get(mime)
    return DetectionResult(
        mime=mime,
        profile_suggested=profile,
        extension_hint=_extension(filename),
    )


@functools.lru_cache(maxsize=512)
def _extension(filename: str | None) -> str | None:
    if not filename or "." not in filename:
        return None
    return filename.rsplit(".", 1)[1].lower()


def _by_extension(filename: str) -> str | None:
    ext = _extension(filename)
    if ext is None:
        return None
    return {
        "txt":   "text/plain",
        "md":    "text/markdown",
        "pdf":   "application/pdf",
        "docx":  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "doc":   "application/msword",
        "html":  "text/html",
        "htm":   "text/html",
        "eml":   "message/rfc822",
        "ics":   "text/calendar",
        "json":  "application/json",
        "ipynb": "application/x-ipynb+json",
        "py":    "text/x-python",
        "go":    "text/x-go",
        "rs":    "text/x-rust",
        "wav":   "audio/wav",
        "mp3":   "audio/mpeg",
        "m4a":   "audio/x-m4a",
        "ogg":   "audio/ogg",
        "flac":  "audio/flac",
        "webm":  "audio/webm",
    }.get(ext)
