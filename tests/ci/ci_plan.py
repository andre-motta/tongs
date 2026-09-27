"""Select the CI lanes a change needs, failing closed to the full graph.

This program is the single source of lane policy for ``ci.yml``.  The ``changes``
job runs ``compute`` on the checked-out synthetic merge commit and publishes one
``'true'``/``'false'`` output per lane; every lane job keys its ``if`` on those
outputs.  The always-run aggregate then runs ``effective``, which recomputes the
plan itself and unions it with the upstream plan, and both verifiers read that
effective plan: a selected lane must succeed and a deselected lane must report
exactly ``skipped``.

Classification is a union over :data:`RULES`.  Every rule a changed path
matches contributes its lanes, a path that matches a full rule or no rule at all
selects the full graph, and adding matches can only add lanes.  Patterns match
by path, never by extension: an exact path, a ``prefix/**`` subtree, or a glob
in the final path segment that matches only files directly inside that
directory (``*.md`` is therefore a repository-root glob).

Every doubt selects the full graph with its reason recorded: an event other
than ``pull_request``, the ``ci:full`` label, a checked-out commit that is not
the expected synthetic merge, a missing or zero SHA, any git error, an empty
diff and any unexpected exception.  :func:`compute_plan` never raises.

The classifier guards against accidental skips, not malicious ones.  For a pull
request ``ci.yml`` itself comes from the merge commit, so a hostile change could
rewrite the gate regardless; rule edits live under ``tests/ci/**``, which always
selects the full graph, and every push to ``main`` runs the full graph.

The program is standard library only, because the ``changes`` job and the
aggregate install nothing.  The verifiers load it by path.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

#: Every lane, in presentation order.
LANES: tuple[str, ...] = (
    "docs",
    "lint",
    "core",
    "desktop_fixtures",
    "fedora_podman",
    "desktop",
    "packaging",
)
ALL_LANES: frozenset[str] = frozenset(LANES)
FULL_LABEL = "ci:full"
PLAN_VERSION = 1

#: A lane that cannot run without another.  Packaging jobs run inside the
#: called desktop production workflow, so selecting them selects desktop.
LANE_IMPLIES: dict[str, frozenset[str]] = {"packaging": frozenset({"desktop"})}

#: The ``ci.yml`` job that owns each lane.  Packaging has no job of its own.
LANE_CI_JOBS: dict[str, str] = {
    "docs": "docs",
    "lint": "lint-and-format",
    "core": "core",
    "desktop_fixtures": "desktop-fixtures",
    "fedora_podman": "fedora-podman",
    "desktop": "desktop-production",
}
#: The planning job every lane job needs.
CHANGES_JOB = "changes"

#: Jobs of the called ``desktop-production.yml`` that each lane owns.
LANE_PRODUCTION_JOBS: dict[str, frozenset[str]] = {
    "desktop": frozenset(
        {"source-identity", "desktop-tap", "installed-core", "native-payload"}
    ),
    "packaging": frozenset(
        {"archive", "archive-evidence", "archive-sbom", "rpm-lifecycle"}
    ),
}

#: Receipt-bearing gate checks each lane publishes.
LANE_CHECKS: dict[str, frozenset[str]] = {
    "core": frozenset({"core-python-3.12", "core-python-3.13"}),
    "desktop": frozenset(
        {
            "desktop-production-tap",
            "desktop-installed-core",
            "desktop-native-payload-fixture",
        }
    ),
    "packaging": frozenset(
        {
            "desktop-archive-lifecycle",
            "desktop-archive-sbom",
            "desktop-rpm-lifecycle",
        }
    ),
}

#: Every result GitHub reports for a job.
JOB_RESULTS: frozenset[str] = frozenset({"success", "failure", "cancelled", "skipped"})

_GLOB_CHARACTERS = frozenset("*?[")
_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
#: Path-specific reasons kept in a plan before the rest are counted, so a huge
#: diff cannot push the ``plan`` job output past GitHub's size limits.
MAX_PATH_REASONS = 20


def _has_glob(text: str) -> bool:
    return any(character in _GLOB_CHARACTERS for character in text)


def _valid_path(path: str) -> bool:
    if not path or path.startswith("/") or "\\" in path or "\x00" in path:
        return False
    return all(part not in {"", ".", ".."} for part in path.split("/"))


def _valid_pattern(pattern: str) -> bool:
    if not pattern or pattern.startswith("/") or "\\" in pattern:
        return False
    if pattern.endswith("/**"):
        prefix = pattern[:-3]
        return bool(prefix) and not _has_glob(prefix) and _valid_path(prefix)
    parent, _, name = pattern.rpartition("/")
    if _has_glob(parent) or not name or name == "**":
        return False
    return not parent or _valid_path(parent)


def pattern_matches(pattern: str, path: str) -> bool:
    """Return whether one rule pattern matches one repository-relative path."""

    if pattern.endswith("/**"):
        return path.startswith(pattern[:-2])
    if _has_glob(pattern):
        parent, _, glob = pattern.rpartition("/")
        path_parent, _, name = path.rpartition("/")
        return path_parent == parent and fnmatchcase(name, glob)
    return path == pattern


@dataclass(frozen=True, slots=True)
class Rule:
    """One path rule: the lanes it selects, or the full graph."""

    name: str
    patterns: tuple[str, ...]
    lanes: frozenset[str] = frozenset()
    full: bool = False

    def __post_init__(self) -> None:
        if not self.name or not self.patterns:
            raise ValueError("a rule needs a name and at least one pattern")
        invalid = [pattern for pattern in self.patterns if not _valid_pattern(pattern)]
        if invalid:
            raise ValueError(f"rule {self.name!r} has invalid patterns {invalid}")
        unknown = sorted(set(self.lanes) - ALL_LANES)
        if unknown:
            raise ValueError(f"rule {self.name!r} names unknown lanes {unknown}")
        if self.full == bool(self.lanes):
            raise ValueError(
                f"rule {self.name!r} must either select lanes or the full graph"
            )

    def matches(self, path: str) -> bool:
        return any(pattern_matches(pattern, path) for pattern in self.patterns)


RULES: tuple[Rule, ...] = (
    Rule(
        name="docs",
        patterns=(
            "docs/**",
            "site/**",
            ".agents/**",
            "*.md",
            ".github/ISSUE_TEMPLATE/**",
            ".github/PULL_REQUEST_TEMPLATE.md",
            ".github/PULL_REQUEST_TEMPLATE/**",
            ".github/FUNDING.yml",
            ".github/linters/**",
        ),
        lanes=frozenset({"docs"}),
    ),
    # The wheel's readme, so the core lane builds it as well as the docs lane.
    Rule(name="readme", patterns=("README.md",), lanes=frozenset({"docs", "core"})),
    # Files that core-lane tests read (the generated lane tables and the pinned
    # markdown linter), so editing them runs those tests as well as the docs lane.
    Rule(
        name="core-read-docs",
        patterns=(
            ".agents/ci/README.md",
            ".agents/testing/README.md",
            ".github/linters/**",
        ),
        lanes=frozenset({"docs", "core"}),
    ),
    # Terminal modules.  The sidecar never imports them.  By CTO decision they
    # run lint and core only: the desktop installed-core job also launches the
    # installed TUI, so a TUI change that breaks that launch surfaces on the
    # full-graph push to main (or before merge with the ci:full label).
    Rule(
        name="tui",
        patterns=(
            "src/tongs/views/**",
            "src/tongs/widgets/**",
            "src/tongs/mcp/**",
            "src/tongs/app.py",
            "src/tongs/commands.py",
            "src/tongs/helpers.py",
            "src/tongs/__main__.py",
        ),
        lanes=frozenset({"lint", "core"}),
    ),
    # The suites only the core lane runs.  None of them is imported by a
    # desktop job's tests, which test_ci_plan_drift.py keeps honest.
    Rule(
        name="core-tests",
        patterns=(
            "tests/test_*.py",
            "tests/test_cache/**",
            "tests/test_diff/**",
            "tests/test_forges/**",
            "tests/test_mcp/**",
            "tests/test_plugins/**",
            "tests/test_scanner/**",
            "tests/test_views/**",
            "tests/test_widgets/**",
        ),
        lanes=frozenset({"lint", "core"}),
    ),
    # Everything the desktop sidecar reaches, including forge clients it loads
    # at run time, plus the suites and fixtures the desktop jobs run.  The
    # ``tui_services`` adapter is here because the desktop TAP job's draft
    # acceptance test imports it.  None of this is read by the archive, SBOM
    # or RPM jobs except through the packaging rule below.
    Rule(
        name="sidecar",
        patterns=(
            "src/tongs/cache/**",
            "src/tongs/config.py",
            "src/tongs/desktop/**",
            "src/tongs/diff/**",
            "src/tongs/errors.py",
            "src/tongs/forges/**",
            "src/tongs/plugins/**",
            "src/tongs/scanner/**",
            "src/tongs/services/**",
            "src/tongs/state/**",
            "src/tongs/tui_services.py",
            "tests/__init__.py",
            "tests/desktop/**",
            "tests/fixtures/**",
            "tests/integration/**",
            "tests/plugins/**",
            "tests/services/**",
            "tests/state/**",
            "examples/desktop-plugin/**",
        ),
        lanes=frozenset({"lint", "core", "desktop_fixtures", "desktop"}),
    ),
    # The archive producer's _SOURCE_INPUTS outside the full-graph roots, the
    # programs the archive, archive-evidence, archive-sbom and rpm-lifecycle
    # jobs run, the tongs modules those programs import, and the files they
    # read.  test_ci_plan_drift.py derives each of those sets and checks it.
    Rule(
        name="packaging",
        patterns=(
            "LICENSE",
            "scripts/build_desktop_archive.py",
            "scripts/build_desktop_sbom.py",
            "src/tongs/__init__.py",
            "src/tongs/desktop/artifact_contract/**",
            "src/tongs/desktop/installer/**",
            "tests/integration/desktop/archive_evidence.py",
            "tests/integration/desktop/candidate_attestation.py",
            "tests/integration/desktop/rpm_payload_contract.py",
            "tests/integration/desktop/sbom_evidence.py",
            "tests/desktop/installer/fixtures/**",
            "tests/packaging/**",
        ),
        lanes=frozenset({"lint", "core", "desktop_fixtures", "desktop", "packaging"}),
    ),
    # The spike prototypes run only in the desktop fixture job.
    Rule(name="spikes", patterns=("spikes/**",), lanes=frozenset({"desktop_fixtures"})),
    # Manual native evidence tooling.  No CI job runs it, so only lint checks it.
    Rule(
        name="release-evidence",
        patterns=("scripts/release-evidence/**",),
        lanes=frozenset({"lint"}),
    ),
    Rule(
        name="ci-infrastructure",
        patterns=(
            ".github/workflows/**",
            ".github/scripts/**",
            "tests/ci/**",
            "tests/containers/**",
        ),
        full=True,
    ),
    # Build and packaging inputs.  These are the only paths, with the CI
    # infrastructure above, that select the Fedora Podman probe.  Hatchling
    # reads .gitignore to choose the files a wheel or sdist ships.
    Rule(
        name="build-configuration",
        patterns=(
            "pyproject.toml",
            "requirements/**",
            "packaging/**",
            "desktop/**",
            ".gitignore",
        ),
        full=True,
    ),
)


def close_lanes(lanes: Iterable[str]) -> frozenset[str]:
    """Add every lane that a selected lane implies."""

    closed = set(lanes)
    pending = list(closed)
    while pending:
        for implied in LANE_IMPLIES.get(pending.pop(), frozenset()):
            if implied not in closed:
                closed.add(implied)
                pending.append(implied)
    return frozenset(closed)


def _capped(reasons: list[str]) -> list[str]:
    if len(reasons) <= MAX_PATH_REASONS:
        return reasons
    omitted = len(reasons) - MAX_PATH_REASONS
    return [*reasons[:MAX_PATH_REASONS], f"... and {omitted} more"]


def classify_paths(
    paths: Sequence[str],
) -> tuple[frozenset[str], bool, tuple[str, ...]]:
    """Classify changed paths into ``(lanes, full, reasons)``.

    Lane closure through :data:`LANE_IMPLIES` is applied, and a full result
    carries every lane.  An empty path list is full, because an empty diff
    proves nothing about what changed.
    """

    if not paths:
        return ALL_LANES, True, ("empty diff",)
    lanes: set[str] = set()
    full_reasons: list[str] = []
    rule_counts: dict[str, int] = {}
    for path in paths:
        if not isinstance(path, str) or not _valid_path(path):
            full_reasons.append(f"invalid path {path!r}")
            continue
        matched = [rule for rule in RULES if rule.matches(path)]
        if not matched:
            full_reasons.append(f"unmatched path {path}")
            continue
        for rule in matched:
            rule_counts[rule.name] = rule_counts.get(rule.name, 0) + 1
            if rule.full:
                full_reasons.append(f"{path} matches full rule {rule.name}")
            lanes.update(rule.lanes)
    rule_reasons = [
        f"rule {rule.name} matched {rule_counts[rule.name]} path(s)"
        for rule in RULES
        if rule.name in rule_counts
    ]
    if full_reasons:
        return ALL_LANES, True, tuple(_capped(full_reasons) + rule_reasons)
    return close_lanes(lanes), False, tuple(rule_reasons)


class PlanError(ValueError):
    """Raised when a plan document or plan value is malformed."""


@dataclass(frozen=True, slots=True)
class Plan:
    """The lanes one run selects and why."""

    version: int
    full: bool
    lanes: frozenset[str]
    reasons: tuple[str, ...]
    checked_out: str

    def to_json(self) -> str:
        """Canonical single-line JSON with every lane spelled out."""

        document = {
            "version": self.version,
            "full": self.full,
            "lanes": {lane: lane in self.lanes for lane in LANES},
            "reasons": list(self.reasons),
            "checked_out": self.checked_out,
        }
        return json.dumps(document, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str) -> Plan:
        """Parse a plan strictly, raising ``ValueError`` on any deviation."""

        try:
            document = json.loads(text)
        except (TypeError, json.JSONDecodeError) as error:
            raise PlanError("plan is not JSON") from error
        if not isinstance(document, dict):
            raise PlanError("plan must be a JSON object")
        expected = {"version", "full", "lanes", "reasons", "checked_out"}
        if set(document) != expected:
            raise PlanError(f"plan keys {sorted(document)} are not {sorted(expected)}")
        if document["version"] != PLAN_VERSION or isinstance(document["version"], bool):
            raise PlanError(f"unsupported plan version {document['version']!r}")
        if not isinstance(document["full"], bool):
            raise PlanError("plan 'full' must be a boolean")
        lanes = document["lanes"]
        if not isinstance(lanes, dict) or set(lanes) != ALL_LANES:
            raise PlanError("plan 'lanes' must name every lane exactly once")
        if not all(isinstance(value, bool) for value in lanes.values()):
            raise PlanError("plan lane values must be booleans")
        reasons = document["reasons"]
        if not isinstance(reasons, list) or not all(
            isinstance(reason, str) for reason in reasons
        ):
            raise PlanError("plan 'reasons' must be a list of strings")
        if not isinstance(document["checked_out"], str):
            raise PlanError("plan 'checked_out' must be a string")
        plan = cls(
            version=PLAN_VERSION,
            full=document["full"],
            lanes=frozenset(lane for lane, selected in lanes.items() if selected),
            reasons=tuple(reasons),
            checked_out=document["checked_out"],
        )
        validate_plan(plan)
        return plan

    def lane_outputs(self) -> dict[str, str]:
        """One ``'true'``/``'false'`` value per lane, for a job's outputs."""

        return {lane: "true" if lane in self.lanes else "false" for lane in LANES}

    def union(self, other: Plan) -> Plan:
        """Every lane either plan selects; full when either is full."""

        full = self.full or other.full
        reasons = self.reasons + tuple(
            reason for reason in other.reasons if reason not in self.reasons
        )
        return Plan(
            version=PLAN_VERSION,
            full=full,
            lanes=ALL_LANES if full else close_lanes(self.lanes | other.lanes),
            reasons=reasons,
            checked_out=self.checked_out,
        )


