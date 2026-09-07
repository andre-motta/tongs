"""Stage fixed backend and frontend inputs, then build an installable wheel."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_DESKTOP_ROOT = _HERE.parent


def _hash_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def stage_project(frontend: Path, destination: Path) -> dict[str, str]:
    """Create a self-contained wheel source tree from fixed comparison inputs."""
    frontend = frontend.resolve()
    if not (frontend / "index.html").is_file():
        raise ValueError(f"frontend has no index.html: {frontend}")
    backend = (_DESKTOP_ROOT / "backend" / "__init__.py").resolve()
    if not backend.is_file():
        raise ValueError(f"fixed backend is missing: {backend}")

    shutil.copy2(_HERE / "pyproject.toml", destination / "pyproject.toml")
    shutil.copy2(_HERE / "README.md", destination / "README.md")
    package = destination / "src" / "tongs_desktop_webview"
    shutil.copytree(_HERE / "src" / "tongs_desktop_webview", package)
    shutil.copy2(backend, package / "_fixture_backend.py")
    packaged_frontend = package / "assets" / "frontend"
    shutil.copytree(frontend, packaged_frontend)
    source_icon = _HERE / "packaging" / "io.github.andre_motta.tongs.png"
    frontend_icon = packaged_frontend / "assets" / "tongs-icon.png"
    if (
        frontend_icon.is_file()
        and frontend_icon.read_bytes() != source_icon.read_bytes()
    ):
        raise ValueError("frontend Tongs icon differs from the project icon")
    if not frontend_icon.is_file():
        frontend_icon.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_icon, frontend_icon)

    manifest = {
        "backend_sha256": hashlib.sha256(backend.read_bytes()).hexdigest(),
        "frontend_sha256": _hash_tree(frontend),
        "icon_sha256": hashlib.sha256(source_icon.read_bytes()).hexdigest(),
        "protocol": "prototype-1",
    }
    (package / "_build_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )
    return manifest


def build_wheel(frontend: Path, output: Path) -> tuple[Path, dict[str, str]]:
    """Build a wheel and return its path plus immutable input hashes."""
    output.mkdir(parents=True, exist_ok=True)
    with (
        tempfile.TemporaryDirectory(prefix="tongs-webview-wheel-") as temporary,
        tempfile.TemporaryDirectory(prefix="tongs-webview-output-") as wheel_output,
    ):
        stage = Path(temporary)
        manifest = stage_project(frontend, stage)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "build",
                "--wheel",
                "--no-isolation",
                "--outdir",
                wheel_output,
                str(stage),
            ],
            check=True,
        )
        created = list(Path(wheel_output).glob("*.whl"))
        if len(created) != 1:
            raise RuntimeError(f"expected one new wheel, found {len(created)}")
        wheel = output / created[0].name
        shutil.copy2(created[0], wheel)
    return wheel, manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frontend", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=_HERE / "dist")
    args = parser.parse_args(argv)
    try:
        wheel, manifest = build_wheel(args.frontend, args.output)
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(wheel.resolve())
    print(json.dumps(manifest, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
