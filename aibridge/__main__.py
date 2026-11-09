"""Command-line entry point for AI-Bridge."""

import argparse
import json
import os
from dataclasses import asdict

from aibridge.dashboard import create_server
from aibridge.inspection import inspect_repository


def main() -> None:
    parser = argparse.ArgumentParser(prog="aibridge")
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect_parser = subparsers.add_parser(
        "inspect", help="Summarize a local project repository"
    )
    inspect_parser.add_argument("path", nargs="?", default=".")
    dashboard_parser = subparsers.add_parser(
        "dashboard", help="Start the private read-only Control Center"
    )
    dashboard_parser.add_argument("--root", default=".")
    dashboard_parser.add_argument("--port", type=int, default=8000)
    arguments = parser.parse_args()

    if arguments.command == "dashboard":
        if not 1 <= arguments.port <= 65535:
            parser.error("--port debe estar entre 1 y 65535.")
        host = "0.0.0.0" if os.environ.get("CODESPACES", "").lower() == "true" else "127.0.0.1"
        try:
            server = create_server(arguments.root, host=host, port=arguments.port)
        except ValueError as error:
            parser.error(str(error))
        print(f"AI-Bridge Control Center escuchando en el puerto {arguments.port}.")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nAI-Bridge Control Center detenido.")
        finally:
            server.server_close()
        return

    try:
        snapshot = inspect_repository(arguments.path)
    except ValueError as error:
        parser.error(str(error))

    print(json.dumps(asdict(snapshot), indent=2))


if __name__ == "__main__":
    main()