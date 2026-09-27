"""Prove the lane classifier selects the right lanes and fails closed.

Layer 1 is :func:`classify_paths` over literal paths, one case per rule
pattern plus the unmatched fallback.  The fail-closed cases drive
:func:`compute_plan` through a scripted git so every doubt is shown to select
the full graph, and a real repository proves rename and merge-parent handling.
The import-closure drift test keeps the desktop lane honest: nothing the
desktop sidecar imports may be classified into a plan that skips it.
"""

from __future__ import annotations

import itertools
import json
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from tests.ci import ci_plan
from tests.ci.ci_plan import (
    ALL_LANES,
    FULL_LABEL,
    LANE_CHECKS,
    LANE_PRODUCTION_JOBS,
    LANES,
    RULES,
    Plan,
    PlanError,
    Rule,
    classify_paths,
    close_lanes,
    compute_plan,
    effective_plan,
    expected_ci_results,
    expected_production_results,
    explain_plan,
    full_plan,
    plan_mismatches,
    render_table,
    selected_checks,
)

ROOT = Path(__file__).resolve().parents[2]
PROGRAM = ROOT / "tests/ci/ci_plan.py"
MERGE = "c" * 40
BASE = "b" * 40
HEAD = "a" * 40
ZERO = "0" * 40
DOCS = frozenset({"docs"})
TUI = frozenset({"lint", "core"})
CORE_READ_DOCS = frozenset({"docs", "core"})
CORE_TESTS = frozenset({"lint", "core"})
README = frozenset({"docs", "core"})
SIDECAR = frozenset({"lint", "core", "desktop_fixtures", "desktop"})
PACKAGING = SIDECAR | {"packaging"}
SPIKES = frozenset({"desktop_fixtures"})
RELEASE_EVIDENCE = frozenset({"lint"})


# Layer 1: every rule pattern, by literal path.

