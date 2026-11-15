"""Run Folio: python -m web --library DIR [--host HOST] [--port PORT].

The library folder is fixed when Folio starts (in Docker, the Dockerfile
passes the /library mount); the browser can't choose it.
"""
import argparse
import os

import uvicorn

from web import server
from web.core import normalize_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m web", description="Folio music-score server")
    parser.add_argument("--library", required=True, help="the music library folder")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8989)
    args = parser.parse_args(argv)
    # Absolute, so the library doesn't depend on the working directory.
    args.library = os.path.abspath(normalize_path(args.library))
    if not os.path.isdir(args.library):
        parser.error(f"--library: not a directory: {args.library}")
    return args


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    server.LIBRARY_DIR = args.library
    uvicorn.run(server.app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