def validate_plan(plan: Plan) -> None:
    """Reject a plan whose lanes are unknown, unclosed or inconsistent."""

    unknown = sorted(set(plan.lanes) - ALL_LANES)
    if unknown:
        raise PlanError(f"plan names unknown lanes {unknown}")
    if close_lanes(plan.lanes) != plan.lanes:
        raise PlanError("plan lanes are not closed under LANE_IMPLIES")
    if plan.full and plan.lanes != ALL_LANES:
        raise PlanError("a full plan must select every lane")


def full_plan(checked_out: str, *reasons: str) -> Plan:
    """The full graph, carrying why it was chosen."""

    return Plan(
        version=PLAN_VERSION,
        full=True,
        lanes=ALL_LANES,
        reasons=tuple(reasons),
        checked_out=checked_out,
    )


def _plan_for_paths(checked_out: str, paths: Sequence[str]) -> Plan:
    lanes, full, reasons = classify_paths(paths)
    return Plan(
        version=PLAN_VERSION,
        full=full,
        lanes=lanes,
        reasons=reasons,
        checked_out=checked_out,
    )


def _real_sha(value: object) -> bool:
    return (
        isinstance(value, str)
        and _SHA.fullmatch(value) is not None
        and set(value) != {"0"}
    )


def _split_null(output: str) -> list[str]:
    return [path for path in output.split("\0") if path]


