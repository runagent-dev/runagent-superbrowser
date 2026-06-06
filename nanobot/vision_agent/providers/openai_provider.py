"""OpenAI vision provider.

Uses the official `openai` async client against the default endpoint.
Requests JSON-mode output so parsing is straightforward. This class is
also the foundation for OpenRouter + Gemini providers, which subclass it
and override the base URL. (GeminiVisionProvider overrides
`chat_with_image` outright, so the adaptation below is OpenAI/OpenRouter
only.)

Request-shape adaptation
------------------------
OpenAI's parameter contract differs across model families:
  - gpt-4o / gpt-4.1   accept `max_tokens` + `temperature`, reject
                       `reasoning_effort`.
  - gpt-5.x / o-series require `max_completion_tokens` (not `max_tokens`),
                       only accept the default `temperature`, and DO accept
                       `reasoning_effort`.
Rather than hardcode a model→params table that rots every release, we send
a best-effort request shape and, on a 400 that names an unsupported
parameter, flip the offending knob and retry. The learned flags are cached
on the instance, so the discovery round-trips happen at most once per
process per model — the compact retry and every later call send the right
shape directly.

Setting `reasoning_effort=minimal` on reasoning models is the OpenAI
analogue of the Gemini provider's `thinking_budget=0`: spatial bbox
extraction wants fast structured output, not deliberation, and minimal
reasoning leaves the whole token budget for the JSON payload. Models that
don't support the knob auto-drop it via the same adaptation path. Disable
entirely with VISION_OPENAI_REASONING_EFFORT="".
"""

from __future__ import annotations

import os
from typing import Optional

from openai import AsyncOpenAI

from .base import ProviderResponse, VisionProvider


class OpenAIVisionProvider(VisionProvider):
    name = "openai"

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: Optional[str] = None,
        max_tokens: int = 1500,
        timeout_s: float = 8.0,
    ) -> None:
        super().__init__(
            model=model,
            api_key=api_key,
            base_url=base_url,
            max_tokens=max_tokens,
            timeout_s=timeout_s,
        )
        # One client per provider instance — the SDK handles connection
        # pooling internally, so we don't spin up a new client per call.
        client_kwargs: dict[str, object] = {
            "api_key": api_key,
            "timeout": timeout_s,
        }
        if base_url:
            client_kwargs["base_url"] = base_url
        self._client = AsyncOpenAI(**client_kwargs)

        # Request-shape flags, discovered lazily from 400s the first time a
        # constrained model rejects a param, then cached (see docstring).
        self._use_max_completion_tokens = False
        self._drop_temperature = False
        # reasoning_effort: "" disables it; the default "minimal" is
        # auto-dropped for models that don't support it.
        self._reasoning_effort: Optional[str] = (
            os.environ.get("VISION_OPENAI_REASONING_EFFORT", "minimal").strip()
            or None
        )

    def _build_kwargs(self, messages: list) -> dict:
        """Assemble create() kwargs from the current learned flags."""
        kwargs: dict[str, object] = {
            "model": self.model,
            "messages": messages,
            "response_format": {"type": "json_object"},
        }
        if self._use_max_completion_tokens:
            kwargs["max_completion_tokens"] = self.max_tokens
        else:
            kwargs["max_tokens"] = self.max_tokens
        # gpt-5.x / o-series only allow the default temperature; once we've
        # learned that, stop sending it at all.
        if not self._drop_temperature:
            kwargs["temperature"] = 0.1
        if self._reasoning_effort:
            kwargs["reasoning_effort"] = self._reasoning_effort
        return kwargs

    def _adapt_from_error(self, exc: Exception) -> bool:
        """Flip a request-shape flag based on a 400 unsupported-parameter
        error. Returns True if something changed (so a retry is worth it),
        False if the error is unrelated (so the caller should re-raise).
        """
        param = str(getattr(exc, "param", None) or "")
        msg = str(getattr(exc, "message", "") or exc).lower()
        changed = False
        if (
            (param == "max_tokens" or "max_tokens" in msg)
            and "max_completion_tokens" in msg
            and not self._use_max_completion_tokens
        ):
            self._use_max_completion_tokens = True
            changed = True
        if (
            (param == "temperature" or "temperature" in msg)
            and not self._drop_temperature
            and ("default" in msg or "unsupported" in msg or "does not support" in msg)
        ):
            self._drop_temperature = True
            changed = True
        if (
            (param == "reasoning_effort" or "reasoning_effort" in msg)
            and self._reasoning_effort is not None
        ):
            self._reasoning_effort = None
            changed = True
        return changed

    async def chat_with_image(
        self,
        *,
        screenshot_b64: str,
        system_prompt: str,
        user_prompt: str,
        mime_type: str = "image/jpeg",
        timeout_s: Optional[float] = None,
    ) -> ProviderResponse:
        data_url = f"data:{mime_type};base64,{screenshot_b64}"
        # Optional per-call timeout override — lets the retry path use a
        # longer deadline than the default without rebuilding the client.
        client = self._client
        if timeout_s is not None:
            client = client.with_options(timeout=timeout_s)
        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    # `image_url` is the universal multimodal-content shape —
                    # OpenAI, OpenRouter, and Gemini's compat endpoint all
                    # accept it.
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ]
        # Adapt to the model's parameter contract on 400s. Each OpenAI 400
        # names one offending param, so a handful of attempts converges;
        # the flags are cached, so this only costs round-trips on the first
        # call per constrained model.
        last_exc: Optional[Exception] = None
        completion = None
        for _ in range(4):
            try:
                completion = await client.chat.completions.create(
                    **self._build_kwargs(messages)
                )
                break
            except Exception as exc:  # noqa: BLE001
                if self._adapt_from_error(exc):
                    last_exc = exc
                    continue
                raise
        if completion is None:
            # Exhausted adaptation attempts without a successful call.
            raise last_exc if last_exc is not None else RuntimeError(
                "vision request failed without a specific provider error"
            )
        text = completion.choices[0].message.content or ""
        tokens = None
        usage = getattr(completion, "usage", None)
        if usage is not None:
            tokens = getattr(usage, "total_tokens", None)
        return ProviderResponse(
            text=text,
            tokens_used=tokens,
            model=self.model,
            provider=self.name,
        )