LAYER_ONE: list[tuple[str, frozenset[str] | None]] = [
    # DOCS
    ("docs/index.md", DOCS),
    ("docs/work/release-v1.0.0.md", DOCS),
    ("docs/stylesheets/extra.css", DOCS),
    ("docs/snippet.py", DOCS),
    ("site/package.json", DOCS),
    ("site/src/content/docs", DOCS),
    ("site/public/CNAME", DOCS),
    (".agents/testing/README.md", CORE_READ_DOCS),
    ("CONTRIBUTING.md", DOCS),
    ("AGENTS.md", DOCS),
    ("SECURITY.md", DOCS),
    ("CODE_OF_CONDUCT.md", DOCS),
    (".github/ISSUE_TEMPLATE/bug.yml", DOCS),
    (".github/PULL_REQUEST_TEMPLATE.md", DOCS),
    (".github/PULL_REQUEST_TEMPLATE/feature.md", DOCS),
    (".github/FUNDING.yml", DOCS),
    (".github/linters/.markdownlint-cli2.yaml", CORE_READ_DOCS),
    # README
    ("README.md", README),
    # TUI
    ("src/tongs/views/inbox.py", TUI),
    (".agents/ci/README.md", CORE_READ_DOCS),
    (".agents/testing/README.md", CORE_READ_DOCS),
    (".github/linters/package-lock.json", CORE_READ_DOCS),
    ("src/tongs/widgets/diff_panel.py", TUI),
    ("src/tongs/mcp/server.py", TUI),
    ("src/tongs/app.py", TUI),
    ("src/tongs/commands.py", TUI),
    ("src/tongs/helpers.py", TUI),
    ("src/tongs/__main__.py", TUI),
    # CORE TESTS
    ("tests/test_config.py", CORE_TESTS),
    ("tests/test_tui_session.py", CORE_TESTS),
    ("tests/test_cache/test_store.py", CORE_TESTS),
    ("tests/test_diff/test_parser.py", CORE_TESTS),
    ("tests/test_forges/test_github.py", CORE_TESTS),
    ("tests/test_mcp/test_server.py", CORE_TESTS),
    ("tests/test_plugins/test_registry.py", CORE_TESTS),
    ("tests/test_scanner/test_remote.py", CORE_TESTS),
    ("tests/test_views/test_inbox.py", CORE_TESTS),
    ("tests/test_widgets/test_mr_table.py", CORE_TESTS),
    # SIDECAR
    ("src/tongs/cache/store.py", SIDECAR),
    ("src/tongs/config.py", SIDECAR),
    ("src/tongs/desktop/sidecar.py", SIDECAR),
    ("src/tongs/desktop/protocol/server.py", SIDECAR),
    ("src/tongs/diff/parser.py", SIDECAR),
    ("src/tongs/errors.py", SIDECAR),
    ("src/tongs/forges/github.py", SIDECAR),
    ("src/tongs/plugins/desktop.py", SIDECAR),
    ("src/tongs/scanner/discovery.py", SIDECAR),
    ("src/tongs/services/session.py", SIDECAR),
    ("src/tongs/state/drafts/store.py", SIDECAR),
    ("src/tongs/tui_services.py", SIDECAR),
    ("tests/__init__.py", SIDECAR),
    ("tests/desktop/test_sidecar.py", SIDECAR),
    ("tests/desktop/electron/app.test.mjs", SIDECAR),
    ("tests/fixtures/builder_mr_3113.diff", SIDECAR),
    ("tests/integration/desktop/installed_core_composition.py", SIDECAR),
    ("tests/plugins/conftest.py", SIDECAR),
    ("tests/services/test_session.py", SIDECAR),
    ("tests/state/test_drafts.py", SIDECAR),
    ("examples/desktop-plugin/pyproject.toml", SIDECAR),
    # PACKAGING
    ("LICENSE", PACKAGING),
    ("scripts/build_desktop_archive.py", PACKAGING),
    ("scripts/build_desktop_sbom.py", PACKAGING),
    ("src/tongs/__init__.py", PACKAGING),
    ("src/tongs/desktop/artifact_contract/models.py", PACKAGING),
    ("src/tongs/desktop/installer/metadata.py", PACKAGING),
    ("tests/integration/desktop/archive_evidence.py", PACKAGING),
    ("tests/integration/desktop/candidate_attestation.py", PACKAGING),
    ("tests/integration/desktop/rpm_payload_contract.py", PACKAGING),
    ("tests/integration/desktop/sbom_evidence.py", PACKAGING),
    ("tests/desktop/installer/fixtures/wheel.json", PACKAGING),
    ("tests/packaging/desktop/sbom/schema/spdx-2.3.schema.json", PACKAGING),
    # SPIKES
    ("spikes/desktop/README.md", SPIKES),
    ("spikes/desktop/tests/test_backend.py", SPIKES),
    # RELEASE EVIDENCE
    ("scripts/release-evidence/native/ci-proof.mjs", RELEASE_EVIDENCE),
    # FULL rules
    (".github/workflows/ci.yml", None),
    (".github/workflows/docs.yml", None),
    (".github/scripts/verify_desktop_production.py", None),
    ("tests/ci/ci_plan.py", None),
    ("tests/ci/test_ci_plan.py", None),
    ("tests/containers/fedora-44/Containerfile", None),
    ("pyproject.toml", None),
    ("requirements/installer-verifier.lock", None),
    ("packaging/rpm/tongs.spec", None),
    ("desktop/package.json", None),
    (".gitignore", None),
    # Unmatched, therefore full
    ("src/tongs/new_module.py", None),
    ("scripts/new_tool.py", None),
    ("examples/other-plugin/setup.py", None),
    (".github/CODEOWNERS", None),
    ("tests/conftest.py", None),
    ("tests/test_new/test_x.py", None),
    # Path, never extension: nested markdown and lookalike names stay full
    ("src/tongs/NOTES.md", None),
    ("src/tongs/views.py", None),
    ("src/tongs/app.py.orig", None),
    ("docs", None),
    ("site", None),
    # The retired MkDocs config no longer names a lane
    ("mkdocs.yml", None),
    ("Docs/index.md", None),
    ("readme.md", DOCS),
    ("tests/test_config.txt", None),
]


@pytest.mark.parametrize(("path", "expected"), LAYER_ONE, ids=[p for p, _ in LAYER_ONE])
def test_layer_one_classification(path: str, expected: frozenset[str] | None) -> None:
    lanes, full, reasons = classify_paths([path])
    if expected is None:
        assert full
        assert lanes == ALL_LANES
        assert any(path in reason for reason in reasons)
    else:
        assert not full
        assert lanes == expected
        assert reasons


