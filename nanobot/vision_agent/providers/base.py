"""Abstract vision provider interface.

Each concrete provider wraps whatever SDK / endpoint it needs to send a
single screenshot + system prompt + user prompt to a vision-capable model
and return the raw JSON string. Parsing / validation is the client's job,
not the provider's.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Optional


@dataclass
class ProviderResponse:
    """Raw JSON text returned by the model plus bookkeeping."""

    text: str
    tokens_used: Optional[int]
    model: str
    provider: str


class VisionProvider(abc.ABC):
    """Vision-only, single-screenshot contract.

    Implementations MUST:
      - accept a base64-encoded JPEG (or PNG) screenshot
      - send it with `system_prompt` as system message and `user_prompt`
        as user message
      - request JSON-mode output when the endpoint supports it
      - return the raw response text — the client parses + validates
    """

    name: str = "base"

    def __init__(self, *, model: str, api_key: str, base_url: Optional[str] = None,
                 max_tokens: int = 1500, timeout_s: float = 8.0) -> None:
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.max_tokens = max_tokens
        self.timeout_s = timeout_s

    @abc.abstractmethod
    async def chat_with_image(
        self,
        *,
        screenshot_b64: str,
        system_prompt: str,
        user_prompt: str,
        mime_type: str = "image/jpeg",
        timeout_s: Optional[float] = None,
    ) -> ProviderResponse:
        """Send screenshot + prompts, return raw JSON response text.

        `timeout_s` overrides the provider-default timeout for THIS
        call only — used by the retry path to give a longer deadline
        on the compact-mode second attempt.
        """
        raise NotImplementedError
