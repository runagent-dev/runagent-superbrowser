"""Single source of truth for where the per-role workspaces live and how
they get their bundled ``SOUL.md`` prompt.

Why this module exists
----------------------
Each nanobot agent's "brain" is the ``SOUL.md`` in its workspace dir
(nanobot's ``agent/context.py`` loads ``BOOTSTRAP_FILES = ["AGENTS.md",
"SOUL.md", "USER.md"]`` from there). The orchestrator / browser / search
workers each have one. Historically those dirs were resolved as
``Path(__file__).parent.parent / "workspace_<role>"`` from three different
call sites — which only works in a source checkout. Once the package is
installed and flattened to the wheel root, that path points at a
non-existent ``site-packages/workspace_<role>`` and every agent silently
falls back to nanobot's empty default prompt.

This module fixes that:

* In a **source checkout** it reuses the existing in-repo
  ``nanobot/workspace_<role>`` dirs verbatim (zero behaviour change).
* When **installed**, it provisions writable workspaces under
  ``~/.superbrowser/workspaces/<role>`` and drops the bundled prompt
  (shipped as package data under ``superbrowser_bridge/_prompts/<role>/``)
  into each one.
* ``$SUPERBROWSER_WORKSPACE_ROOT`` overrides the base in both cases.

Pure Python, no nanobot import — safe to import at module load time. The
module-level ``BROWSER_WORKSPACE`` / ``SEARCH_WORKSPACE`` / ``LEARNINGS_DIR``
constants in the rest of the bridge are computed from ``workspace_for`` /
``learnings_dir`` here, so set ``$SUPERBROWSER_WORKSPACE_ROOT`` *before*
importing the bridge if you need to override it (the SDK does this).
"""

from __future__ import annotations

import contextlib
import os
import shutil
import time
from functools import lru_cache
from importlib import resources as _resources
from pathlib import Path

ROLES: tuple[str, ...] = ("orchestrator", "browser", "search")


def _validate_role(role: str) -> None:
    if role not in ROLES:
        raise ValueError(f"unknown workspace role {role!r}; expected one of {ROLES}")


@lru_cache(maxsize=1)
def _detect_dev_root() -> Path | None:
    """Return the in-repo ``nanobot/`` dir iff running from a source checkout.

    Heuristic: walk up from this file; the first ancestor that contains
    ``workspace_orchestrator/SOUL.md`` is the dev root. Returns ``None`` when
    installed (flattened: no such sibling exists). Cached — the checkout
    layout doesn't change within a process.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "workspace_orchestrator" / "SOUL.md").is_file():
            return parent
    return None


def _base_and_layout() -> tuple[Path, bool]:
    """Return ``(base_dir, is_dev_layout)``.

    ``is_dev_layout`` True means role dirs are named ``workspace_<role>``
    under ``base`` (the in-repo convention). False means ``<role>``.

    Precedence: ``$SUPERBROWSER_WORKSPACE_ROOT`` → dev checkout →
    ``~/.superbrowser/workspaces``. The env override is read live (not
    cached) so it can be set before the bridge is imported.
    """
    env = os.environ.get("SUPERBROWSER_WORKSPACE_ROOT")
    if env:
        return Path(env).expanduser().resolve(), False
    dev = _detect_dev_root()
    if dev is not None:
        return dev, True
    return Path.home() / ".superbrowser" / "workspaces", False


def workspace_root() -> Path:
    """The base dir under which the per-role workspaces live."""
    return _base_and_layout()[0]


def workspace_for(role: str) -> Path:
    """Absolute path to the workspace dir for ``role`` (does not create it)."""
    _validate_role(role)
    base, dev_layout = _base_and_layout()
    return base / (f"workspace_{role}" if dev_layout else role)


def learnings_dir() -> Path:
    """Per-domain routing/captcha learnings dir (under the orchestrator ws)."""
    return workspace_for("orchestrator") / "learnings"


def prompts_dir() -> Path:
    """The bundled ``_prompts`` tree shipped as package data.

    Resolves via ``importlib.resources`` so it works whether installed or
    in-tree. In a source checkout this path may not exist (the ``_prompts``
    copies are produced by the wheel build's force-include); that's fine —
    in dev ``provision`` reads the canonical ``workspace_<role>/SOUL.md``
    that already sit on disk, so no copy is needed.
    """
    try:
        return Path(str(_resources.files("superbrowser_bridge") / "_prompts"))
    except Exception:  # pragma: no cover - defensive
        return Path(__file__).resolve().parent / "_prompts"


@contextlib.contextmanager
def _provision_lock(root: Path, timeout: float = 10.0):
    """Best-effort cross-process lock to serialize first-run provisioning.

    Uses an ``O_CREAT|O_EXCL`` lockfile. If it can't acquire within
    ``timeout`` it proceeds anyway — ``provision`` is idempotent and writes
    atomically, so a contended race is safe, just slightly wasteful.
    """
    lock_path = root / ".provision.lock"
    fd: int | None = None
    start = time.monotonic()
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.monotonic() - start > timeout:
                fd = None
                break
            time.sleep(0.05)
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
            with contextlib.suppress(OSError):
                lock_path.unlink()


def _atomic_copy(src: Path, dst: Path) -> None:
    tmp = dst.with_name(dst.name + f".tmp.{os.getpid()}")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)


def provision(force: bool = False) -> dict[str, Path]:
    """Idempotently create each role workspace and seed its ``SOUL.md``.

    * Creates ``workspace_for(role)`` for every role and ``learnings_dir()``.
    * Copies the bundled ``_prompts/<role>/SOUL.md`` into the workspace when
      it's missing (or when ``force``). Never clobbers a user-edited
      ``SOUL.md`` unless ``force=True``. Writes are atomic.
    * A no-op in a source checkout (the canonical SOUL files already exist).

    Returns ``{role: workspace_path}``.
    """
    out: dict[str, Path] = {}
    root = workspace_root()
    root.mkdir(parents=True, exist_ok=True)
    with _provision_lock(root):
        src_base = prompts_dir()
        for role in ROLES:
            ws = workspace_for(role)
            ws.mkdir(parents=True, exist_ok=True)
            soul = ws / "SOUL.md"
            src = src_base / role / "SOUL.md"
            if (force or not soul.exists()) and src.is_file():
                _atomic_copy(src, soul)
            out[role] = ws
        learnings_dir().mkdir(parents=True, exist_ok=True)
    return out