def test_every_rule_pattern_is_exercised_by_layer_one() -> None:
    paths = [path for path, _ in LAYER_ONE]
    for rule in RULES:
        for pattern in rule.patterns:
            assert any(ci_plan.pattern_matches(pattern, path) for path in paths), (
                rule.name,
                pattern,
            )


@pytest.mark.parametrize(
    "path",
    ["", "/etc/passwd", "docs/../src/tongs/config.py", "docs//index.md", "./docs/a"],
)
def test_invalid_paths_select_the_full_graph(path: str) -> None:
    lanes, full, reasons = classify_paths([path])
    assert full and lanes == ALL_LANES
    assert any("invalid path" in reason for reason in reasons)


def test_an_empty_path_list_selects_the_full_graph() -> None:
    assert classify_paths([]) == (ALL_LANES, True, ("empty diff",))


def test_matching_rules_add_their_lanes_together() -> None:
    lanes, full, _ = classify_paths(["docs/index.md", "src/tongs/views/inbox.py"])
    assert not full and lanes == DOCS | TUI
    lanes, full, _ = classify_paths(["docs/index.md", "README.md"])
    assert not full and lanes == README
    lanes, full, _ = classify_paths(["docs/index.md", "src/tongs/forges/github.py"])
    assert not full and lanes == DOCS | SIDECAR
    lanes, full, _ = classify_paths(["src/tongs/views/x.py", "spikes/desktop/a.py"])
    assert not full and lanes == TUI | SPIKES
    lanes, full, _ = classify_paths(["docs/index.md", "src/tongs/new_module.py"])
    assert full and lanes == ALL_LANES


# One case per path class of the widened table.

PATH_CLASSES: list[tuple[str, frozenset[str] | None]] = [
    ("src/tongs/views/x.py", TUI),
    ("src/tongs/forges/x.py", SIDECAR),
    ("src/tongs/__init__.py", PACKAGING),
    ("pyproject.toml", None),
    ("tests/fixtures/x", SIDECAR),
    ("tests/containers/x", None),
]


@pytest.mark.parametrize(
    ("path", "expected"), PATH_CLASSES, ids=[p for p, _ in PATH_CLASSES]
)
def test_each_path_class(path: str, expected: frozenset[str] | None) -> None:
    lanes, full, _ = classify_paths([path])
    if expected is None:
        assert full and lanes == ALL_LANES
        assert "fedora_podman" in lanes
        return
    assert not full and lanes == expected
    assert "fedora_podman" not in lanes


def test_only_full_paths_select_the_fedora_podman_probe() -> None:
    for rule in RULES:
        assert rule.full or "fedora_podman" not in close_lanes(rule.lanes), rule.name


def test_packaging_is_selected_only_by_the_packaging_rule_or_the_full_graph() -> None:
    selecting = [
        rule.name
        for rule in RULES
        if not rule.full and "packaging" in close_lanes(rule.lanes)
    ]
    assert selecting == ["packaging"]


def test_adding_paths_can_only_add_lanes() -> None:
    samples = [path for path, _ in LAYER_ONE[::3]]
    for size in (1, 2):
        for combination in itertools.combinations(samples, size):
            lanes, full, _ = classify_paths(list(combination))
            for extra in samples:
                more, more_full, _ = classify_paths([*combination, extra])
                assert lanes <= more
                assert more_full or not full


def test_full_reasons_are_capped() -> None:
    paths = [f"unknown/{index}.txt" for index in range(100)]
    _, full, reasons = classify_paths(paths)
    assert full
    assert len(reasons) == ci_plan.MAX_PATH_REASONS + 1
    assert reasons[-1] == "... and 80 more"


