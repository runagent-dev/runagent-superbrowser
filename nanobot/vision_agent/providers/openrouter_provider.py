"""OpenRouter vision provider — same SDK as OpenAI, different base URL.

Model names should be OpenRouter-formatted (e.g. 'openai/gpt-4o-mini',
'google/gemini-flash-1.5'). See https://openrouter.ai/models.
"""

from __future__ import annotations

from typing import Optional

from .openai_provider import OpenAIVisionProvider


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterVisionProvider(OpenAIVisionProvider):
    name = "openrouter"

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
            base_url=base_url or OPENROUTER_BASE_URL,
            max_tokens=max_tokens,
            timeout_s=timeout_s,
        )
