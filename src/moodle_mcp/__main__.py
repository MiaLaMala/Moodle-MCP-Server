"""Entry point for ``python -m moodle_mcp`` / ``uv run moodle-mcp``.

Loads configuration from the environment (.env is picked up automatically)
and starts the FastMCP server over stdio. Fails fast with the exact
spec-mandated message when ``MOODLE_URL`` is missing.
"""

from __future__ import annotations

import logging
import sys

from .config import ConfigError, MoodleConfig
from .server import create_server


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        config = MoodleConfig.load()
    except ConfigError as err:
        print(str(err), file=sys.stderr)
        sys.exit(2)

    server = create_server(config)
    server.run()


if __name__ == "__main__":
    main()