def test_close_lanes_applies_implications() -> None:
    assert close_lanes({"packaging"}) == {"packaging", "desktop"}
    assert close_lanes({"docs"}) == {"docs"}
    assert close_lanes(()) == frozenset()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": "x", "patterns": ()},
        {"name": "", "patterns": ("a",)},
        {"name": "x", "patterns": ("a",)},
        {"name": "x", "patterns": ("a",), "lanes": frozenset({"docs"}), "full": True},
        {"name": "x", "patterns": ("a",), "lanes": frozenset({"bogus"})},
        {"name": "x", "patterns": ("/abs",), "lanes": DOCS},
        {"name": "x", "patterns": ("a/**/b",), "lanes": DOCS},
        {"name": "x", "patterns": ("*/x.py",), "lanes": DOCS},
        {"name": "x", "patterns": ("**",), "lanes": DOCS},
        {"name": "x", "patterns": ("../x",), "lanes": DOCS},
        {"name": "x", "patterns": ("a\\b",), "lanes": DOCS},
    ],
)
def test_rules_reject_invalid_definitions(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        Rule(**kwargs)


def test_a_directory_glob_matches_only_direct_children() -> None:
    assert ci_plan.pattern_matches("tests/test_*.py", "tests/test_a.py")
    assert not ci_plan.pattern_matches("tests/test_*.py", "tests/x/test_a.py")
    assert ci_plan.pattern_matches("*.md", "AGENTS.md")
    assert not ci_plan.pattern_matches("*.md", "docs/AGENTS.md")
    assert ci_plan.pattern_matches("docs/**", "docs/a/b/c")
    assert not ci_plan.pattern_matches("docs/**", "docs")
    assert not ci_plan.pattern_matches("docs/**", "docsx/a")


# Plan values.


def _plan(*lanes: str, checked_out: str = MERGE) -> Plan:
    return Plan(
        version=1,
        full=False,
        lanes=frozenset(lanes),
        reasons=("r",),
        checked_out=checked_out,
    )


def test_plan_json_is_canonical_and_round_trips() -> None:
    plan = _plan("docs", "core")
    text = plan.to_json()
    assert "\n" not in text
    document = json.loads(text)
    assert list(document) == sorted(document)
    assert document["lanes"] == {lane: lane in {"docs", "core"} for lane in LANES}
    assert Plan.from_json(text) == plan
    full = full_plan(MERGE, "why")
    assert Plan.from_json(full.to_json()) == full


def _document(**changes: Any) -> str:
    document = json.loads(_plan("docs").to_json())
    document.update(changes)
    return json.dumps(document)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json",
        "[]",
        _document(extra=1),
        _document(version=2),
        _document(version=True),
        _document(full="false"),
        _document(lanes={"docs": True}),
        _document(lanes={**dict.fromkeys(LANES, False), "docs": "yes"}),
        _document(lanes={**dict.fromkeys(LANES, False), "packaging": True}),
        _document(full=True),
        _document(reasons="r"),
        _document(reasons=[1]),
        _document(checked_out=None),
    ],
)
def test_plan_parsing_is_strict(text: str) -> None:
    with pytest.raises(PlanError):
        Plan.from_json(text)


def test_lane_outputs_spell_every_lane() -> None:
    assert _plan("docs").lane_outputs() == {
        lane: "true" if lane == "docs" else "false" for lane in LANES
    }


def test_union_adds_lanes_and_keeps_full() -> None:
    union = _plan("docs").union(_plan("packaging", "desktop"))
    assert union.lanes == {"docs", "packaging", "desktop"} and not union.full
    assert _plan("docs").union(full_plan(MERGE, "x")).lanes == ALL_LANES
    assert full_plan(MERGE, "x").union(_plan()).full


# compute_plan fail-closed rules.


def _event(labels: Sequence[str] = (), head: str = HEAD, base: str = BASE) -> dict:
    return {
        "pull_request": {
            "labels": [{"name": name} for name in labels],
            "head": {"sha": head},
            "base": {"sha": base},
        }
    }


def _git(
    *,
    head: str = MERGE,
    parents: Sequence[str] = (BASE, HEAD),
    diff: Sequence[str] = ("docs/index.md",),
    error: BaseException | None = None,
) -> Callable[[Sequence[str]], str]:
    def run(arguments: Sequence[str]) -> str:
        if error is not None:
            raise error
        if list(arguments) == ["rev-parse", "HEAD"]:
            return head + "\n"
        if list(arguments[:2]) == ["rev-list", "--parents"]:
            return " ".join([head, *parents]) + "\n"
        if arguments[0] == "diff":
            assert list(arguments) == [
                "diff",
                "--no-renames",
                "--name-only",
                "-z",
                "HEAD^1",
                "HEAD",
            ]
            return "".join(f"{path}\0" for path in diff)
        raise AssertionError(arguments)

    return run


