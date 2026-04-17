"""Gemini vision provider via the OpenAI-compat endpoint.

Google exposes an OpenAI-compatible surface at
  https://generativelanguage.googleapis.com/v1beta/openai

so we reuse the same openai SDK we use elsewhere. Gemini-specific tuning
goes through `extra_body` — the compat layer forwards those keys to the
native generation config:

  - thinking_budget=0 is REQUIRED for accurate object detection per the
    Gemini spatial-understanding docs. Even on flash-grade models, leaving
    thinking enabled produces noticeably looser bounding boxes (the model
    drifts during reasoning).
  - reasoning_effort="none" is the OpenAI-shaped equivalent some Gemini
    versions accept; we pass both for compatibility across model
    revisions.
"""

from __future__ import annotations

from typing import Optional

from .base import ProviderResponse
from .openai_provider import OpenAIVisionProvider


GEMINI_OPENAI_COMPAT_BASE_URL = (
    "https://generativelanguage.googleapis.com/v1beta/openai"
)


class GeminiVisionProvider(OpenAIVisionProvider):
    name = "gemini"

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
            base_url=base_url or GEMINI_OPENAI_COMPAT_BASE_URL,
            max_tokens=max_tokens,
            timeout_s=timeout_s,
        )

    async def chat_with_image(
        self,
        *,
        screenshot_b64: str,
        system_prompt: str,
        user_prompt: str,
        mime_type: str = "image/jpeg",
    ) -> ProviderResponse:
        data_url = f"data:{mime_type};base64,{screenshot_b64}"
        # Gemini's OpenAI-compat endpoint forwards `extra_body` keys into
        # the native generation_config. Disable thinking for spatial
        # tasks — measurable accuracy boost on box_2d outputs per the
        # Gemini object-detection docs. Note: do NOT also pass
        # `reasoning_effort`; the compat endpoint rejects with 400
        # "Expected one of either reasoning_effort or custom
        # thinking_config; found both."
        extra_body = {
            "extra_body": {
                "google": {
                    "thinking_config": {"thinking_budget": 0},
                }
            },
        }
        completion = await self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                },
            ],
            max_tokens=self.max_tokens,
            temperature=0.1,
            response_format={"type": "json_object"},
            extra_body=extra_body,
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
