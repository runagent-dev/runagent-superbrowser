"""OpenAI vision provider.

Uses the official `openai` async client against the default endpoint.
Requests JSON-mode output so parsing is straightforward. This class is
also the foundation for OpenRouter + Gemini providers, which subclass it
and override the base URL.
"""

from __future__ import annotations

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

    async def chat_with_image(
        self,
        *,
        screenshot_b64: str,
        system_prompt: str,
        user_prompt: str,
        mime_type: str = "image/jpeg",
    ) -> ProviderResponse:
        data_url = f"data:{mime_type};base64,{screenshot_b64}"
        completion = await self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_prompt},
                        # `image_url` is the universal multimodal-content
                        # shape — OpenAI, OpenRouter, and Gemini's compat
                        # endpoint all accept it.
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                },
            ],
            max_tokens=self.max_tokens,
            temperature=0.1,
            response_format={"type": "json_object"},
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