def _compute(event_name: str = "pull_request", **kwargs: Any) -> Plan:
    event = kwargs.pop("event", _event())
    checked_out = kwargs.pop("checked_out", MERGE)
    return compute_plan(
        event_name=event_name, event=event, checked_out=checked_out, git=_git(**kwargs)
    )


def test_a_docs_only_pull_request_selects_the_docs_lane() -> None:
    plan = _compute()
    assert not plan.full and plan.lanes == DOCS and plan.checked_out == MERGE


def test_a_tui_pull_request_selects_lint_and_core() -> None:
    plan = _compute(diff=("src/tongs/views/inbox.py", "tests/test_commands.py"))
    assert not plan.full and plan.lanes == TUI


def test_a_core_tests_only_pull_request_selects_lint_and_core() -> None:
    plan = _compute(diff=("tests/test_commands.py", "tests/test_views/test_inbox.py"))
    assert not plan.full and plan.lanes == CORE_TESTS


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"event_name": "push"}, "not pull_request"),
        ({"event_name": "workflow_dispatch"}, "not pull_request"),
        ({"event": _event(labels=["bug", FULL_LABEL])}, FULL_LABEL),
        ({"head": "d" * 40}, "is not the checked-out"),
        ({"parents": (BASE,)}, "1 parent"),
        ({"parents": (BASE, HEAD, "e" * 40)}, "3 parent"),
        ({"parents": (BASE, "e" * 40)}, "HEAD^2"),
        ({"event": _event(head="")}, "head SHA"),
        ({"event": _event(head=ZERO)}, "head SHA"),
        ({"event": _event(base=ZERO)}, "base SHA"),
        ({"event": _event(base="short")}, "base SHA"),
        ({"checked_out": ZERO, "head": ZERO}, "checked-out SHA"),
        ({"checked_out": ""}, "checked-out SHA"),
        (
            {"error": subprocess.CalledProcessError(128, ["git", "diff"], b"", b"bad")},
            "git error",
        ),
        ({"error": RuntimeError("boom")}, "classifier error"),
        ({"diff": ()}, "empty diff"),
        ({"event": {}}, "no pull_request"),
        ({"event": {"pull_request": {"labels": "ci:full"}}}, "labels are malformed"),
        ({"event": {"pull_request": {"head": [], "base": None}}}, "head SHA"),
        ({"event": None}, "no pull_request"),
    ],
)
def test_every_doubt_selects_the_full_graph(
    kwargs: dict[str, Any], reason: str
) -> None:
    event_name = kwargs.pop("event_name", "pull_request")
    plan = _compute(event_name, **kwargs)
    assert plan.full and plan.lanes == ALL_LANES
    assert any(reason in item for item in plan.reasons), plan.reasons


def test_compute_plan_never_raises_on_hostile_input() -> None:
    class Exploding(dict):
        def get(self, *_: Any) -> Any:
            raise KeyError("boom")

    plan = compute_plan(
        event_name="pull_request", event=Exploding(), checked_out=MERGE, git=_git()
    )
    assert plan.full


# A real repository: merge parents and rename semantics.


def _run(repository: Path, *arguments: str) -> str:
    environment = {
        **os.environ,
        "GIT_AUTHOR_NAME": "ci",
        "GIT_AUTHOR_EMAIL": "ci@example.invalid",
        "GIT_COMMITTER_NAME": "ci",
        "GIT_COMMITTER_EMAIL": "ci@example.invalid",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    completed = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-C", str(repository), *arguments],
        capture_output=True,
        text=True,
        check=True,
        env=environment,
    )
    return completed.stdout.strip()


def _write(repository: Path, path: str, text: str = "x\n") -> None:
    target = repository / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