def changed_paths(git: Callable[[Sequence[str]], str]) -> list[str]:
    """Paths the checked-out merge commit changes relative to its first parent."""

    return _split_null(
        git(["diff", "--no-renames", "--name-only", "-z", "HEAD^1", "HEAD"])
    )


def _pull_request_plan(
    *,
    event_name: str,
    event: Mapping[str, Any],
    checked_out: str,
    git: Callable[[Sequence[str]], str],
) -> Plan:
    if event_name != "pull_request":
        return full_plan(checked_out, f"event {event_name!r} is not pull_request")
    pull_request = event.get("pull_request") if isinstance(event, Mapping) else None
    if not isinstance(pull_request, Mapping):
        return full_plan(checked_out, "event carries no pull_request object")
    labels = pull_request.get("labels") or []
    if not isinstance(labels, list):
        return full_plan(checked_out, "pull request labels are malformed")
    names = {label.get("name") for label in labels if isinstance(label, Mapping)}
    if FULL_LABEL in names:
        return full_plan(checked_out, f"label {FULL_LABEL} is set")
    head = pull_request.get("head")
    base = pull_request.get("base")
    head_sha = head.get("sha") if isinstance(head, Mapping) else None
    base_sha = base.get("sha") if isinstance(base, Mapping) else None
    if not _real_sha(head_sha):
        return full_plan(checked_out, "pull request head SHA is missing or zero")
    if not _real_sha(base_sha):
        return full_plan(checked_out, "pull request base SHA is missing or zero")
    if not _real_sha(checked_out):
        return full_plan(checked_out, "checked-out SHA is missing or zero")
    head_commit = git(["rev-parse", "HEAD"]).strip()
    if head_commit != checked_out:
        return full_plan(
            checked_out, f"HEAD {head_commit} is not the checked-out {checked_out}"
        )
    parents = git(["rev-list", "--parents", "-n", "1", "HEAD"]).split()[1:]
    if len(parents) != 2:
        return full_plan(
            checked_out, f"HEAD has {len(parents)} parent(s), not a two-parent merge"
        )
    if parents[1] != head_sha:
        return full_plan(
            checked_out,
            f"HEAD^2 {parents[1]} is not the pull request head {head_sha}",
        )
    paths = changed_paths(git)
    if not paths:
        return full_plan(checked_out, "empty diff")
    return _plan_for_paths(checked_out, paths)


