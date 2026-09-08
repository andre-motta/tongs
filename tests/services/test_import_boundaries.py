"""Fresh-interpreter regressions for public service and draft imports."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


def _fresh_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(path for path in sys.path if path)
    return environment


@pytest.mark.parametrize(
    "statement",
    [
        (
            "from tongs.state.drafts import DraftStore; "
            "from tongs.services import ApplicationSession, ReviewSubmissionService; "
            "assert DraftStore and ApplicationSession and ReviewSubmissionService"
        ),
        (
            "from tongs.services import ApplicationSession, ReviewSubmissionService; "
            "from tongs.state.drafts import DraftStore; "
            "assert DraftStore and ApplicationSession and ReviewSubmissionService"
        ),
        (
            "from tongs.services import (ApplicationSession, CloseReviewCommand, "
            "MRActionService, ReviewActionTarget); "
            "assert ApplicationSession and CloseReviewCommand and "
            "MRActionService and ReviewActionTarget"
        ),
    ],
)
def test_public_import_orders_work_in_fresh_interpreter(
    tmp_path: Path, statement: str
) -> None:
    completed = subprocess.run(
        [sys.executable, "-P", "-c", statement],
        cwd=tmp_path,
        env=_fresh_environment(),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_actual_sidecar_entrypoint_starts_after_cold_import(tmp_path: Path) -> None:
    completed = subprocess.run(
        [sys.executable, "-P", "-m", "tongs.desktop.sidecar"],
        cwd=tmp_path,
        env=_fresh_environment(),
        input=b"",
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stdout == b""
    assert completed.stderr == b""
