"""Controlled graphical-editor stand-in for native workspace utility proof."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path


def main() -> int:
    evidence_path = Path(os.environ["TONGS_EDITOR_EVIDENCE_PATH"])
    export_path = Path(sys.argv[-1])
    metadata = export_path.stat()
    document = {
        "argv": sys.argv[1:-1],
        "export_name": export_path.name,
        "export_parent": export_path.parent.name,
        "file_mode": stat.S_IMODE(metadata.st_mode),
        "content": export_path.read_text(encoding="utf-8"),
    }
    descriptor = os.open(
        evidence_path,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.write(descriptor, f"{json.dumps(document, sort_keys=True)}\n".encode())
    finally:
        os.close(descriptor)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
