"""Command-line entry point: `python -m edgar_mcp` or `edgar-mcp`.

edgar-mcp                                    # stdio, for desktop clients
edgar-mcp --transport streamable-http --port 8000
edgar-mcp --snapshot bundled                 # offline, no SEC requests
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from edgar_mcp import __version__
from edgar_mcp.config import BUNDLED, Settings, Toolset, bundled_snapshot
from edgar_mcp.server import build_server


def main(argv: Sequence[str] | None = None) -> int:
    """Runs the MCP server.

    Args:
      argv: Arguments, without the program name. Defaults to `sys.argv`.

    Returns:
      Process exit code.
    """
    parser = argparse.ArgumentParser(
        prog="edgar-mcp", description="MCP server for SEC EDGAR."
    )
    parser.add_argument(
        "--version", action="version", version=f"edgar-mcp {__version__}"
    )
    parser.add_argument(
        "--transport", choices=["stdio", "streamable-http"], default="stdio"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--snapshot",
        help=f"Serve a snapshot instead of the SEC: a directory, or {BUNDLED}.",
    )
    parser.add_argument(
        "--toolset", choices=[t.value for t in Toolset], help="Tool surface."
    )
    args = parser.parse_args(argv)

    try:
        settings = Settings.from_env()
    except ValueError as error:
        print(f"edgar-mcp: {error}", file=sys.stderr)
        return 2
    if args.snapshot:
        path = (
            bundled_snapshot()
            if args.snapshot == BUNDLED
            else Path(args.snapshot)
        )
        settings = dataclasses.replace(settings, snapshot_dir=path)
    if args.toolset:
        settings = dataclasses.replace(settings, toolset=Toolset(args.toolset))

    # One INFO line per HTTP request drowns out the server's own logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    server = build_server(settings)
    if args.transport == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http", host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
