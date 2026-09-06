"""superbrowser_config — the unified product config for SuperBrowser.

Canonical file: ``~/.superbrowser/config.json`` (override: ``SUPERBROWSER_CONFIG``).
It is projected into **unset** environment variables (never read directly by
feature code), so precedence is always:

    process env > .env > config.json explicit > profile preset > code defaults

Entry points:
- ``apply_to_env()`` — the projection hook (called by the SDK/bridge right
  after dotenv; also by the TS engine's twin in ``src/config/apply.ts``)
- ``superbrowser-config`` CLI — init wizard, get/set, validate, import-env,
  show --effective (with per-key provenance)
"""

from .detect import chrome_candidates, detect_profile, detection_signals, find_chrome
from .loader import (
    CONFIG_PATH_ENV,
    CONFIG_VERSION,
    DEFAULT_CONFIG_PATH,
    GUARD_ENV,
    PROFILE_ENV,
    apply_to_env,
    config_path,
    deep_merge,
    effective,
    load,
    merged_config,
    resolve_profile,
)
from .presets import PRESETS, preset_for
from .redact import is_secret_path, mask, redact_config
from .schema import SuperBrowserConfig, validate_config
from .writer import write_config

__all__ = [
    "CONFIG_PATH_ENV",
    "CONFIG_VERSION",
    "DEFAULT_CONFIG_PATH",
    "GUARD_ENV",
    "PRESETS",
    "PROFILE_ENV",
    "SuperBrowserConfig",
    "apply_to_env",
    "chrome_candidates",
    "config_path",
    "deep_merge",
    "detect_profile",
    "detection_signals",
    "effective",
    "find_chrome",
    "is_secret_path",
    "load",
    "mask",
    "merged_config",
    "preset_for",
    "redact_config",
    "resolve_profile",
    "validate_config",
    "write_config",
]
