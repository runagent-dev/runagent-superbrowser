"""Human-assist framework — typed "a human must act" requests.

Generalizes the captcha handoff into one family: captcha, login, otp,
approval, free-text. Requests are recorded in a ledger under
``~/.superbrowser/human-assist/``, notified to the channels gateway through
the same ``HANDOFF_WEBHOOK_URL`` the TS captcha ladder already uses (payload
v2 = today's payload + additive fields), and resolved by detectors polling
the engine — so the agent resumes the moment the human is done.
"""

from .emitter import notify_gateway, print_assist_banner
from .models import AssistState, AssistType, HumanAssistRequest
from .store import AssistStore, default_store

__all__ = [
    "AssistState",
    "AssistType",
    "AssistStore",
    "HumanAssistRequest",
    "default_store",
    "notify_gateway",
    "print_assist_banner",
]
