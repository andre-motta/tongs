"""Hold every CI file to one credential, permission and timeout contract.

Each workflow under ``.github/workflows`` is loaded and checked for rules
that apply to every file, not only to the release workflows:

* every file declares ``permissions`` explicitly, at the top level or on
  every job, so no job silently inherits the repository default token scope;
* every ``actions/checkout`` step sets ``persist-credentials: false``, so no
  later step (a build backend, an npm install script) can read the job token
  from ``.git/config``;
* every job that runs steps sets ``timeout-minutes``, so a hung job cannot
  hold a runner, or a token, for the six-hour default;
* the manual packaging workflows run only on ``workflow_dispatch`` with
  read-only tokens and check out the exact candidate commit.

``test_release_lock.py`` holds every install under a write token to the
hash-locked release lock.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.ci.verify_desktop_production_gate import ROOT

WORKFLOW_DIRECTORY = ROOT / ".github/workflows"

#: The dispatch-only packaging workflows and the exact read scopes each holds.
MANUAL_PACKAGING_WORKFLOWS = {
    "desktop-archive.yml": {"contents": "read"},
    "desktop-rpm.yml": {"actions": "read", "contents": "read"},
    "desktop-python-rpms.yml": {"contents": "read"},
}


def _workflow_paths() -> list[Path]:
    return sorted(
        path
        for pattern in ("*.yml", "*.yaml")
        for path in WORKFLOW_DIRECTORY.glob(pattern)
    )


@pytest.fixture(scope="module")
def workflows() -> dict[str, dict[str, Any]]:
    return {path.name: yaml.safe_load(path.read_text()) for path in _workflow_paths()}


def _jobs(
    workflows: dict[str, dict[str, Any]],
) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (name, job_id, job)
        for name, document in workflows.items()
        for job_id, job in document["jobs"].items()
    ]


def test_every_checkout_drops_its_credentials(
    workflows: dict[str, dict[str, Any]],
) -> None:
    checkouts = 0
    for name, job_id, job in _jobs(workflows):
        for index, step in enumerate(job.get("steps", [])):
            if not str(step.get("uses", "")).startswith("actions/checkout@"):
                continue
            checkouts += 1
            persist = (step.get("with") or {}).get("persist-credentials")
            assert persist is False, (
                f"{name}:{job_id}:{index} keeps the token in .git/config"
            )
    assert checkouts


def test_no_workflow_runs_on_pull_request_target(
    workflows: dict[str, dict[str, Any]],
) -> None:
    # pull_request_target runs fork code with the base repository's secrets.
    # PyYAML reads the bare `on` key as True.
    for name, document in workflows.items():
        triggers = document.get(True) or document.get("on") or {}
        if isinstance(triggers, str):
            triggers = {triggers: None}
        elif isinstance(triggers, list):
            triggers = dict.fromkeys(triggers)
        assert "pull_request_target" not in triggers, (
            f"{name} runs on pull_request_target"
        )


def test_every_file_declares_its_permissions(
    workflows: dict[str, dict[str, Any]],
) -> None:
    for name, document in workflows.items():
        if "permissions" in document:
            continue
        undeclared = [
            job_id
            for job_id, job in document["jobs"].items()
            if "permissions" not in job
        ]
        assert not undeclared, f"{name} leaves permissions implicit for {undeclared}"


def test_every_job_has_a_timeout(workflows: dict[str, dict[str, Any]]) -> None:
    for name, job_id, job in _jobs(workflows):
        label = f"{name}:{job_id}"
        if "uses" in job:
            # GitHub refuses timeout-minutes on a job that calls a reusable
            # workflow.  The called file's own jobs carry the timeouts, and
            # this scan covers them because the called file is local.
            called = str(job["uses"])
            assert called.startswith("./.github/workflows/"), label
            assert Path(called).name in workflows, label
            assert "timeout-minutes" not in job, label
            continue
        timeout = job.get("timeout-minutes")
        assert isinstance(timeout, int) and not isinstance(timeout, bool), label
        assert 0 < timeout <= 180, f"{label} timeout {timeout}"


@pytest.mark.parametrize("name", sorted(MANUAL_PACKAGING_WORKFLOWS))
def test_manual_packaging_workflows_are_dispatch_only_and_read_only(
    workflows: dict[str, dict[str, Any]], name: str
) -> None:
    document = workflows[name]
    # PyYAML reads the bare ``on`` key as the boolean True.
    triggers = document.get("on", document.get(True))
    assert set(triggers) == {"workflow_dispatch"}, name
    assert document["permissions"] == MANUAL_PACKAGING_WORKFLOWS[name]
    checkouts = 0
    for job_id, job in document["jobs"].items():
        assert "permissions" not in job, f"{name}:{job_id} widens its token"
        for step in job["steps"]:
            if str(step.get("uses", "")).startswith("actions/checkout@"):
                checkouts += 1
                assert step["with"]["ref"] == "${{ env.TONGS_HEAD_SHA }}", name
    assert checkouts, name
