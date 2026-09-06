"""Start the chat gateway (WhatsApp/Telegram/Discord + web console) from Python.

Usually you'd just run the CLI:

    superbrowser-config init          # brain key + Gemini VISION key
    superbrowser-gateway setup        # enable channels
    superbrowser-gateway login whatsapp   # scan the QR (WhatsApp only)
    superbrowser-gateway             # run it; console at http://127.0.0.1:8460

This example does the equivalent programmatically — useful for embedding the
gateway in a larger service. Requires the gateway extra:

    pip install "runagent-superbrowser[gateway]"

and a running engine (`npm run dev` or the Docker container) at :3100.
"""

from superbrowser_gateway.app import main_async_entry
from superbrowser_gateway.config import load_settings


def main() -> None:
    settings = load_settings()
    settings.enabled = True
    # Point at your engine if it isn't on the default localhost:3100.
    # settings.engine_url = "http://127.0.0.1:3100"

    print(f"Console:  http://{settings.bind}:{settings.port}")
    print(f"Engine:   {settings.engine_url}")
    print("Channels are read from ~/.superbrowser/config.json (gateway.channels).")
    print("Ctrl-C to stop.\n")

    # Blocks, running the orchestrator on the message bus + the HTTP surface.
    main_async_entry(settings)


if __name__ == "__main__":
    main()
