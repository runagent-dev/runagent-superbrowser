"""Hatch build hook: bundle the web console (Vite) into the wheel.

Ports nanobot's webui hook. Builds ``console/`` into
``nanobot/superbrowser_gateway/console_dist`` so ``pip install
"runagent-superbrowser[gateway]"`` serves the UI with zero Node on the user's
machine (the sdist ships the prebuilt dist too).

Behaviour:
- Skips for editable installs (``pip install -e .``) — console devs use
  ``cd console && npm run dev``.
- No-op when ``console/package.json`` is absent (installing from an sdist that
  already contains ``console_dist/``).
- Skips when ``SUPERBROWSER_SKIP_CONSOLE_BUILD=1``.
- Skips when ``console_dist/index.html`` already exists, unless
  ``SUPERBROWSER_FORCE_CONSOLE_BUILD=1``.
- Uses ``bun`` when present, else ``npm`` (``install`` then ``run build``).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class ConsoleBuildHook(BuildHookInterface):
    PLUGIN_NAME = "console-build"

    def initialize(self, version: str, build_data: dict) -> None:
        root = Path(self.root)
        console_dir = root / "console"
        package_json = console_dir / "package.json"
        dist_dir = root / "nanobot" / "superbrowser_gateway" / "console_dist"
        index_html = dist_dir / "index.html"

        if self.target_name == "wheel" and version == "editable":
            self.app.display_info("[console-build] skipped for editable install")
            return
        if os.environ.get("SUPERBROWSER_SKIP_CONSOLE_BUILD") == "1":
            self.app.display_info("[console-build] skipped via SUPERBROWSER_SKIP_CONSOLE_BUILD=1")
            return
        if not package_json.is_file():
            self.app.display_info("[console-build] no console/ source, assuming prebuilt console_dist/")
            return

        force = os.environ.get("SUPERBROWSER_FORCE_CONSOLE_BUILD") == "1"
        if index_html.is_file() and not force:
            self.app.display_info(
                f"[console-build] reusing existing build at {dist_dir} "
                "(SUPERBROWSER_FORCE_CONSOLE_BUILD=1 to rebuild)"
            )
            return

        runner = self._pick_runner()
        if runner is None:
            raise RuntimeError(
                "[console-build] neither `bun` nor `npm` on PATH; install one or set "
                "SUPERBROWSER_SKIP_CONSOLE_BUILD=1 to bypass."
            )
        self.app.display_info(f"[console-build] using {runner} to build the console")
        self._run([runner, "install"], cwd=console_dir)
        self._run([runner, "run", "build"], cwd=console_dir)
        if not index_html.is_file():
            raise RuntimeError(f"[console-build] build finished but {index_html} is missing")
        self.app.display_info(f"[console-build] console ready at {dist_dir}")

    @staticmethod
    def _pick_runner() -> str | None:
        for candidate in ("bun", "npm"):
            if shutil.which(candidate):
                return candidate
        return None

    def _run(self, cmd: list[str], *, cwd: Path) -> None:
        self.app.display_info(f"[console-build] $ {' '.join(cmd)} (cwd={cwd})")
        try:
            subprocess.run(cmd, cwd=cwd, check=True)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"[console-build] command failed ({exc.returncode}): {' '.join(cmd)}") from exc