@pytest.fixture()
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _run(root, "init", "-q", "-b", "main")
    _write(root, "docs/guide.md", "guide\n" * 20)
    # A second docs file keeps a rename out of git's directory-rename logic.
    _write(root, "docs/index.md", "index\n" * 20)
    _write(root, "src/tongs/views/inbox.py")
    _run(root, "add", "-A")
    _run(root, "commit", "-q", "-m", "base")
    return root


def _pull_request(repository: Path, change: Callable[[], None]) -> tuple[str, str]:
    """Commit ``change`` on a branch and build GitHub's two-parent merge."""

    _run(repository, "checkout", "-q", "-b", "topic")
    change()
    _run(repository, "add", "-A")
    _run(repository, "commit", "-q", "-m", "topic")
    head = _run(repository, "rev-parse", "HEAD")
    _run(repository, "checkout", "-q", "main")
    _write(repository, "docs/other.md")
    _run(repository, "add", "-A")
    _run(repository, "commit", "-q", "-m", "base moves on")
    base = _run(repository, "rev-parse", "HEAD")
    _run(repository, "merge", "-q", "--no-ff", "-m", "merge", "topic")
    return head, base


def _real_git(repository: Path) -> Callable[[Sequence[str]], str]:
    return lambda arguments: ci_plan.run_git(["-C", str(repository), *arguments])


def test_a_real_merge_classifies_only_the_pull_request_diff(repository: Path) -> None:
    head, base = _pull_request(
        repository, lambda: _write(repository, "src/tongs/views/inbox.py", "y\n")
    )
    merge = _run(repository, "rev-parse", "HEAD")
    plan = compute_plan(
        event_name="pull_request",
        event=_event(head=head, base=base),
        checked_out=merge,
        git=_real_git(repository),
    )
    # The base branch's own docs change is HEAD^1's side and does not count.
    assert not plan.full and plan.lanes == TUI


def test_a_rename_counts_both_its_old_and_new_path(repository: Path) -> None:
    def rename() -> None:
        _run(repository, "mv", "docs/guide.md", "src/tongs/views/guide.md")

    head, base = _pull_request(repository, rename)
    merge = _run(repository, "rev-parse", "HEAD")
    git = _real_git(repository)
    assert set(ci_plan.changed_paths(git)) == {
        "docs/guide.md",
        "src/tongs/views/guide.md",
    }
    plan = compute_plan(
        event_name="pull_request",
        event=_event(head=head, base=base),
        checked_out=merge,
        git=git,
    )
    assert not plan.full and plan.lanes == DOCS | TUI


def test_a_real_non_merge_commit_selects_the_full_graph(repository: Path) -> None:
    commit = _run(repository, "rev-parse", "HEAD")
    plan = compute_plan(
        event_name="pull_request",
        event=_event(),
        checked_out=commit,
        git=_real_git(repository),
    )
    assert plan.full
    assert any("git error" in reason or "parent" in reason for reason in plan.reasons)


def test_explain_plans_a_local_branch_from_its_merge_base(repository: Path) -> None:
    _run(repository, "checkout", "-q", "-b", "topic")
    _write(repository, "docs/new.md")
    _run(repository, "add", "-A")
    _run(repository, "commit", "-q", "-m", "docs")
    _run(repository, "checkout", "-q", "main")
    _write(repository, "src/tongs/forges/github.py")
    _run(repository, "add", "-A")
    _run(repository, "commit", "-q", "-m", "main moves on")
    git = _real_git(repository)
    plan, paths = explain_plan(base="main", head="topic", labels=(), git=git)
    assert paths == ("docs/new.md",)
    assert not plan.full and plan.lanes == DOCS
    plan, _ = explain_plan(base="main", head="topic", labels=[FULL_LABEL], git=git)
    assert plan.full
    plan, _ = explain_plan(base="nowhere", head="topic", labels=(), git=git)
    assert plan.full and "git error" in plan.reasons[0]


def test_the_explain_command_prints_the_plan(repository: Path) -> None:
    _run(repository, "checkout", "-q", "-b", "topic")
    _write(repository, "src/tongs/widgets/x.py")
    _run(repository, "add", "-A")
    _run(repository, "commit", "-q", "-m", "tui")
    completed = subprocess.run(
        [sys.executable, str(PROGRAM), "explain", "--base", "main"],
        capture_output=True,
        text=True,
        cwd=repository,
        check=True,
    )
    assert "reduced graph" in completed.stdout
    assert "Lanes: lint, core\n" in completed.stdout
    assert "src/tongs/widgets/x.py" in completed.stdout


