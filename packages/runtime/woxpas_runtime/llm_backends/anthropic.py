"""Anthropic backend (claude-* models).

Anthropic's `/v1/messages` API has a different shape from OpenAI:
  - System message is a top-level `system` field (string), not in the messages list.
  - Messages alternate user/assistant; no system role inside the list.
  - Response shape: content is a list of blocks; first text block is the answer.

We adapt our canonical Message[] (which is OpenAI-shaped) to Anthropic's shape
on the wire. Caller doesn't see the difference.

Auth: ModelArtefact.api_key_env names the env var (e.g., WOXPAS_KEY_ANTHROPIC).
Default api_base: https://api.anthropic.com — overridable per artefact.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx
import structlog

from woxpas_runtime.llm_backend import (
    LlmResponse,
    Message,
    ResponseFormat,
    StreamDelta,
    TokenUsage,
)
from woxpas_runtime.models import ModelArtefact

log = structlog.get_logger(__name__)


_DEFAULT_BASE = "https://api.anthropic.com"
_DEFAULT_VERSION = "2023-06-01"


class AnthropicBackend:
    """LlmBackend implementation for Anthropic's /v1/messages endpoint."""

    backend_kind = "llm.anthropic"
    is_cloud = True

    def __init__(self, artefact: ModelArtefact, *, anthropic_version: str = _DEFAULT_VERSION) -> None:
        if not artefact.api_model:
            raise ValueError(f"artefact {artefact.name} missing api_model")
        if not artefact.api_key_env:
            raise ValueError(f"artefact {artefact.name} missing api_key_env")
        self._artefact = artefact
        self._key_env = artefact.api_key_env
        base = (artefact.api_base or _DEFAULT_BASE).rstrip("/")
        self._url = f"{base}/v1/messages"
        self._version = anthropic_version

    @property
    def name(self) -> str:
        return self._artefact.name

    @property
    def model_version(self) -> str:
        return self._artefact.model_version

    def _api_key(self) -> str:
        key = os.environ.get(self._key_env, "").strip()
        if not key:
            raise RuntimeError(
                f"API key not set for {self._artefact.name}: "
                f"set env var {self._key_env}"
            )
        return key

    def _build_body(
        self,
        messages: list[Message],
        *,
        max_tokens: int,
        temperature: float,
        response_format: ResponseFormat,
        stream: bool,
    ) -> dict[str, Any]:
        """Adapt OpenAI-shape Message[] to Anthropic's body shape.

        Standard models: temperature/top_p OK.
        Reasoning models (extended thinking): temperature MUST be 1.0; the
        artefact carries the `thinking: {type, budget_tokens}` block via
        extra_request_kwargs. max_tokens MUST be > thinking budget — we don't
        validate here (let Anthropic 400 with the precise message), but
        callers configuring a reasoning artefact should size accordingly.
        """
        system_parts: list[str] = []
        chat_msgs: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "system":
                system_parts.append(m.content)
                continue
            chat_msgs.append({"role": m.role, "content": m.content})

        body: dict[str, Any] = {
            "model": self._artefact.api_model,
            "max_tokens": max_tokens,
            "messages": chat_msgs,
            "stream": stream,
        }
        if self._artefact.model_class == "reasoning":
            # Anthropic requires temperature=1.0 with extended thinking.
            body["temperature"] = 1.0
        else:
            body["temperature"] = temperature

        if system_parts:
            body["system"] = "\n\n".join(system_parts)

        if response_format == "json_object":
            body["system"] = (body.get("system", "") +
                              "\n\nRespond with a single valid JSON object only. "
                              "No prose, no markdown fences.").strip()

        if self._artefact.extra_request_kwargs:
            body.update(self._artefact.extra_request_kwargs)
        return body

    async def generate(
        self,
        messages: list[Message],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        response_format: ResponseFormat = "text",
        timeout_s: float = 120.0,
    ) -> LlmResponse:
        body = self._build_body(
            messages,
            max_tokens=max_tokens, temperature=temperature,
            response_format=response_format, stream=False,
        )

        headers = {
            "x-api-key": self._api_key(),
            "anthropic-version": self._version,
            "Content-Type": "application/json",
        }

        log.info("llm.cloud.request",
                 backend=self.backend_kind, model=self.name,
                 url=self._url, max_tokens=max_tokens)

        async with httpx.AsyncClient(timeout=timeout_s) as client:
            try:
                resp = await client.post(self._url, json=body, headers=headers)
                resp.raise_for_status()
                payload = resp.json()
            except httpx.HTTPStatusError as e:
                log.error("llm.cloud.http_error",
                          backend=self.backend_kind, model=self.name,
                          status=e.response.status_code,
                          body=e.response.text[:500])
                raise
            except httpx.HTTPError as e:
                log.error("llm.cloud.transport_error",
                          backend=self.backend_kind, model=self.name, error=str(e))
                raise

        # Anthropic response: content is a list of blocks; concatenate text blocks.
        text_blocks = [
            b.get("text", "")
            for b in (payload.get("content") or [])
            if b.get("type") == "text"
        ]
        text = "".join(text_blocks)

        usage = payload.get("usage") or {}
        return LlmResponse(
            text=text,
            finish_reason=str(payload.get("stop_reason") or "stop"),
            usage=TokenUsage(
                prompt_tokens=int(usage.get("input_tokens", 0)),
                completion_tokens=int(usage.get("output_tokens", 0)),
                total_tokens=int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0)),
            ),
            model=self.name,
            backend_kind=self.backend_kind,
            raw=payload,
        )

    async def generate_stream(
        self,
        messages: list[Message],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        response_format: ResponseFormat = "text",
        timeout_s: float = 120.0,
    ) -> AsyncIterator[StreamDelta]:
        """Stream tokens via Anthropic's Server-Sent Events.

        Anthropic SSE event shapes (relevant ones):
          - message_start         {message: {usage: {input_tokens: N}}}
          - content_block_start
          - content_block_delta   {delta: {type: "text_delta", text: "..."}}
                                  {delta: {type: "thinking_delta", thinking: "..."}}
          - content_block_stop
          - message_delta         {delta: {stop_reason: "..."}, usage: {output_tokens: N}}
          - message_stop

        For reasoning models with extended thinking enabled, the stream emits
        `thinking_delta` events containing the model's internal reasoning
        trace. v0.1: skip these (don't emit StreamDelta for them) — synthesis
        only needs the final answer text. v0.2 will surface them via
        StreamDelta.kind for "show your thinking" UIs.

        We accumulate input_tokens at message_start and output_tokens at
        message_delta to produce the terminal usage.
        """
        body = self._build_body(
            messages,
            max_tokens=max_tokens, temperature=temperature,
            response_format=response_format, stream=True,
        )

        headers = {
            "x-api-key": self._api_key(),
            "anthropic-version": self._version,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

        log.info("llm.cloud.stream_request",
                 backend=self.backend_kind, model=self.name,
                 url=self._url, max_tokens=max_tokens)

        prompt_tokens = 0
        completion_tokens = 0
        finish_reason: str | None = None

        async with httpx.AsyncClient(timeout=timeout_s) as client:
            async with client.stream("POST", self._url, json=body, headers=headers) as resp:
                if resp.status_code >= 400:
                    text = (await resp.aread()).decode("utf-8", "replace")
                    log.error("llm.cloud.stream_http_error",
                              backend=self.backend_kind, model=self.name,
                              status=resp.status_code, body=text[:500])
                    raise httpx.HTTPStatusError(
                        f"stream HTTP {resp.status_code}: {text[:200]}",
                        request=resp.request, response=resp,
                    )
                async for raw_line in resp.aiter_lines():
                    line = raw_line.strip()
                    if not line or not line.startswith("data:"):
                        # `event: <name>` lines are informational; we read
                        # the type from the JSON body itself, so ignore.
                        continue
                    data = line.removeprefix("data:").strip()
                    if not data:
                        continue
                    try:
                        ev = json.loads(data)
                    except json.JSONDecodeError:
                        log.warning("llm.cloud.stream_bad_chunk",
                                    backend=self.backend_kind,
                                    preview=data[:120])
                        continue
                    et = ev.get("type")
                    if et == "message_start":
                        u = (ev.get("message") or {}).get("usage") or {}
                        prompt_tokens = int(u.get("input_tokens", 0))
                    elif et == "content_block_delta":
                        d = ev.get("delta") or {}
                        if d.get("type") == "text_delta":
                            text_piece = d.get("text") or ""
                            if text_piece:
                                yield StreamDelta(text=text_piece, done=False)
                    elif et == "message_delta":
                        d = ev.get("delta") or {}
                        sr = d.get("stop_reason")
                        if sr:
                            finish_reason = str(sr)
                        u = ev.get("usage") or {}
                        if "output_tokens" in u:
                            completion_tokens = int(u.get("output_tokens", 0))
                    elif et == "message_stop":
                        break
                    elif et == "error":
                        err = ev.get("error") or {}
                        raise RuntimeError(
                            f"anthropic stream error: "
                            f"{err.get('type', 'unknown')}: {err.get('message', '')}"
                        )

        yield StreamDelta(
            text="",
            done=True,
            finish_reason=finish_reason or "stop",
            usage=TokenUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
        )
