"""Contract checks for the hosted reproducible archive build."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[4]
WORKFLOW = ROOT / ".github/workflows/desktop-archive.yml"
HOSTED_RUNNER = ROOT / "packaging/desktop/archive/run_hosted.sh"
CONTAINER_RUNNER = ROOT / "packaging/desktop/archive/build_in_container.sh"


def test_hosted_scripts_are_valid_bash() -> None:
    for script in (HOSTED_RUNNER, CONTAINER_RUNNER):
        completed = subprocess.run(
            ["bash", "-n", str(script)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr


def test_workflow_checks_out_exact_candidate_and_retains_bounded_evidence() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "permissions:\n  contents: read\n" in workflow
    assert "ref: ${{ env.TONGS_HEAD_SHA }}" in workflow
    assert "persist-credentials: false" in workflow
    assert "packaging/desktop/archive/run_hosted.sh" in workflow
    assert "retention-days: 14" in workflow
    assert "release" not in workflow.lower()


def test_hosted_runner_builds_two_clean_roots_with_pinned_inputs() -> None:
    runner = HOSTED_RUNNER.read_text(encoding="utf-8")

    assert 'actual_head=$(git -C "$source_root" rev-parse HEAD)' in runner
    assert 'if [[ "$actual_head" != "$expected_head" ]]' in runner
    assert runner.count('tar --extract --file "$source_archive"') == 2
    assert "for build in a b; do" in runner
    assert 'diff --recursive --brief "$work_root/output-a"' in runner
    assert "574f7d8cd2a82d77812849729a282b86639b050de120d58b138a126d16b48692" in runner
    assert "podman build" in runner
    assert "docker" not in runner