def compute_plan(
    *,
    event_name: str,
    event: Mapping[str, Any],
    checked_out: str,
    git: Callable[[Sequence[str]], str],
) -> Plan:
    """Plan the lanes for one run.  Never raises; any failure is full."""

    try:
        return _pull_request_plan(
            event_name=event_name, event=event, checked_out=checked_out, git=git
        )
    except subprocess.CalledProcessError as error:
        return full_plan(str(checked_out), f"git error: {_describe(error)}")
    except Exception as error:  # noqa: BLE001 - every failure selects full
        return full_plan(str(checked_out), f"classifier error: {_describe(error)}")


def effective_plan(
    *, recomputed: Plan, upstream_json: str | None, changes_result: str
) -> Plan:
    """Union the aggregate's own plan with the ``changes`` job's plan.

    The full graph is chosen when ``changes`` did not succeed or its plan is
    missing, unparseable or bound to another commit.
    """

    if changes_result != "success":
        return recomputed.union(
            full_plan(
                recomputed.checked_out,
                f"changes job result is {changes_result!r}, not success",
            )
        )
    if upstream_json is None or not upstream_json.strip():
        return recomputed.union(
            full_plan(recomputed.checked_out, "upstream plan is missing")
        )
    try:
        upstream = Plan.from_json(upstream_json)
    except ValueError as error:
        return recomputed.union(
            full_plan(recomputed.checked_out, f"upstream plan is unparseable: {error}")
        )
    if upstream.checked_out != recomputed.checked_out:
        return recomputed.union(
            full_plan(
                recomputed.checked_out,
                f"upstream plan is for {upstream.checked_out!r}, not "
                f"{recomputed.checked_out!r}",
            )
        )
    return recomputed.union(upstream)


