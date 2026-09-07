"""Capture timeout recovery must not leave a hung child alive."""

from __future__ import annotations

import subprocess
import sys

import pytest

from capture_smoke import _communicate


def test_timed_out_capture_is_killed_and_reaped() -> None:
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "print('ready', flush=True); time.sleep(60)"
            ),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "ready"
        with pytest.raises(subprocess.TimeoutExpired):
            _communicate(process, timeout=0.05)
        assert process.poll() is not None
        assert process.returncode < 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