# effective_plan.


def test_effective_plan_unions_agreeing_plans() -> None:
    recomputed = _plan("docs")
    assert effective_plan(
        recomputed=recomputed,
        upstream_json=recomputed.to_json(),
        changes_result="success",
    ).lanes == {"docs"}


def test_effective_plan_takes_the_larger_side() -> None:
    upstream = _plan("docs", "lint", "core")
    effective = effective_plan(
        recomputed=_plan("docs"),
        upstream_json=upstream.to_json(),
        changes_result="success",
    )
    assert effective.lanes == {"docs", "lint", "core"}
    effective = effective_plan(
        recomputed=upstream,
        upstream_json=_plan("docs").to_json(),
        changes_result="success",
    )
    assert effective.lanes == {"docs", "lint", "core"}
    assert plan_mismatches(upstream, _plan("docs").to_json()) == (
        "lane lint: upstream=False, recomputed=True",
        "lane core: upstream=False, recomputed=True",
    )


@pytest.mark.parametrize(
    ("upstream", "result", "reason"),
    [
        (None, "success", "missing"),
        ("", "success", "missing"),
        ("{broken", "success", "unparseable"),
        (_plan("docs").to_json(), "failure", "'failure'"),
        (_plan("docs").to_json(), "cancelled", "'cancelled'"),
        (_plan("docs").to_json(), "skipped", "'skipped'"),
        (
            _plan("docs", checked_out="d" * 40).to_json(),
            "success",
            "upstream plan is for",
        ),
        (full_plan(MERGE, "upstream full").to_json(), "success", "upstream full"),
    ],
)
def test_effective_plan_fails_closed(
    upstream: str | None, result: str, reason: str
) -> None:
    effective = effective_plan(
        recomputed=_plan("docs"), upstream_json=upstream, changes_result=result
    )
    assert effective.full and effective.lanes == ALL_LANES
    assert any(reason in item for item in effective.reasons), effective.reasons


def test_plan_mismatches_report_an_unusable_upstream() -> None:
    assert plan_mismatches(_plan("docs"), None)[0].startswith(
        "upstream plan is unusable"
    )
    assert plan_mismatches(_plan("docs"), _plan("docs").to_json()) == ()


# Verifier expectations derived from a plan.


def test_expected_results_for_partial_desktop_selection() -> None:
    plan = _plan("lint", "core", "desktop_fixtures", "desktop")
    ci = expected_ci_results(plan)
    assert ci["desktop-production"] == {"success"}
    assert ci["docs"] == {"skipped"} and ci["fedora-podman"] == {"skipped"}
    assert ci["changes"] == {"success"}
    production = expected_production_results(plan)
    assert production is not None
    assert {job for job, allowed in production.items() if allowed == {"skipped"}} == (
        LANE_PRODUCTION_JOBS["packaging"]
    )
    assert selected_checks(plan) == LANE_CHECKS["core"] | LANE_CHECKS["desktop"]


def test_expected_results_for_the_full_and_docs_plans() -> None:
    full = full_plan(MERGE, "x")
    assert expected_ci_results(full)["changes"] == ci_plan.JOB_RESULTS
    assert all(
        allowed == {"success"}
        for job, allowed in expected_ci_results(full).items()
        if job != "changes"
    )
    docs = _plan("docs")
    assert expected_production_results(docs) is None
    assert selected_checks(docs) == frozenset()


def test_expectations_reject_an_unclosed_plan() -> None:
    with pytest.raises(PlanError):
        expected_ci_results(_plan("packaging"))


# The command line.


def _write_event(tmp_path: Path, document: object) -> Path:
    path = tmp_path / "event.json"
    path.write_text(json.dumps(document))
    return path


def _main(*arguments: str) -> int:
    return ci_plan.main(list(arguments))


