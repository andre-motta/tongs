"""Entry point for python -m tongs."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["--install-desktop"]:
        from tongs.desktop.installer.commands import run_desktop_cli

        return run_desktop_cli(["install"], console_argv0=sys.argv[0])
    if arguments and arguments[0] == "desktop":
        from tongs.desktop.installer.commands import run_desktop_cli

        return run_desktop_cli(arguments[1:], console_argv0=sys.argv[0])

    parser = argparse.ArgumentParser(
        prog="tongs",
        description="Terminal code review inbox for GitHub and GitLab",
    )
    parser.add_argument(
        "--scan-root",
        "-d",
        metavar="DIR",
        help="root directory to scan for git repos (default: ~/git)",
    )
    args = parser.parse_args(arguments)

    from tongs.app import TongsApp
    from tongs.config import load_config

    config = load_config()
    if args.scan_root:
        config.scan_root = args.scan_root

    app = TongsApp(config=config)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