def plan_mismatches(recomputed: Plan, upstream_json: str | None) -> tuple[str, ...]:
    """Describe how the upstream plan differs from the recomputed one."""

    try:
        upstream = Plan.from_json(upstream_json or "")
    except ValueError as error:
        return (f"upstream plan is unusable: {error}",)
    found: list[str] = []
    if upstream.full != recomputed.full:
        found.append(f"full: upstream={upstream.full}, recomputed={recomputed.full}")
    for lane in LANES:
        mine, theirs = lane in recomputed.lanes, lane in upstream.lanes
        if mine != theirs:
            found.append(f"lane {lane}: upstream={theirs}, recomputed={mine}")
    if upstream.checked_out != recomputed.checked_out:
        found.append(
            f"checked_out: upstream={upstream.checked_out!r}, "
            f"recomputed={recomputed.checked_out!r}"
        )
    return tuple(found)


def expected_ci_results(plan: Plan) -> dict[str, frozenset[str]]:
    """Results each ``ci.yml`` job the aggregate needs may report.

    A selected lane must succeed and a deselected lane must report exactly
    ``skipped``.  ``changes`` must succeed, except that a full plan tolerates
    any ``changes`` result because every lane already has to succeed.
    """

    validate_plan(plan)
    expected = {
        job: frozenset({"success"}) if lane in plan.lanes else frozenset({"skipped"})
        for lane, job in LANE_CI_JOBS.items()
    }
    expected[CHANGES_JOB] = JOB_RESULTS if plan.full else frozenset({"success"})
    return expected


