"""Sobe o backend do capacete:  helmet/.venv/bin/python helmet/run.py"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import uvicorn  # noqa: E402

from helmet.app import create_app  # noqa: E402
from helmet.settings import get_settings  # noqa: E402


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    settings = get_settings()
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        ws_ping_interval=20,
        ws_ping_timeout=20,
        ws_max_size=1 << 20,
        log_level="info",
    )


if __name__ == "__main__":
    main()
