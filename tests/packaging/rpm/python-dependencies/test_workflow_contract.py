from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).parents[4]
PACKAGING = ROOT / "packaging" / "rpm" / "python-dependencies"

#: The only containers that may reach the network: the provider audit and the
#: source download before the build, and the clean install from Fedora.
NETWORKED_PROGRAMS = {
    "audit_providers.py",
    "prepare_sources.py",
    "install_and_verify.sh",
}


def _podman_runs(script: str) -> list[list[str]]:
    """Return each ``podman run`` command, continuation lines joined, as words."""

    joined = script.replace("\\\n", " ")
    return [line.split() for line in joined.splitlines() if "podman run" in line]


def _program(run: list[str]) -> str:
    return next(
        Path(word).name
        for word in run
        if word.startswith("/checkout/") and word.endswith((".py", ".sh"))
    )


def test_every_companion_has_one_reviewable_spec() -> None:
    manifest = json.loads((PACKAGING / "manifest.json").read_text())
    expected = {f"python-{name}.spec" for name in manifest["build_order"]}
    actual = {path.name for path in (PACKAGING / "specs").glob("*.spec")}

    assert actual == expected


def test_hosted_containers_build_offline_without_new_privileges() -> None:
    harness = (PACKAGING / "run_hosted.sh").read_text()
    rebuilder = (PACKAGING / "rebuild_srpms.sh").read_text()
    installer = (PACKAGING / "install_and_verify.sh").read_text()

    assert "RUNNER_ENVIRONMENT:-} == github-hosted" in harness
    runs = _podman_runs(harness)
    assert runs
    for run in runs:
        assert "--security-opt=no-new-privileges" in run, _program(run)
    networked = {_program(run) for run in runs if "--network=none" not in run}
    assert networked == NETWORKED_PROGRAMS
    # Every source RPM rebuild runs without a network.
    rebuilds = _podman_runs(rebuilder)
    assert rebuilds
    for run in rebuilds:
        assert "--network=none" in run, " ".join(run)
    # The clean install takes packages from RPMs only, never from PyPI.
    assert "pip install" not in installer