def expected_production_results(plan: Plan) -> dict[str, frozenset[str]] | None:
    """Results each called production job may report, or ``None`` when the
    plan deselected desktop and the production results must be empty."""

    validate_plan(plan)
    if "desktop" not in plan.lanes:
        return None
    return {
        job: frozenset({"success"}) if lane in plan.lanes else frozenset({"skipped"})
        for lane, jobs in LANE_PRODUCTION_JOBS.items()
        for job in jobs
    }


def selected_checks(plan: Plan) -> frozenset[str]:
    """The receipt-bearing checks a plan requires."""

    validate_plan(plan)
    return frozenset(
        check for lane in plan.lanes for check in LANE_CHECKS.get(lane, frozenset())
    )


def render_table() -> str:
    """A markdown lane table generated from :data:`RULES`."""

    lines = [
        "| Rule | Paths | Lanes |",
        "| --- | --- | --- |",
    ]
    for rule in RULES:
        patterns = ", ".join(f"`{pattern}`" for pattern in rule.patterns)
        lanes = (
            "full graph"
            if rule.full
            else ", ".join(lane for lane in LANES if lane in close_lanes(rule.lanes))
        )
        lines.append(f"| {rule.name} | {patterns} | {lanes} |")
    lines.append("| (unmatched) | any other path | full graph |")
    lines.append("")
    lines.append(
        "Matching rules add their lanes together, and a push to `main`, the "
        f"`{FULL_LABEL}` label or any doubt about the diff selects the full graph."
    )
    return "\n".join(lines) + "\n"


def _describe(error: BaseException) -> str:
    if isinstance(error, subprocess.CalledProcessError):
        command = " ".join(str(part) for part in error.cmd or ())
        stderr = error.stderr
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        detail = " ".join(str(stderr or "").split())[:200]
        return f"{command} exited {error.returncode}: {detail}".strip()
    return f"{type(error).__name__}: {error}"[:300]


def run_git(arguments: Sequence[str]) -> str:
    """Run git in the current directory and return its decoded stdout."""

    completed = subprocess.run(
        ["git", *arguments],
        capture_output=True,
        check=True,
        timeout=120,
    )
    return completed.stdout.decode("utf-8", errors="replace")


def _load_event(path: Path | None) -> tuple[Mapping[str, Any], str | None]:
    if path is None:
        return {}, "no event path was given"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        return {}, f"event payload is unreadable: {_describe(error)}"
    if not isinstance(document, dict):
        return {}, "event payload is not a JSON object"
    return document, None