def test_compute_writes_the_plan_and_every_lane_output(tmp_path: Path) -> None:
    output = tmp_path / "plan.json"
    github_output = tmp_path / "github-output"
    summary = tmp_path / "summary.md"
    code = _main(
        "compute",
        "--event-name",
        "push",
        "--event-path",
        str(_write_event(tmp_path, {})),
        "--checked-out",
        MERGE,
        "--output",
        str(output),
        "--github-output",
        str(github_output),
        "--step-summary",
        str(summary),
    )
    assert code == 0
    plan = Plan.from_json(output.read_text())
    assert plan.full
    lines = github_output.read_text().splitlines()
    assert lines[0] == f"plan={plan.to_json()}"
    assert lines[1:] == [f"{lane}=true" for lane in LANES]
    text = summary.read_text()
    assert "full graph" in text and "Merge parents" in text


def test_compute_exits_zero_on_an_unreadable_pull_request_event(tmp_path: Path) -> None:
    output = tmp_path / "plan.json"
    code = _main(
        "compute",
        "--event-name",
        "pull_request",
        "--event-path",
        str(tmp_path / "absent.json"),
        "--checked-out",
        MERGE,
        "--output",
        str(output),
    )
    assert code == 0
    plan = Plan.from_json(output.read_text())
    assert plan.full and "unreadable" in plan.reasons[0]


def test_compute_reports_an_io_failure_with_a_non_zero_exit(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("")
    code = _main(
        "compute",
        "--event-name",
        "push",
        "--checked-out",
        MERGE,
        "--output",
        str(blocker / "plan.json"),
    )
    assert code == 2


def test_effective_writes_the_union_and_lists_mismatches(tmp_path: Path) -> None:
    output = tmp_path / "plan.json"
    github_output = tmp_path / "github-output"
    summary = tmp_path / "summary.md"
    code = _main(
        "effective",
        "--event-name",
        "push",
        "--event-path",
        str(_write_event(tmp_path, {})),
        "--checked-out",
        MERGE,
        "--upstream-json",
        _plan("docs").to_json(),
        "--changes-result",
        "success",
        "--output",
        str(output),
        "--github-output",
        str(github_output),
        "--step-summary",
        str(summary),
    )
    assert code == 0
    assert Plan.from_json(output.read_text()).full
    assert "core=true" in github_output.read_text().splitlines()
    text = summary.read_text()
    assert "Upstream and recomputed plans differ" in text
    assert "Merge parents" in text


def test_render_table_is_generated_from_the_rules(
    capsys: pytest.CaptureFixture,
) -> None:
    table = render_table()
    for rule in RULES:
        assert f"| {rule.name} |" in table
    assert "| (unmatched) | any other path | full graph |" in table
    assert _main("render-table") == 0
    assert capsys.readouterr().out == table


def test_the_program_runs_standalone_with_the_standard_library() -> None:
    completed = subprocess.run(
        [sys.executable, "-I", str(PROGRAM), "render-table"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == render_table()


# No rule without the desktop lane may cover the sidecar's import closure.

# Paths are reported relative to the directory containing the tongs package, so
# the test works from a source checkout and from an installed wheel alike.
_CLOSURE_PROBE = """
import json, os, sys
import tongs
import tongs.desktop.sidecar
import tongs.desktop.protocol.server
root = os.path.dirname(os.path.dirname(os.path.abspath(tongs.__file__)))
print(json.dumps(sorted(
    os.path.relpath(os.path.abspath(module.__file__), root).replace(os.sep, "/")
    for name, module in list(sys.modules.items())
    if (name == "tongs" or name.startswith("tongs."))
    and getattr(module, "__file__", None)
)))
"""


def test_the_sidecar_import_closure_always_selects_desktop() -> None:
    completed = subprocess.run(
        [sys.executable, "-c", _CLOSURE_PROBE],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=True,
    )
    closure = []
    for relative in json.loads(completed.stdout):
        assert relative.startswith("tongs/"), (
            f"unexpected tongs module path: {relative}"
        )
        closure.append(f"src/{relative}")
    assert "src/tongs/desktop/sidecar.py" in closure
    assert "src/tongs/desktop/protocol/server.py" in closure
    offenders = []
    for path in closure:
        lanes, full, _ = classify_paths([path])
        if not full and "desktop" not in lanes:
            offenders.append(f"{path} -> {sorted(lanes)}")
    assert offenders == [], offenders
