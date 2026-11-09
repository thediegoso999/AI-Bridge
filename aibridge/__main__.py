"""Command-line entry point for AI-Bridge."""

import argparse
import json
from dataclasses import asdict

from aibridge.inspection import inspect_repository


def main() -> None:
    parser = argparse.ArgumentParser(prog="aibridge")
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect_parser = subparsers.add_parser(
        "inspect", help="Summarize a local project repository"
    )
    inspect_parser.add_argument("path", nargs="?", default=".")
    arguments = parser.parse_args()

    try:
        snapshot = inspect_repository(arguments.path)
    except ValueError as error:
        parser.error(str(error))

    print(json.dumps(asdict(snapshot), indent=2))


if __name__ == "__main__":
    main()