def _plan_from_event(arguments: argparse.Namespace) -> Plan:
    event, error = _load_event(arguments.event_path)
    if error is not None and arguments.event_name == "pull_request":
        return full_plan(arguments.checked_out, error)
    return compute_plan(
        event_name=arguments.event_name,
        event=event,
        checked_out=arguments.checked_out,
        git=run_git,
    )


def _write_outputs(plan: Plan, arguments: argparse.Namespace) -> None:
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(plan.to_json() + "\n", encoding="utf-8")
    if arguments.github_output is not None:
        lines = [f"plan={plan.to_json()}"]
        lines.extend(f"{lane}={value}" for lane, value in plan.lane_outputs().items())
        with arguments.github_output.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")


def _markdown_code(text: str) -> str:
    return "`" + text.replace("`", "'").replace("\n", " ") + "`"


def _lane_table(plan: Plan) -> list[str]:
    lines = ["| Lane | Selected | Job |", "| --- | --- | --- |"]
    for lane in LANES:
        job = LANE_CI_JOBS.get(lane)
        if job is None:
            jobs = sorted(LANE_PRODUCTION_JOBS.get(lane, frozenset()))
            job = "desktop-production: " + ", ".join(jobs)
        selected = "yes" if lane in plan.lanes else "no"
        lines.append(f"| {lane} | {selected} | {job} |")
    return lines


def _merge_parents(git: Callable[[Sequence[str]], str]) -> str:
    try:
        commits = git(["rev-list", "--parents", "-n", "1", "HEAD"]).split()
    except Exception as error:  # noqa: BLE001 - summary only
        return f"unavailable ({_describe(error)})"
    if not commits:
        return "unavailable"
    labels = ["HEAD"] + [f"HEAD^{index}" for index in range(1, len(commits))]
    return ", ".join(
        f"{label} {commit}" for label, commit in zip(labels, commits, strict=True)
    )


def _summary(plan: Plan, heading: str, extra: Sequence[str] = ()) -> list[str]:
    graph = "full graph" if plan.full else "reduced graph"
    lines = [f"### {heading}", "", f"Plan: {graph} for {plan.checked_out}", ""]
    lines.extend(_lane_table(plan))
    lines.extend(["", "Reasons:", ""])
    lines.extend(f"- {_markdown_code(reason)}" for reason in plan.reasons or ("none",))
    lines.extend(extra)
    return lines


def _append_summary(path: Path | None, lines: Sequence[str]) -> None:
    if path is None:
        return
    with path.open("a", encoding="utf-8", errors="replace") as handle:
        handle.write("\n".join(lines) + "\n\n")


def _changed_path_lines(event_name: str) -> list[str]:
    lines = ["", f"Merge parents: {_merge_parents(run_git)}", ""]
    if event_name != "pull_request":
        return [*lines, "Changed paths: not evaluated for this event."]
    try:
        paths = changed_paths(run_git)
    except Exception as error:  # noqa: BLE001 - summary only
        return [*lines, f"Changed paths: unavailable ({_describe(error)})"]
    lines.extend(
        [
            f"<details><summary>Changed paths ({len(paths)})</summary>",
            "",
        ]
    )
    shown = paths[:500]
    lines.extend(f"- {_markdown_code(path)}" for path in shown)
    if len(paths) > len(shown):
        lines.append(f"- ... and {len(paths) - len(shown)} more")
    lines.extend(["", "</details>"])
    return lines


def _compute(arguments: argparse.Namespace) -> int:
    try:
        plan = _plan_from_event(arguments)
    except Exception as error:  # noqa: BLE001 - every failure selects full
        plan = full_plan(arguments.checked_out, f"classifier error: {_describe(error)}")
    try:
        _write_outputs(plan, arguments)
    except OSError as error:
        print(f"ci_plan: unable to write the plan: {error}", file=sys.stderr)
        return 2
    try:
        _append_summary(
            arguments.step_summary,
            _summary(plan, "CI lane plan", _changed_path_lines(arguments.event_name)),
        )
    except OSError as error:
        print(f"ci_plan: unable to write the step summary: {error}", file=sys.stderr)
    print(plan.to_json())
    return 0


