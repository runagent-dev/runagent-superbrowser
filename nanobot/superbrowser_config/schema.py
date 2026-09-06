"""Pydantic schema + lint for ~/.superbrowser/config.json.

Validation is deliberately lenient (``extra="allow"`` everywhere) so a config
written by a newer release still loads here; lint turns the risky shapes into
warnings/errors instead. The projection loader does not depend on this module
— a schema bug can never break env projection.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic.alias_generators import to_camel

from .detect import PROFILES
from .envmap import AUTO_SENTINEL


class _Base(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="allow")


class EngineConcurrency(_Base):
    max_concurrent: int | None = None
    max_queued: int | None = None
    default_timeout_ms: int | None = None
    max_sessions: int | None = None
    rate_limit_per_min: int | None = None
    task_timeout_ms: int | None = None


class EngineCors(_Base):
    enabled: bool | None = None
    allow_origin: str | None = None


class EngineFirewall(_Base):
    allow: list[str] | None = None
    deny: list[str] | None = None


class EngineConfig(_Base):
    port: int | None = None
    url: str | None = None
    token: str | None = None
    headless: bool | None = None
    download_dir: str | None = None
    chrome_path: str | None = None
    concurrency: EngineConcurrency | None = None
    cors: EngineCors | None = None
    firewall: EngineFirewall | None = None


class BrainConfig(_Base):
    provider: str | None = None
    model: str | None = None
    api_key: str | None = None
    base_url: str | None = None


class VisionConfig(_Base):
    enabled: bool | None = None
    provider: str | None = None
    model: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    cache_size: int | None = None
    max_tokens: int | None = None
    max_bboxes: int | None = None
    timeout_ms: int | None = None
    som_overlay: bool | None = None
    cache_ttl_sec: int | None = None


class CaptchaConfig(_Base):
    provider: str | None = None
    api_key: str | None = None


class AntibotConfig(_Base):
    cookie_jar: bool | None = None
    cookie_jar_path: str | None = None
    captcha_policy: str | None = None
    max_human_handoffs: int | None = None
    proxy_pool: str | None = None
    proxy_pool_residential: str | None = None
    learning_reads: bool | None = None


class T3Config(_Base):
    chrome_path: str | None = None
    chrome_channel: str | None = None
    persist_profile: bool | None = None
    profile_root: str | None = None
    profile_max_mb: int | None = None
    headless: bool | None = None
    auto_xvfb: bool | None = None
    xvfb_display: str | None = None
    viewer_port: int | None = None
    disable_http2: bool | None = None
    cf_wait_s: int | None = None
    cf_solver_wait_s: int | None = None
    ua_profile: str | None = None


class HandoffConfig(_Base):
    webhook_url: str | None = None
    timeout_ms: int | None = None
    ask_timeout_ms: int | None = None
    login_timeout_ms: int | None = None


class IdentitiesConfig(_Base):
    enabled: bool | None = None
    autosave: bool | None = None
    ttl_days: int | None = None
    encryption_key: str | None = None
    policy: str | None = None


class GatewayConfig(_Base):
    enabled: bool | None = None
    bind: str | None = None
    port: int | None = None
    token: str | None = None
    loopback_bypass: bool | None = None
    console: dict[str, Any] | None = None
    channels: dict[str, Any] | None = None
    handoff: dict[str, Any] | None = None


class SuperBrowserConfig(_Base):
    version: int = 1
    profile: str | None = None
    engine: EngineConfig | None = None
    brain: BrainConfig | None = None
    vision: VisionConfig | None = None
    captcha: CaptchaConfig | None = None
    antibot: AntibotConfig | None = None
    t3: T3Config | None = None
    public_host: str | None = None
    handoff: HandoffConfig | None = None
    gateway: GatewayConfig | None = None
    identities: IdentitiesConfig | None = None


def validate_config(raw: dict[str, Any]) -> tuple[SuperBrowserConfig | None, list[str], list[str]]:
    """Returns (model, warnings, errors). ``model`` is None when errors exist."""
    warnings: list[str] = []
    errors: list[str] = []

    try:
        config = SuperBrowserConfig.model_validate(raw)
    except ValidationError as exc:
        for issue in exc.errors():
            location = ".".join(str(part) for part in issue["loc"])
            errors.append(f"{location}: {issue['msg']}")
        return None, warnings, errors

    if "version" not in raw:
        warnings.append('missing "version" — add "version": 1')
    if config.profile is not None and config.profile not in PROFILES:
        errors.append(f'profile must be one of {"/".join(PROFILES)}, got {config.profile!r}')

    if config.profile == "vm" and not config.public_host:
        warnings.append(
            "profile=vm without publicHost — human-handoff links will be unopenable "
            "from other devices (set publicHost to this host's reachable URL)"
        )

    gateway = config.gateway
    if gateway is not None and gateway.bind not in (None, "127.0.0.1", "localhost", "::1") and not gateway.token:
        errors.append(
            f"gateway.bind={gateway.bind!r} without gateway.token — a non-loopback bind requires a token"
        )

    for label, section in (("engine", config.engine), ("t3", config.t3)):
        chrome = getattr(section, "chrome_path", None) if section else None
        if chrome and chrome != AUTO_SENTINEL and not Path(chrome).expanduser().exists():
            warnings.append(f'{label}.chromePath {chrome!r} does not exist on this machine (use "auto"?)')

    return config, warnings, errors
