"""Contract checks for the hosted reproducible archive build."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[4]
HOSTED_RUNNER = ROOT / "packaging/desktop/archive/run_hosted.sh"
CONTAINER_RUNNER = ROOT / "packaging/desktop/archive/build_in_container.sh"
CONTAINERFILE = ROOT / "packaging/desktop/archive/Containerfile.build"
RPM_NEVRAS = ROOT / "packaging/desktop/archive/builder-rpms.nevra"
RPM_HASHES = ROOT / "packaging/desktop/archive/builder-rpms.sha256"


def test_hosted_scripts_are_valid_bash() -> None:
    for script in (HOSTED_RUNNER, CONTAINER_RUNNER):
        completed = subprocess.run(
            ["bash", "-n", str(script)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr


def test_hosted_runner_builds_two_clean_roots_with_pinned_inputs() -> None:
    runner = HOSTED_RUNNER.read_text(encoding="utf-8")

    assert 'actual_head=$(git -C "$source_root" rev-parse HEAD)' in runner
    assert 'if [[ "$actual_head" != "$expected_head" ]]' in runner
    assert runner.count('tar --extract --file "$source_archive"') == 2
    assert "for build in a b; do" in runner
    assert 'diff --recursive --brief "$work_root/output-a"' in runner
    assert "574f7d8cd2a82d77812849729a282b86639b050de120d58b138a126d16b48692" in runner
    # The pinned digest is enforced on the download, not only recorded.
    assert (
        """printf '%s  %s\\n' "$electron_sha256" "$electron_archive" """
        "| sha256sum --check --strict"
    ) in runner
    assert "podman build" in runner
    assert "docker" not in runner


def test_builder_enforces_and_retains_fedora_rpm_signatures() -> None:
    containerfile = CONTAINERFILE.read_text(encoding="utf-8")
    runner = HOSTED_RUNNER.read_text(encoding="utf-8")

    assert "rpmkeys --checksig --verbose ./*.rpm" in containerfile
    assert "--setopt=localpkg_gpgcheck=True ./*.rpm" in containerfile
    assert "rpm-nevra.txt" in containerfile
    assert "rpm-signatures.txt" in runner
    assert "rpm-qa.txt" in runner


def _builder_rpm_lists() -> tuple[list[str], list[tuple[str, str]]]:
    nevras = RPM_NEVRAS.read_text(encoding="utf-8").splitlines()
    hashes = [
        tuple(line.split("  ", 1))
        for line in RPM_HASHES.read_text(encoding="utf-8").splitlines()
    ]
    return nevras, hashes


def test_builder_rpm_closure_is_pinned_by_nevra_and_sha256() -> None:
    nevras, hashes = _builder_rpm_lists()

    assert nevras == sorted(nevras)
    assert len(nevras) == len(set(nevras)) == len(hashes)
    expected_files = set()
    for nevra in nevras:
        name, epoch_version_release, arch = nevra.split(" ")
        epoch, version_release = epoch_version_release.split(":", 1)
        assert epoch.isdigit()
        expected_files.add(f"{name}-{version_release}.{arch}.rpm")
    for digest, filename in hashes:
        assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert {filename for _, filename in hashes} == expected_files
    # The Node runtime and its native libraries come from the pinned closure.
    for name in ("nodejs22-libs", "libuv", "expat", "python3.12-libs"):
        assert any(nevra.startswith(f"{name} ") for nevra in nevras)


def test_builder_installs_only_the_committed_closure_offline() -> None:
    containerfile = CONTAINERFILE.read_text(encoding="utf-8")
    fetch, install = containerfile.split("\nFROM ", 1)
    base = fetch.splitlines()[0].removeprefix("FROM ").removesuffix(" AS fetch")

    assert "@sha256:" in base
    assert install.startswith(base + "\n")
    assert "dnf download --destdir=/tmp/builder-rpms" in fetch
    assert "sha256sum --check --strict /tmp/builder-rpms.sha256" in fetch
    assert "--resolve" not in containerfile
    assert "dnf download" not in install
    assert "COPY --from=fetch /tmp/builder-rpms /tmp/builder-rpms" in install
    assert "dnf install --assumeyes --disablerepo='*'" in install
    assert install.count("dnf install") == 1
    assert "sha256sum --check --strict /tmp/builder-rpms.sha256" in install
    assert '"$(cat /tmp/builder-rpms.nevra)"' in install
    assert "comm -23 /tmp/builder-rpms.nevra" in install
