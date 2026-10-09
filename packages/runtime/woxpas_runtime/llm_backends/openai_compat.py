"""OpenAI-compatible backend — covers DeepSeek, Kimi/Moonshot, OpenAI itself,
Together, Groq, Anyscale, Fireworks, vLLM-served models, Ollama, LM Studio,
and any other endpoint that implements `POST /chat/completions` with the
standard request/response shape.

Per-model config lives in ModelArtefact:
  api_base    e.g., 'https://api.deepseek.com/v1'
  api_model   e.g., 'deepseek-chat'    (the provider's model id)
  api_key_env e.g., 'WOXPAS_KEY_DEEPSEEK' (env var name holding the bearer token)

We never log the API key. Audit logs carry token counts + model + backend_kind only.
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


class OpenAiCompatibleBackend:
    """LlmBackend implementation for any OpenAI-compatible chat-completions endpoint."""

    backend_kind = "llm.openai_compat"
    is_cloud = True

    def __init__(self, artefact: ModelArtefact) -> None:
        if not artefact.api_base:
            raise ValueError(f"artefact {artefact.name} missing api_base")
        if not artefact.api_model:
            raise ValueError(f"artefact {artefact.name} missing api_model")
        if not artefact.api_key_env:
            raise ValueError(f"artefact {artefact.name} missing api_key_env")
        self._artefact = artefact
        self._key_env = artefact.api_key_env
        self._url = artefact.api_base.rstrip("/") + "/chat/completions"

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
        """Build the chat-completions request body with model_class branching.

        For reasoning models (o3, DeepSeek-R1):
          - Drop `temperature` + `top_p` — providers reject non-default values.
          - Use `max_completion_tokens` instead of `max_tokens`.
          - DON'T request `response_format=json_object` — most reasoning
            endpoints reject it. Caller falls back to schema reinforcement
            in the system prompt (build_hardened_system_prompt already does
            this).
        Provider-specific extras come from artefact.extra_request_kwargs and
        are merged LAST so they can override anything we set above.
        """
        body: dict[str, Any] = {
            "model": self._artefact.api_model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": stream,
        }
        if stream:
            body["stream_options"] = {"include_usage": True}

        if self._artefact.model_class == "reasoning":
            body["max_completion_tokens"] = max_tokens
            # No temperature / top_p / response_format — providers reject them
            # for reasoning models.
        else:
            body["max_tokens"]  = max_tokens
            body["temperature"] = temperature
            body["top_p"]       = 1.0
            if response_format == "json_object":
                body["response_format"] = {"type": "json_object"}

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
            max_tokens=max_tokens,
            temperature=temperature,
            response_format=response_format,
            stream=False,
        )
        # _build_body always sets stream_options when stream=True; drop the
        # field for non-stream calls (some providers 400 on unexpected keys).
        body.pop("stream_options", None)

        headers = {
            "Authorization": f"Bearer {self._api_key()}",
            "Content-Type": "application/json",
        }

        log.info("llm.cloud.request",
                 backend=self.backend_kind, model=self.name,
                 url=self._artefact.api_base, max_tokens=max_tokens)

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

        choice = (payload.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        text = msg.get("content") or ""
        usage = payload.get("usage") or {}

        return LlmResponse(
            text=text,
            finish_reason=str(choice.get("finish_reason") or "stop"),
            usage=TokenUsage(
                prompt_tokens=int(usage.get("prompt_tokens", 0)),
                completion_tokens=int(usage.get("completion_tokens", 0)),
                total_tokens=int(usage.get("total_tokens", 0)),
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
        """Stream tokens via Server-Sent Events.

        OpenAI-compatible endpoints return `data: {...json...}\\n\\n` lines.
        The final line is `data: [DONE]`. We also request stream_options
        with include_usage=true so providers that support it (OpenAI itself,
        DeepSeek, Together) emit a final usage chunk before [DONE]. Providers
        that ignore the field still work — we just stamp zero usage.

        For reasoning models, _build_body drops temperature/top_p/response_format
        and uses max_completion_tokens. The stream shape stays the same.
        Reasoning-model "thinking" tokens are NOT exposed in the SSE delta
        content for OpenAI-compat endpoints (they're billed but hidden), so
        we don't need to filter them.
        """
        body = self._build_body(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            response_format=response_format,
            stream=True,
        )

        headers = {
            "Authorization": f"Bearer {self._api_key()}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

        log.info("llm.cloud.stream_request",
                 backend=self.backend_kind, model=self.name,
                 url=self._artefact.api_base, max_tokens=max_tokens)

        finish_reason: str | None = None
        usage_obj: TokenUsage | None = None

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
                        continue
                    data = line.removeprefix("data:").strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        log.warning("llm.cloud.stream_bad_chunk",
                                    backend=self.backend_kind,
                                    preview=data[:120])
                        continue
                    # Two chunk shapes possible:
                    # 1. content delta: {"choices": [{"delta": {"content": "..."},
                    #                                  "finish_reason": null|str}]}
                    # 2. usage-only: {"choices": [], "usage": {...}}
                    choices = chunk.get("choices") or []
                    if choices:
                        c0 = choices[0]
                        delta = c0.get("delta") or {}
                        text_piece = delta.get("content") or ""
                        fr = c0.get("finish_reason")
                        if fr:
                            finish_reason = str(fr)
                        if text_piece:
                            yield StreamDelta(text=text_piece, done=False)
                    if (u := chunk.get("usage")):
                        usage_obj = TokenUsage(
                            prompt_tokens=int(u.get("prompt_tokens", 0)),
                            completion_tokens=int(u.get("completion_tokens", 0)),
                            total_tokens=int(u.get("total_tokens", 0)),
                        )

        yield StreamDelta(
            text="",
            done=True,
            finish_reason=finish_reason or "stop",
            usage=usage_obj or TokenUsage(),
        )
