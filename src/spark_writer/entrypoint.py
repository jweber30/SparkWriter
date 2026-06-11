"""Console-script dispatcher for SparkWriter."""

from __future__ import annotations

import sys


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "write":
        from . import cli

        return cli.main(sys.argv[1:])

    from .app import main as app_main

    return app_main()
