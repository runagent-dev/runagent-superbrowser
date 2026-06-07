"""Dev shim for running the SuperBrowser agent from a source checkout.

The real entry point now lives in ``superbrowser_bridge.cli`` (exposed as the
``superbrowser-agent`` console script once installed). This shim only exists so
the historical workflow still works without installing the package:

    # 1. start the TS engine:  npm start   (or: superbrowser)
    # 2. run a task:
    python nanobot/run.py "Search for the latest AI news and summarize"

The ``sys.path`` insert below makes ``superbrowser_bridge`` / ``vision_agent``
importable as top-level packages when running straight from the tree; it is NOT
needed (and not present) once the package is pip-installed.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from superbrowser_bridge.cli import main  # noqa: E402 - after sys.path setup

if __name__ == "__main__":
    # Use the in-tree workspace for the dev experience.
    main(workspace=str(Path(__file__).parent / "workspace"))
