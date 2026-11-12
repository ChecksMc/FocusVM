"""Command-line entry point for FocusVM."""

import argparse
import sys
from collections.abc import Sequence
from typing import Literal

from . import __version__
from .config import ConfigError, ConfigPaths, ConfigStore, Settings

EXIT_INVALID_INPUT = 2
EXIT_OPERATION_FAILED = 4


class _Arguments(argparse.Namespace):
    command: Literal["init", "config"]
    storage_pool: str


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="focusvm", description="Set up the FocusVM development environment."
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )

    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Create the initial configuration")
    init.add_argument(
        "--storage-pool",
        required=True,
        metavar="NAME",
        help="Name of the libvirt storage pool to use",
    )
    config = commands.add_parser("config", help="Inspect the configuration")
    config_commands = config.add_subparsers(required=True)
    config_commands.add_parser("validate", help="Check the current configuration")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run a FocusVM command and return its exit status."""
    args = _parser().parse_args(argv, namespace=_Arguments())

    try:
        path = ConfigPaths.from_environment().config_file
        store = ConfigStore(path)
        if args.command == "init":
            if store.initialize(Settings.for_pool(args.storage_pool)):
                print(f"Created configuration at {path}")
            else:
                print(f"Configuration already initialized at {path}")
        else:
            try:
                store.load()
            except FileNotFoundError as error:
                raise ConfigError(
                    f"No configuration at {path}. Run focusvm init --storage-pool NAME."
                ) from error
            print(f"Configuration is valid at {path}")
    except ConfigError as error:
        print(f"focusvm: {error}", file=sys.stderr)
        return EXIT_INVALID_INPUT
    except OSError as error:
        print(f"focusvm: {error}", file=sys.stderr)
        return EXIT_OPERATION_FAILED

    return 0
