"""Prove the stage receipt binder refuses to publish an unsupported claim.

Every case runs the real issue #110 receipt reader and the real issue #115
report parsers.  The positive case therefore also proves the published receipt
survives independent consumption; the negative cases prove that a plan cannot
assert a stage the staged bytes do not support.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.ci.desktop_stage_receipt import (
    RECEIPTS,
    StageReceiptError,
    admit_source,
    consume_stage_receipt,
    publish_stage_receipt,
)

CHECK_ID = "desktop-production-tap"
REPORT_PATH = "reports/desktop-tap-evidence.json"
RECEIPT_NAME = "desktop-tap-receipt.json"


def _junit(classname: str = "tests.test_example", outcome: str = "") -> str:
    counters = {"failures": 0, "errors": 0, "skipped": 0}
    if "failure" in outcome:
        counters["failures"] = 1
    elif "error" in outcome:
        counters["errors"] = 1
    elif "skipped" in outcome:
        counters["skipped"] = 1
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<testsuites><testsuite name="pytest" tests="1" '
        f'failures="{counters["failures"]}" errors="{counters["errors"]}" '
        f'skipped="{counters["skipped"]}">'
        f'<testcase classname="{classname}" name="test_case" time="0.01">'
        f"{outcome}</testcase></testsuite></testsuites>"
    )


def _tap(status: str = "ok", directive: str = "") -> str:
    name = "production shell case"
    return (
        "\n".join(
            [
                "TAP version 13",
                f"# Subtest: {name}",
                f"{status} 1 - {name}{directive}",
                "  ---",
                "  duration_ms: 0.125",
                "  type: 'test'",
                "  ...",
                "1..1",
                "# tests 1",
                "# suites 0",
                "# pass 1",
                "# fail 0",
                "# cancelled 0",
                "# skipped 0",
                "# todo 0",
                "# duration_ms 1.25",
            ]
        )
        + "\n"
    )


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        check=True,
        text=True,
    )
    return completed.stdout.strip()


@pytest.fixture()
def checkout(tmp_path: Path) -> tuple[Path, str, str]:
    """Create a real clean git checkout so source admission runs for real."""

    root = tmp_path / "checkout"
    root.mkdir()
    _git(root, "init", "--quiet", "--initial-branch=main")
    _git(root, "config", "user.email", "gate@example.invalid")
    _git(root, "config", "user.name", "Gate Fixture")
    (root / "README.md").write_text("fixture\n")
    _git(root, "add", "README.md")
    _git(root, "commit", "--quiet", "-m", "fixture")
    return root, _git(root, "rev-parse", "HEAD"), _git(root, "rev-parse", "HEAD^{tree}")


@pytest.fixture()
def produced(tmp_path: Path) -> dict[str, Path]:
    directory = tmp_path / "producer-output"
    directory.mkdir()
    paths = {
        "tap": directory / "desktop-shell.tap",
        "plugin": directory / "plugin-example.junit.xml",
        "draft": directory / "draft-process.junit.xml",
        "left": directory / "clean-final.json",
        "right": directory / "upgraded-final.json",
    }
    paths["tap"].write_text(_tap())
    paths["plugin"].write_text(_junit("examples.desktop_plugin.tests.test_provider"))
    paths["draft"].write_text(
        _junit("tests.integration.desktop.test_draft_process_acceptance.TestDrafts")
    )
    paths["left"].write_text('{"packages": ["python3-tongs"]}\n')
    paths["right"].write_text('{"packages": ["python3-tongs"]}\n')
    return paths


def _plan(produced: dict[str, Path], **overrides: Any) -> dict[str, Any]:
    plan: dict[str, Any] = {
        "check_id": CHECK_ID,
        "report_path": REPORT_PATH,
        "scope": {
            "covered": "production-desktop-shell-plugin-and-draft-process-reports",
            "excluded": ["hardware-gpu-function", "rpm-package-set"],
        },
        "files": [
            {
                "kind": "report",
                "path": "reports/desktop-shell.tap",
                "source": str(produced["tap"]),
                "format": "node-tap",
            },
            {
                "kind": "report",
                "path": "reports/plugin-example.junit.xml",
                "source": str(produced["plugin"]),
                "format": "pytest-junit",
            },
            {
                "kind": "report",
                "path": "reports/draft-process.junit.xml",
                "source": str(produced["draft"]),
                "format": "pytest-junit",
            },
            {
                "kind": "artifact",
                "path": "artifacts/clean-final.json",
                "source": str(produced["left"]),
                "role": "rpm-clean-final-state",
            },
            {
                "kind": "artifact",
                "path": "artifacts/upgraded-final.json",
                "source": str(produced["right"]),
                "role": "rpm-upgraded-final-state",
            },
        ],
        "stages": [
            {
                "name": "production-shell-tap",
                "evidence": ["reports/desktop-shell.tap"],
                "observation": {"runner": "ubuntu-24.04"},
            },
            {
                "name": "plugin-and-draft-process",
                "evidence": [
                    "reports/plugin-example.junit.xml",
                    "reports/draft-process.junit.xml",
                ],
                "observation": {},
            },
        ],
        "equalities": [
            {
                "left": "artifacts/clean-final.json",
                "right": "artifacts/upgraded-final.json",
                "reason": "a clean final install must equal the upgraded final install",
            }
        ],
        "large_artifacts": [],
    }
    plan.update(overrides)
    return plan


def _policy(commit: str, tree: str, **overrides: Any) -> Any:
    values: dict[str, Any] = {
        "expected_commit": commit,
        "expected_tree": tree,
        "expected_repository": "andre-motta/tongs",
        "expected_run_id": "34274245440",
        "expected_attempt": 1,
        "expected_environment": "github-hosted-ubuntu-24.04",
        "expected_provenance": "hosted",
        "expected_check_id": CHECK_ID,
        "allowed_report_formats": ("artifact-lifecycle-v1", "node-tap", "pytest-junit"),
    }
    values.update(overrides)
    return RECEIPTS.ReceiptPolicy(**values)


def _publish(
    checkout: tuple[Path, str, str],
    plan: dict[str, Any],
    tmp_path: Path,
    *,
    name: str = "evidence",
    policy: Any | None = None,
) -> Any:
    root, commit, tree = checkout
    plan_path = tmp_path / f"{name}-plan.json"
    plan_path.write_text(json.dumps(plan))
    return publish_stage_receipt(
        source_root=root,
        plan_path=plan_path,
        output_root=tmp_path / name,
        receipt_name=RECEIPT_NAME,
        receipt_policy=policy or _policy(commit, tree),
    )


def test_publishes_and_independently_consumes_a_complete_stage(
    checkout: tuple[Path, str, str], produced: dict[str, Path], tmp_path: Path
) -> None:
    binding = _publish(checkout, _plan(produced), tmp_path)
    assert binding.check_id == CHECK_ID
    assert binding.stage_count == 2
    assert binding.report_path == REPORT_PATH

    _, commit, tree = checkout
    evidence = tmp_path / "evidence"
    again = consume_stage_receipt(
        evidence_root=evidence,
        receipt_path=evidence / RECEIPT_NAME,
        receipt_policy=_policy(commit, tree),
        expected_report_path=REPORT_PATH,
    )
    assert again == binding

    report = json.loads((evidence / REPORT_PATH).read_bytes())
    assert report["result"] == "pass"
    assert [stage["name"] for stage in report["stages"]] == [
        "production-shell-tap",
        "plugin-and-draft-process",
    ]
    assert report["equalities"][0]["reason"].startswith("a clean final install")
    assert report["scope"]["excluded"] == ["hardware-gpu-function", "rpm-package-set"]
    assert set(report["report_outcomes"]) == {
        "reports/desktop-shell.tap",
        "reports/plugin-example.junit.xml",
        "reports/draft-process.junit.xml",
    }


@pytest.mark.parametrize(
    ("status", "directive"),
    [("not ok", ""), ("ok", " # SKIP unsupported"), ("ok", " # TODO later")],
)
def test_refuses_to_receipt_a_failing_skipped_or_todo_tap_report(
    checkout: tuple[Path, str, str],
    produced: dict[str, Path],
    tmp_path: Path,
    status: str,
    directive: str,
) -> None:
    produced["tap"].write_text(_tap(status, directive))
    with pytest.raises(StageReceiptError, match="did not pass"):
        _publish(checkout, _plan(produced), tmp_path)
    assert not (tmp_path / "evidence").exists()
    assert not list(tmp_path.glob(".evidence.*"))


@pytest.mark.parametrize(
    "outcome",
    ['<failure message="boom"/>', '<error message="import"/>', "<skipped/>"],
)
def test_refuses_to_receipt_a_failing_or_skipped_python_report(
    checkout: tuple[Path, str, str],
    produced: dict[str, Path],
    tmp_path: Path,
    outcome: str,
) -> None:
    produced["plugin"].write_text(_junit(outcome=outcome))
    with pytest.raises(StageReceiptError, match="did not pass"):
        _publish(checkout, _plan(produced), tmp_path)


def test_refuses_a_declared_equality_that_does_not_hold(
    checkout: tuple[Path, str, str], produced: dict[str, Path], tmp_path: Path
) -> None:
    produced["right"].write_text('{"packages": ["python3-tongs", "extra"]}\n')
    with pytest.raises(StageReceiptError, match="declared equality failed"):
        _publish(checkout, _plan(produced), tmp_path)


def test_refuses_a_stage_without_staged_evidence(
    checkout: tuple[Path, str, str], produced: dict[str, Path], tmp_path: Path
) -> None:
    plan = _plan(produced)
    plan["stages"][0]["evidence"] = []
    with pytest.raises(StageReceiptError, match="names no staged evidence"):
        _publish(checkout, plan, tmp_path)


def test_refuses_a_stage_naming_evidence_that_was_never_staged(
    checkout: tuple[Path, str, str], produced: dict[str, Path], tmp_path: Path
) -> None:
    plan = _plan(produced)
    plan["stages"][0]["evidence"] = ["reports/imaginary.tap"]
    with pytest.raises(StageReceiptError, match="names unstaged evidence"):
        _publish(checkout, plan, tmp_path)


def test_refuses_a_repeated_stage_name(
    checkout: tuple[Path, str, str], produced: dict[str, Path], tmp_path: Path
) -> None:
    plan = _plan(produced)
    plan["stages"][1]["name"] = plan["stages"][0]["name"]
    with pytest.raises(StageReceiptError, match="repeats the stage name"):
        _publish(checkout, plan, tmp_path)


def test_refuses_a_plan_for_another_check(
    checkout: tuple[Path, str, str], produced: dict[str, Path], tmp_path: Path
) -> None:
    plan = _plan(produced)
    plan["check_id"] = "desktop-archive-lifecycle"
    with pytest.raises(StageReceiptError, match="does not match the caller-owned"):
        _publish(checkout, plan, tmp_path)


def test_refuses_a_foreign_lifecycle_report_supplied_by_the_plan(
    checkout: tuple[Path, str, str], produced: dict[str, Path], tmp_path: Path
) -> None:
    plan = _plan(produced)
    plan["files"][0]["format"] = "artifact-lifecycle-v1"
    with pytest.raises(StageReceiptError, match="generated here"):
        _publish(checkout, plan, tmp_path)


def test_refuses_a_plan_that_declares_no_scope_exclusion(
    checkout: tuple[Path, str, str], produced: dict[str, Path], tmp_path: Path
) -> None:
    plan = _plan(produced)
    plan["scope"]["excluded"] = []
    with pytest.raises(StageReceiptError, match="what its scope excludes"):
        _publish(checkout, plan, tmp_path)


@pytest.mark.parametrize(
    "mutation",
    [
        {"stages": []},
        {"files": []},
        {"report_path": "../escape.json"},
        {"report_path": "/absolute.json"},
    ],
)
def test_refuses_structurally_invalid_plans(
    checkout: tuple[Path, str, str],
    produced: dict[str, Path],
    tmp_path: Path,
    mutation: dict[str, Any],
) -> None:
    with pytest.raises(StageReceiptError):
        _publish(checkout, _plan(produced, **mutation), tmp_path)


def test_refuses_an_unknown_plan_key(
    checkout: tuple[Path, str, str], produced: dict[str, Path], tmp_path: Path
) -> None:
    with pytest.raises(StageReceiptError, match="key set mismatch"):
        _publish(checkout, _plan(produced, verdict="green"), tmp_path)


def test_refuses_a_dirty_checkout(
    checkout: tuple[Path, str, str], produced: dict[str, Path], tmp_path: Path
) -> None:
    root, _, _ = checkout
    (root / "README.md").write_text("modified\n")
    with pytest.raises(StageReceiptError, match="git diff"):
        _publish(checkout, _plan(produced), tmp_path)


def test_refuses_a_checkout_at_another_commit(
    checkout: tuple[Path, str, str], produced: dict[str, Path], tmp_path: Path
) -> None:
    root, _, tree = checkout
    with pytest.raises(StageReceiptError, match="HEAD does not match"):
        _publish(
            checkout,
            _plan(produced),
            tmp_path,
            policy=_policy("9" * 40, tree),
        )
    with pytest.raises(StageReceiptError, match="tree does not match"):
        _publish(
            checkout,
            _plan(produced),
            tmp_path,
            policy=_policy(_git(root, "rev-parse", "HEAD"), "8" * 40),
        )


def test_refuses_an_existing_output_root(
    checkout: tuple[Path, str, str], produced: dict[str, Path], tmp_path: Path
) -> None:
    (tmp_path / "evidence").mkdir()
    with pytest.raises(StageReceiptError, match="must not already exist"):
        _publish(checkout, _plan(produced), tmp_path)


def test_refuses_a_policy_that_only_allows_the_lifecycle_format(
    checkout: tuple[Path, str, str], produced: dict[str, Path], tmp_path: Path
) -> None:
    _, commit, tree = checkout
    with pytest.raises(StageReceiptError, match="staged test report formats"):
        _publish(
            checkout,
            _plan(produced),
            tmp_path,
            policy=_policy(
                commit, tree, allowed_report_formats=("artifact-lifecycle-v1",)
            ),
        )


def test_consumer_rejects_a_report_replaced_after_publication(
    checkout: tuple[Path, str, str], produced: dict[str, Path], tmp_path: Path
) -> None:
    _publish(checkout, _plan(produced), tmp_path)
    _, commit, tree = checkout
    evidence = tmp_path / "evidence"
    target = evidence / "reports/desktop-shell.tap"
    target.chmod(0o600)
    target.write_text(_tap("not ok"))
    with pytest.raises(RECEIPTS.ReceiptValidationError):
        consume_stage_receipt(
            evidence_root=evidence,
            receipt_path=evidence / RECEIPT_NAME,
            receipt_policy=_policy(commit, tree),
            expected_report_path=REPORT_PATH,
        )


def test_consumer_rejects_a_stale_run_identity(
    checkout: tuple[Path, str, str], produced: dict[str, Path], tmp_path: Path
) -> None:
    _publish(checkout, _plan(produced), tmp_path)
    _, commit, tree = checkout
    evidence = tmp_path / "evidence"
    with pytest.raises(RECEIPTS.ReceiptValidationError):
        consume_stage_receipt(
            evidence_root=evidence,
            receipt_path=evidence / RECEIPT_NAME,
            receipt_policy=_policy(commit, tree, expected_run_id="34274245441"),
            expected_report_path=REPORT_PATH,
        )


def test_source_admission_accepts_the_real_checkout(
    checkout: tuple[Path, str, str],
) -> None:
    root, commit, tree = checkout
    admit_source(root, commit, tree)
