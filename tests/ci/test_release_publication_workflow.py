"""Pin the shape of the desktop release publication path in release-desktop.yml.

The installer trusts exactly one workflow path at exactly one ref shape:
``.github/workflows/release-desktop.yml`` at ``refs/tags/vX.Y.Z`` from a
``push`` trigger.  These cases hold the workflow to that contract from the
other side: the tag filter admits only stable tags, the signing job runs on
those tags, the publish job is the only job holding ``contents: write`` and
runs only for a tag push, every step that can create anything is preceded by
the installer-path verification, and a dry run can never reach publication.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.ci.verify_desktop_production_gate import ROOT
from tongs.desktop.installer.metadata import OFFICIAL_WORKFLOW_PATH

WORKFLOW = ROOT / OFFICIAL_WORKFLOW_PATH
PUBLISH_WORKFLOW = ROOT / ".github/workflows/publish.yml"
RELEASE_TAG_FILTER = "v[0-9]+.[0-9]+.[0-9]+"
PUBLICATION_PROGRAM = "tests/integration/desktop/release_publication.py"
PAYLOAD_CONTRACT_PROGRAM = "tests/integration/desktop/rpm_payload_contract.py"


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW.read_text())


@pytest.fixture(scope="module")
def jobs(workflow: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return workflow["jobs"]


def _run_lines(job: dict[str, Any]) -> list[str]:
    return [" ".join((step.get("run") or "").split()) for step in job["steps"]]


def _step_index(job: dict[str, Any], marker: str) -> int:
    for index, line in enumerate(_run_lines(job)):
        if marker in line:
            return index
    raise AssertionError(f"no step runs {marker!r}")


def test_the_tag_filter_admits_only_stable_version_tags(
    workflow: dict[str, Any],
) -> None:
    triggers = workflow[True]
    assert triggers["push"]["tags"] == [RELEASE_TAG_FILTER]
    # The same filter pattern must gate the PyPI publication, or one tag would
    # publish the core without the desktop or the other way round.
    publish = yaml.safe_load(PUBLISH_WORKFLOW.read_text())
    assert RELEASE_TAG_FILTER in publish[True]["push"]["tags"] or publish[True]["push"][
        "tags"
    ] == ["v*"], publish[True]["push"]["tags"]
    dispatch = triggers["workflow_dispatch"]["inputs"]["dry_run"]
    assert dispatch["type"] == "boolean"
    assert dispatch["default"] is True


def test_the_workflow_level_permissions_are_read_only(
    workflow: dict[str, Any],
) -> None:
    assert workflow["permissions"] == {"contents": "read"}


def test_only_the_publish_job_can_write_contents(
    jobs: dict[str, dict[str, Any]],
) -> None:
    writers = {
        name
        for name, job in jobs.items()
        if (job.get("permissions") or {}).get("contents") == "write"
    }
    assert writers == {"release-publish"}
    assert jobs["release-publish"]["permissions"] == {"contents": "write"}
    # The signing job mints the OIDC token; it must not also be able to write.
    assert jobs["candidate-attestation"]["permissions"] == {
        "contents": "read",
        "id-token": "write",
        "attestations": "write",
    }


def test_the_signing_and_publish_jobs_run_for_a_tag_push_only_as_expected(
    jobs: dict[str, dict[str, Any]],
) -> None:
    for name in ("candidate-archive", "candidate-attestation"):
        condition = " ".join(jobs[name]["if"].split())
        assert "startsWith(github.ref, 'refs/tags/v')" in condition
        assert "github.event_name == 'push'" in condition
        assert "inputs.dry_run == true" in condition
    publish = " ".join(jobs["release-publish"]["if"].split())
    assert "github.event_name == 'push'" in publish
    assert "startsWith(github.ref, 'refs/tags/v')" in publish
    assert "workflow_dispatch" not in publish
    assert "dry_run" not in publish
    assert "github.repository == 'andre-motta/tongs'" in publish
    assert "github.repository_id == '1305350434'" in publish
    assert "github.repository_owner_id == '30708955'" in publish
    for upstream in ("candidate-archive", "candidate-attestation", "release-rpm"):
        assert f"needs.{upstream}.result == 'success'" in publish
    assert set(jobs["release-publish"]["needs"]) == {
        "candidate-archive",
        "candidate-attestation",
        "release-rpm",
    }


def test_a_dry_run_rebuilds_rpms_but_never_publishes(
    jobs: dict[str, dict[str, Any]],
) -> None:
    rpm = " ".join(jobs["release-rpm"]["if"].split())
    assert "github.event_name == 'workflow_dispatch' && inputs.dry_run == true" in rpm
    assert "github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')" in rpm
    assert jobs["release-rpm"]["permissions"] == {
        "contents": "read",
        "actions": "read",
    }


def test_the_release_version_is_derived_from_the_ref_and_exported(
    jobs: dict[str, dict[str, Any]],
) -> None:
    archive = jobs["candidate-archive"]
    assert archive["outputs"]["release-version"] == (
        "${{ steps.release.outputs.release_version }}"
    )
    assert archive["outputs"]["release-tag"] == "${{ steps.release.outputs.tag }}"
    assert archive["outputs"]["archive-name"] == (
        "${{ steps.release.outputs.archive_name }}"
    )
    derive = next(step for step in archive["steps"] if step.get("id") == "release")
    assert "refs/tags/v*)" in derive["run"]
    assert re.search(r"release_version=\$\{tag#v\}", derive["run"])
    build = next(
        step for step in archive["steps"] if "run_hosted.sh" in (step.get("run") or "")
    )
    assert build["env"]["TONGS_RELEASE_VERSION"] == (
        "${{ steps.release.outputs.release_version }}"
    )
    # The attested archive subject is the derived name, never a literal.
    attest = next(
        step
        for step in jobs["candidate-attestation"]["steps"]
        if step.get("id") == "attest"
    )
    assert (
        "${{ needs.candidate-archive.outputs.archive-name }}"
        in (attest["with"]["subject-path"])
    )
    assert "0.5.0" not in attest["with"]["subject-path"]


def test_the_rpm_job_binds_the_contract_to_the_release_version(
    jobs: dict[str, dict[str, Any]],
) -> None:
    lines = _run_lines(jobs["release-rpm"])
    contract = next(line for line in lines if PAYLOAD_CONTRACT_PROGRAM in line)
    assert '--release-version "$RELEASE_VERSION"' in contract
    assert '--source-commit "$GITHUB_SHA"' in contract
    assert '--artifact-id "${{ needs.candidate-attestation.outputs.artifact-id }}"' in (
        contract
    )
    producer = next(
        line for line in lines if "packaging/rpm/desktop/run_hosted.sh" in line
    )
    assert '--accepted-dir "$RUNNER_TEMP/tongs-desktop-candidate/archive"' in producer
    assert '--payload-manifest "$RUNNER_TEMP/payload-input-contract.json"' in producer
    checkout = jobs["release-rpm"]["steps"][0]
    assert checkout["with"]["fetch-depth"] == 0, "git describe needs the tag"
    assert checkout["with"]["persist-credentials"] is False


def test_verification_precedes_every_creating_step(
    jobs: dict[str, dict[str, Any]],
) -> None:
    job = jobs["release-publish"]
    assemble = _step_index(job, f"{PUBLICATION_PROGRAM} assemble")
    verify = _step_index(job, f"{PUBLICATION_PROGRAM} verify --tag")
    absent = _step_index(job, f"{PUBLICATION_PROGRAM} require-absent")
    create = _step_index(job, "gh release create")
    draft_check = _step_index(job, "--expect-draft")
    publish = _step_index(job, "gh release edit")
    final_check = _step_index(
        job,
        f'{PUBLICATION_PROGRAM} verify-published --tag "$RELEASE_TAG" --assets-dir "$RELEASE_ASSETS" --report',
    )
    assert assemble < verify < absent < create < draft_check < publish < final_check
    lines = _run_lines(job)
    assert lines[create].count("gh release create") == 1
    assert "--draft" in lines[create]
    assert "--verify-tag" in lines[create]
    assert '--notes-file "docs/releases/$RELEASE_TAG.md"' in lines[create]
    assert "--draft=false" in lines[publish]
    assert "--latest" in lines[publish]
    assert sum("gh release" in line for line in lines) == 2
    # Nothing in the workflow deletes or edits a release outside these steps.
    text = WORKFLOW.read_text()
    assert "gh release delete" not in text
    assert text.count("gh release edit") == 1


def test_the_publish_job_checks_out_without_credentials_and_binds_the_tag(
    jobs: dict[str, dict[str, Any]],
) -> None:
    job = jobs["release-publish"]
    checkout = job["steps"][0]
    assert checkout["with"] == {
        "ref": "${{ github.sha }}",
        "fetch-depth": 1,
        "persist-credentials": False,
    }
    guard = _run_lines(job)[_step_index(job, 'test -s "docs/releases/$RELEASE_TAG.md"')]
    assert 'test "$GITHUB_REF" = "refs/tags/$RELEASE_TAG"' in guard
    assert 'test "$(git rev-parse HEAD)" = "$GITHUB_SHA"' in guard
    assert job["env"]["RELEASE_TAG"] == (
        "${{ needs.candidate-archive.outputs.release-tag }}"
    )


def test_every_release_upload_is_retry_safe_and_retained_for_fourteen_days(
    jobs: dict[str, dict[str, Any]],
) -> None:
    uploads = 0
    for name in ("candidate-attestation", "release-rpm", "release-publish"):
        for step in jobs[name]["steps"]:
            if "upload-artifact" not in str(step.get("uses", "")):
                continue
            uploads += 1
            with_block = step["with"]
            artifact = with_block["name"]
            if artifact.startswith("${{ steps.names.outputs.artifact_name }}"):
                names = next(
                    step for step in jobs[name]["steps"] if step.get("id") == "names"
                )
                artifact = names["run"]
            assert "GITHUB_RUN_ID" in artifact or "github.run_id" in artifact
            assert "GITHUB_RUN_ATTEMPT" in artifact or "github.run_attempt" in artifact
            assert with_block["retention-days"] == 14
            assert with_block["if-no-files-found"] == "error"
    assert uploads == 3


def test_the_installer_and_the_workflow_agree_on_the_trusted_path() -> None:
    assert WORKFLOW == ROOT / ".github/workflows/release-desktop.yml"
    assert Path(OFFICIAL_WORKFLOW_PATH).name == "release-desktop.yml"
