"""Distribution tests for staged backend and frontend resources."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

from build_wheel import build_wheel


def test_wheel_contains_exact_backend_and_selected_frontend(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    frontend = Path(__file__).parent / "fixtures" / "minimal_frontend"
    output = tmp_path / "wheelhouse"

    wheel, manifest = build_wheel(frontend, output)

    backend = (root / "backend" / "__init__.py").read_bytes()
    assert manifest["backend_sha256"] == hashlib.sha256(backend).hexdigest()
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        packaged_backend = archive.read("tongs_desktop_webview/_fixture_backend.py")
        packaged_manifest = json.loads(
            archive.read("tongs_desktop_webview/_build_manifest.json")
        )
    assert packaged_backend == backend
    assert "tongs_desktop_webview/assets/frontend/index.html" in names
    assert "tongs_desktop_webview/assets/frontend/assets/tongs-icon.png" in names
    assert packaged_manifest == manifest


def test_wheel_can_be_rebuilt_into_same_output(tmp_path: Path) -> None:
    frontend = Path(__file__).parent / "fixtures" / "minimal_frontend"
    output = tmp_path / "wheelhouse"

    first, first_manifest = build_wheel(frontend, output)
    second, second_manifest = build_wheel(frontend, output)

    assert first == second
    assert first_manifest == second_manifest
    assert len(list(output.glob("*.whl"))) == 1


def test_wheel_imports_outside_checkout(tmp_path: Path) -> None:
    frontend = Path(__file__).parent / "fixtures" / "minimal_frontend"
    wheel, _ = build_wheel(frontend, tmp_path / "wheelhouse")
    installed = tmp_path / "installed"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--target",
            str(installed),
            str(wheel),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    probe = (
        "import json,sys;"
        f"sys.path.insert(0,{str(installed)!r});"
        "from tongs_desktop_webview.launcher import _load_runtime,_packaged_frontend;"
        "backend,_=_load_runtime();"
        "ctx=_packaged_frontend();"
        "path=ctx.__enter__();"
        "print(json.dumps({'health': backend().invoke('health'), "
        "'index': (path/'index.html').is_file()}));"
        "ctx.__exit__(None,None,None)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(completed.stdout)
    assert result == {
        "health": {"fixture": True, "protocol": "prototype-1"},
        "index": True,
    }
