"""Entry point for python -m tongs."""

import sys


def main() -> int:
    from tongs.app import TongsApp

    app = TongsApp()
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
