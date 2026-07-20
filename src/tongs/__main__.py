"""Entry point for python -m tongs."""

import argparse
import sys


def main() -> int:
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
    args = parser.parse_args()

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
