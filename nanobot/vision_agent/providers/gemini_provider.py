"""Gemini vision provider via the OpenAI-compat endpoint.

Google exposes an OpenAI-compatible surface at
  https://generativelanguage.googleapis.com/v1beta/openai

so we can reuse the same openai SDK we use for OpenAI / OpenRouter.
Model names are native Gemini identifiers (e.g. 'gemini-2.0-flash-exp',
'gemini-1.5-flash'). If you need features the compat endpoint doesn't
expose (Gemini-native grounding, multi-image batching shortcuts), swap
this class for a google-genai backed provider — keep the same interface.
"""

from __future__ import annotations

from typing import Optional

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