def _effective(arguments: argparse.Namespace) -> int:
    try:
        recomputed = _plan_from_event(arguments)
    except Exception as error:  # noqa: BLE001 - every failure selects full
        recomputed = full_plan(
            arguments.checked_out, f"classifier error: {_describe(error)}"
        )
    try:
        plan = effective_plan(
            recomputed=recomputed,
            upstream_json=arguments.upstream_json,
            changes_result=arguments.changes_result,
        )
    except Exception as error:  # noqa: BLE001 - every failure selects full
        plan = full_plan(
            arguments.checked_out, f"effective plan error: {_describe(error)}"
        )
    mismatches = plan_mismatches(recomputed, arguments.upstream_json)
    try:
        _write_outputs(plan, arguments)
    except OSError as error:
        print(f"ci_plan: unable to write the plan: {error}", file=sys.stderr)
        return 2
    extra = [
        "",
        f"Merge parents: {_merge_parents(run_git)}",
        "",
        f"Upstream `changes` result: {arguments.changes_result}",
        "",
    ]
    if mismatches:
        extra.append("Upstream and recomputed plans differ:")
        extra.append("")
        extra.extend(f"- {_markdown_code(item)}" for item in mismatches)
    else:
        extra.append("Upstream and recomputed plans agree.")
    try:
        _append_summary(
            arguments.step_summary, _summary(plan, "Effective CI plan", extra)
        )
    except OSError as error:
        print(f"ci_plan: unable to write the step summary: {error}", file=sys.stderr)
    print(plan.to_json())
    return 0


def explain_plan(
    *,
    base: str,
    head: str,
    labels: Sequence[str],
    git: Callable[[Sequence[str]], str],
) -> tuple[Plan, tuple[str, ...]]:
    """Plan a local branch against ``base`` from their merge base."""

    head_commit = ""
    try:
        head_commit = git(["rev-parse", "--verify", f"{head}^{{commit}}"]).strip()
        if FULL_LABEL in labels:
            return full_plan(head_commit, f"label {FULL_LABEL} is set"), ()
        merge_base = git(["merge-base", base, head_commit]).strip()
        paths = tuple(
            _split_null(
                git(["diff", "--no-renames", "--name-only", "-z", merge_base, head])
            )
        )
        if not paths:
            return full_plan(head_commit, "empty diff"), ()
        return _plan_for_paths(head_commit, paths), paths
    except Exception as error:  # noqa: BLE001 - every failure selects full
        return full_plan(head_commit, f"git error: {_describe(error)}"), ()


def _explain(arguments: argparse.Namespace) -> int:
    plan, paths = explain_plan(
        base=arguments.base,
        head=arguments.head,
        labels=arguments.label,
        git=run_git,
    )
    graph = "full graph" if plan.full else "reduced graph"
    print(f"Plan for {arguments.head} against {arguments.base}: {graph}")
    print(f"Changed paths ({len(paths)}):")
    for path in paths:
        print(f"  {path}")
    print("Lanes: " + ", ".join(lane for lane in LANES if lane in plan.lanes))
    jobs = sorted(
        {CHANGES_JOB, "desktop-pr-gate"}
        | {job for lane, job in LANE_CI_JOBS.items() if lane in plan.lanes}
    )
    print("ci.yml jobs that run: " + ", ".join(jobs))
    print("Reasons:")
    for reason in plan.reasons:
        print(f"  {reason}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)

    def run_arguments(command: argparse.ArgumentParser) -> None:
        command.add_argument("--event-name", required=True)
        command.add_argument("--event-path", type=Path, default=None)
        command.add_argument("--checked-out", required=True)
        command.add_argument("--output", required=True, type=Path)
        command.add_argument("--github-output", type=Path, default=None)
        command.add_argument("--step-summary", type=Path, default=None)

    compute = commands.add_parser("compute", help="plan the checked-out merge")
    run_arguments(compute)
    effective = commands.add_parser(
        "effective", help="union the recomputed plan with the upstream plan"
    )
    run_arguments(effective)
    effective.add_argument("--upstream-json", required=True)
    effective.add_argument("--changes-result", required=True)
    explain = commands.add_parser("explain", help="plan a local branch")
    explain.add_argument("--base", required=True)
    explain.add_argument("--head", default="HEAD")
    explain.add_argument("--label", action="append", default=[])
    commands.add_parser("render-table", help="print the markdown lane table")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "compute":
        return _compute(arguments)
    if arguments.command == "effective":
        return _effective(arguments)
    if arguments.command == "explain":
        return _explain(arguments)
    sys.stdout.write(render_table())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
