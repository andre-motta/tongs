"""Verify bounded pytest JUnit and Node test-runner TAP outcome reports."""

from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

MAX_REPORT_BYTES = 8 * 1024 * 1024
MAX_RECORDS = 50_000
MAX_SUITES = 1_024
MAX_NESTING = 32
MAX_LINES = 100_000
MAX_LINE_BYTES = 256 * 1024

_XML_DECLARATION = re.compile(
    r"\A\s*<\?xml\s+[^?]*?encoding\s*=\s*(['\"])([^'\"]+)\1[^?]*\?>",
    re.IGNORECASE,
)
_XML_FORBIDDEN_DECLARATION = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_NONNEGATIVE_INTEGER = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_TAP_ASSERTION = re.compile(
    r"(?P<status>ok|not ok) (?P<identifier>[0-9]+) - "
    r"(?P<name>.*?)(?: # (?P<directive>SKIP|TODO)(?: .*)?)?\Z"
)
_TAP_PLAN = re.compile(r"1\.\.(?P<count>[0-9]+)\Z")
_TAP_TYPE = re.compile(r"type: ['\"](?P<kind>test|suite)['\"]\Z")
_TAP_DURATION = re.compile(r"[0-9]+(?:\.[0-9]+)?\Z")
_TAP_DIAGNOSTIC_DURATION = re.compile(
    r"duration_ms: (?P<duration>[0-9]+(?:\.[0-9]+)?)\Z"
)


class ReportValidationError(ValueError):
    """Raised when a test report is unsupported, unsafe, or unsuccessful."""


@dataclass(frozen=True)
class PytestJUnitCounts:
    """Observed pytest JUnit outcomes."""

    tests: int
    suites: int
    failures: int
    errors: int
    skipped: int


@dataclass(frozen=True)
class NodeTapCounts:
    """Observed Node test-runner TAP outcomes."""

    tests: int
    suites: int
    passed: int
    failed: int
    cancelled: int
    skipped: int
    todo: int


@dataclass(frozen=True)
class _TapStreamCounts:
    tests: int = 0
    suites: int = 0

    def add(self, other: _TapStreamCounts) -> _TapStreamCounts:
        return _TapStreamCounts(
            tests=self.tests + other.tests,
            suites=self.suites + other.suites,
        )


def _decode_report(report_bytes: bytes, report_name: str) -> str:
    if not isinstance(report_bytes, bytes):
        raise ReportValidationError(f"{report_name} report must be bytes")
    if not report_bytes:
        raise ReportValidationError(f"{report_name} report is empty")
    if len(report_bytes) > MAX_REPORT_BYTES:
        raise ReportValidationError(
            f"{report_name} report exceeds the {MAX_REPORT_BYTES}-byte limit"
        )
    try:
        return report_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ReportValidationError(f"{report_name} report is not UTF-8") from error


def _xml_counter(suite: ET.Element, name: str) -> int:
    raw_value = suite.attrib.get(name)
    if raw_value is None:
        raise ReportValidationError(f"pytest JUnit suite is missing {name!r} counter")
    if len(raw_value) > 10 or _NONNEGATIVE_INTEGER.fullmatch(raw_value) is None:
        raise ReportValidationError(
            f"pytest JUnit suite has malformed {name!r} counter"
        )
    value = int(raw_value)
    if value > MAX_RECORDS:
        raise ReportValidationError(
            f"pytest JUnit suite {name!r} counter exceeds the record limit"
        )
    return value


def _tap_integer(raw_value: str, label: str, *, allow_zero: bool) -> int:
    if len(raw_value) > 10 or _NONNEGATIVE_INTEGER.fullmatch(raw_value) is None:
        raise ReportValidationError(f"Node TAP {label} is malformed")
    value = int(raw_value)
    if not allow_zero and value == 0:
        raise ReportValidationError(f"Node TAP {label} must be positive")
    if value > MAX_RECORDS:
        raise ReportValidationError(f"Node TAP {label} exceeds the record limit")
    return value


def _validate_leaf_element(element: ET.Element, label: str) -> None:
    if list(element):
        raise ReportValidationError(f"pytest JUnit has unsupported nested {label}")


def _validate_properties(properties: ET.Element) -> None:
    for child in properties:
        if child.tag != "property":
            raise ReportValidationError(
                "pytest JUnit properties contain an unsupported element"
            )
        _validate_leaf_element(child, "property element")


def _validate_testcase(
    testcase: ET.Element,
    identities: set[tuple[str, str]],
    expected_classname_prefix: str | None,
) -> tuple[int, int, int]:
    classname = testcase.attrib.get("classname", "")
    name = testcase.attrib.get("name", "")
    if not classname or not name:
        raise ReportValidationError(
            "pytest JUnit testcase is missing classname or name identity"
        )
    identity = (classname, name)
    if identity in identities:
        raise ReportValidationError(
            f"pytest JUnit has duplicate testcase identity {classname!r}::{name!r}"
        )
    identities.add(identity)
    if expected_classname_prefix is not None and not classname.startswith(
        expected_classname_prefix
    ):
        raise ReportValidationError(
            f"pytest JUnit testcase {classname!r} is outside expected prefix "
            f"{expected_classname_prefix!r}"
        )

    outcomes = {"failure": 0, "error": 0, "skipped": 0}
    singleton_metadata: set[str] = set()
    for child in testcase:
        if child.tag in outcomes:
            outcomes[child.tag] += 1
            _validate_leaf_element(child, f"{child.tag} outcome")
        elif child.tag == "properties":
            if child.tag in singleton_metadata:
                raise ReportValidationError(
                    "pytest JUnit testcase repeats properties metadata"
                )
            singleton_metadata.add(child.tag)
            _validate_properties(child)
        elif child.tag in {"system-out", "system-err"}:
            if child.tag in singleton_metadata:
                raise ReportValidationError(
                    f"pytest JUnit testcase repeats {child.tag} metadata"
                )
            singleton_metadata.add(child.tag)
            _validate_leaf_element(child, child.tag)
        else:
            raise ReportValidationError(
                f"pytest JUnit testcase contains unsupported {child.tag!r} element"
            )
    if sum(outcomes.values()) > 1:
        raise ReportValidationError(
            "pytest JUnit testcase has ambiguous multiple outcome elements"
        )
    return outcomes["failure"], outcomes["error"], outcomes["skipped"]


def _parse_junit_tree(report_bytes: bytes) -> ET.Element:
    depth = 0
    elements = 0
    try:
        events = ET.iterparse(io.BytesIO(report_bytes), events=("start", "end"))
        for event, _element in events:
            if event == "start":
                depth += 1
                elements += 1
                if depth > MAX_NESTING:
                    raise ReportValidationError(
                        "pytest JUnit exceeds the XML nesting limit"
                    )
                if elements > MAX_RECORDS * 4:
                    raise ReportValidationError(
                        "pytest JUnit exceeds the XML element limit"
                    )
            else:
                depth -= 1
        root = events.root
    except ET.ParseError as error:
        raise ReportValidationError("pytest JUnit report is malformed XML") from error
    if depth != 0:
        raise ReportValidationError("pytest JUnit report has incomplete XML nesting")
    return root


def verify_pytest_junit(
    report_bytes: bytes, *, expected_classname_prefix: str | None = None
) -> PytestJUnitCounts:
    """Verify a bounded pytest JUnit report and return observed passing counts."""
    text = _decode_report(report_bytes, "pytest JUnit")
    if expected_classname_prefix is not None and (
        not isinstance(expected_classname_prefix, str) or not expected_classname_prefix
    ):
        raise ReportValidationError(
            "expected pytest classname prefix must be a nonempty string"
        )
    if _XML_FORBIDDEN_DECLARATION.search(text):
        raise ReportValidationError(
            "pytest JUnit DTD and entity declarations are forbidden"
        )
    declaration = _XML_DECLARATION.match(text)
    if (
        declaration is not None
        and declaration.group(2).lower().replace("-", "") != "utf8"
    ):
        raise ReportValidationError("pytest JUnit XML declaration must specify UTF-8")

    root = _parse_junit_tree(report_bytes)
    if root.tag == "testsuite":
        suites = [root]
    elif root.tag == "testsuites":
        suites = list(root)
        if any(suite.tag != "testsuite" for suite in suites):
            raise ReportValidationError(
                "pytest JUnit testsuites root contains an unsupported element"
            )
    else:
        raise ReportValidationError("pytest JUnit root must be testsuite or testsuites")
    if not suites:
        raise ReportValidationError("pytest JUnit contains no test suites")
    if len(suites) > MAX_SUITES:
        raise ReportValidationError("pytest JUnit exceeds the suite limit")

    identities: set[tuple[str, str]] = set()
    declared = {name: 0 for name in ("tests", "failures", "errors", "skipped")}
    observed = {name: 0 for name in declared}
    for suite in suites:
        suite_declared = {name: _xml_counter(suite, name) for name in declared}
        testcase_count = 0
        suite_outcomes = {name: 0 for name in ("failures", "errors", "skipped")}
        singleton_metadata: set[str] = set()
        for child in suite:
            if child.tag == "testcase":
                testcase_count += 1
                if len(identities) >= MAX_RECORDS:
                    raise ReportValidationError(
                        "pytest JUnit exceeds the testcase record limit"
                    )
                failure, error, skipped = _validate_testcase(
                    child, identities, expected_classname_prefix
                )
                suite_outcomes["failures"] += failure
                suite_outcomes["errors"] += error
                suite_outcomes["skipped"] += skipped
            elif child.tag == "properties":
                if child.tag in singleton_metadata:
                    raise ReportValidationError(
                        "pytest JUnit suite repeats properties metadata"
                    )
                singleton_metadata.add(child.tag)
                _validate_properties(child)
            elif child.tag in {"system-out", "system-err"}:
                if child.tag in singleton_metadata:
                    raise ReportValidationError(
                        f"pytest JUnit suite repeats {child.tag} metadata"
                    )
                singleton_metadata.add(child.tag)
                _validate_leaf_element(child, child.tag)
            else:
                raise ReportValidationError(
                    f"pytest JUnit suite contains unsupported {child.tag!r} element"
                )
        suite_observed = {"tests": testcase_count, **suite_outcomes}
        if suite_declared != suite_observed:
            raise ReportValidationError(
                "pytest JUnit counters do not match testcase outcomes: "
                f"declared={suite_declared}, observed={suite_observed}"
            )
        for name in declared:
            declared[name] += suite_declared[name]
            observed[name] += suite_observed[name]

    if root.tag == "testsuites":
        root_counter_names = [name for name in declared if name in root.attrib]
        if root_counter_names and len(root_counter_names) != len(declared):
            raise ReportValidationError(
                "pytest JUnit testsuites root has an incomplete counter set"
            )
        if root_counter_names:
            root_declared = {name: _xml_counter(root, name) for name in declared}
            if root_declared != observed:
                raise ReportValidationError(
                    "pytest JUnit root counters do not match testcase outcomes"
                )

    if observed["tests"] < 1:
        raise ReportValidationError("pytest JUnit contains no tests")
    nonpassing = {name: observed[name] for name in ("failures", "errors", "skipped")}
    if any(nonpassing.values()):
        raise ReportValidationError(
            f"pytest JUnit contains non-passing outcomes: {nonpassing}"
        )
    return PytestJUnitCounts(
        tests=observed["tests"],
        suites=len(suites),
        failures=observed["failures"],
        errors=observed["errors"],
        skipped=observed["skipped"],
    )


class _TapParser:
    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self.index = 1
        self.records = 0

    def _current(self) -> str | None:
        if self.index >= len(self.lines):
            return None
        return self.lines[self.index]

    def _diagnostic_kind(self, indent: str) -> str:
        opening = f"{indent}  ---"
        closing = f"{indent}  ..."
        if self._current() != opening:
            raise ReportValidationError(
                "Node TAP assertion is missing its emitted diagnostic block"
            )
        self.index += 1
        kinds: list[str] = []
        durations: list[str] = []
        field_indent = f"{indent}  "
        while True:
            line = self._current()
            if line is None:
                raise ReportValidationError("Node TAP diagnostic block is truncated")
            if line == closing:
                self.index += 1
                break
            if line.startswith(field_indent):
                match = _TAP_TYPE.fullmatch(line[len(field_indent) :])
                if match is not None:
                    kinds.append(match.group("kind"))
                duration_match = _TAP_DIAGNOSTIC_DURATION.fullmatch(
                    line[len(field_indent) :]
                )
                if duration_match is not None:
                    durations.append(duration_match.group("duration"))
            self.index += 1
        if len(kinds) != 1:
            raise ReportValidationError(
                "Node TAP diagnostic block must declare exactly one test type"
            )
        if len(durations) != 1:
            raise ReportValidationError(
                "Node TAP diagnostic block must declare exactly one duration"
            )
        return kinds[0]

    def _stream(self, indent: str, depth: int) -> _TapStreamCounts:
        if depth > MAX_NESTING:
            raise ReportValidationError("Node TAP exceeds the suite nesting limit")
        expected_identifier = 1
        counts = _TapStreamCounts()
        while True:
            line = self._current()
            if line is None:
                raise ReportValidationError(
                    "Node TAP stream is truncated before its plan"
                )
            if line.lower().startswith(f"{indent}bail out!"):
                raise ReportValidationError("Node TAP contains a bailout")
            plan = (
                _TAP_PLAN.fullmatch(line[len(indent) :])
                if line.startswith(indent)
                else None
            )
            if plan is not None:
                planned = _tap_integer(
                    plan.group("count"), "plan count", allow_zero=True
                )
                if planned != expected_identifier - 1:
                    raise ReportValidationError(
                        "Node TAP plan does not match its assertion records"
                    )
                self.index += 1
                return counts

            prefix = f"{indent}# Subtest: "
            if not line.startswith(prefix):
                raise ReportValidationError(
                    f"Node TAP has unsupported content at line {self.index + 1}"
                )
            subtest_name = line[len(prefix) :]
            if not subtest_name:
                raise ReportValidationError("Node TAP contains an unnamed subtest")
            self.index += 1

            child_counts: _TapStreamCounts | None = None
            next_line = self._current()
            if next_line is not None and next_line.startswith(f"{indent}    "):
                child_counts = self._stream(f"{indent}    ", depth + 1)

            assertion_line = self._current()
            if assertion_line is None or not assertion_line.startswith(indent):
                raise ReportValidationError(
                    "Node TAP subtest is missing its assertion record"
                )
            assertion = _TAP_ASSERTION.fullmatch(assertion_line[len(indent) :])
            if assertion is None:
                raise ReportValidationError(
                    "Node TAP subtest has a malformed assertion record"
                )
            identifier = _tap_integer(
                assertion.group("identifier"),
                "assertion identifier",
                allow_zero=False,
            )
            if identifier != expected_identifier:
                raise ReportValidationError(
                    "Node TAP assertion identifiers are duplicate or out of order"
                )
            expected_identifier += 1
            if assertion.group("name") != subtest_name:
                raise ReportValidationError(
                    "Node TAP subtest and assertion names do not match"
                )
            if assertion.group("status") != "ok":
                raise ReportValidationError("Node TAP contains a failing assertion")
            directive = assertion.group("directive")
            if directive == "SKIP":
                raise ReportValidationError("Node TAP contains a skipped test")
            if directive == "TODO":
                raise ReportValidationError("Node TAP contains a TODO test")

            self.index += 1
            kind = self._diagnostic_kind(indent)
            if child_counts is None:
                if kind != "test":
                    raise ReportValidationError(
                        "Node TAP leaf subtest is not declared as a test"
                    )
                counts = counts.add(_TapStreamCounts(tests=1))
            else:
                if kind != "suite":
                    raise ReportValidationError(
                        "Node TAP nested stream is not declared as a suite"
                    )
                counts = counts.add(child_counts).add(_TapStreamCounts(suites=1))
            self.records += 1
            if self.records > MAX_RECORDS:
                raise ReportValidationError("Node TAP exceeds the record limit")

    def parse(self) -> _TapStreamCounts:
        counts = self._stream("", 0)
        summary_names = (
            "tests",
            "suites",
            "pass",
            "fail",
            "cancelled",
            "skipped",
            "todo",
        )
        summary: dict[str, int] = {}
        for name in summary_names:
            line = self._current()
            prefix = f"# {name} "
            if line is None or not line.startswith(prefix):
                raise ReportValidationError(
                    f"Node TAP is missing its final {name!r} counter"
                )
            raw_value = line[len(prefix) :]
            summary[name] = _tap_integer(
                raw_value, f"final {name!r} counter", allow_zero=True
            )
            self.index += 1

        duration_line = self._current()
        duration_prefix = "# duration_ms "
        if duration_line is None or not duration_line.startswith(duration_prefix):
            raise ReportValidationError("Node TAP is missing its final duration")
        raw_duration = duration_line[len(duration_prefix) :]
        if _TAP_DURATION.fullmatch(raw_duration) is None:
            raise ReportValidationError("Node TAP final duration is malformed")
        try:
            duration = Decimal(raw_duration)
        except InvalidOperation as error:
            raise ReportValidationError(
                "Node TAP final duration is malformed"
            ) from error
        if not duration.is_finite() or duration < 0:
            raise ReportValidationError("Node TAP final duration is invalid")
        self.index += 1
        if self.index != len(self.lines):
            raise ReportValidationError(
                "Node TAP contains content after its final summary"
            )

        expected = {
            "tests": counts.tests,
            "suites": counts.suites,
            "pass": counts.tests,
            "fail": 0,
            "cancelled": 0,
            "skipped": 0,
            "todo": 0,
        }
        if summary != expected:
            raise ReportValidationError(
                f"Node TAP final counters disagree with records: "
                f"declared={summary}, observed={expected}"
            )
        if counts.tests < 1:
            raise ReportValidationError("Node TAP contains no tests")
        return counts


def verify_node_tap(report_bytes: bytes) -> NodeTapCounts:
    """Verify a bounded Node test-runner TAP report and return passing counts."""
    text = _decode_report(report_bytes, "Node TAP")
    if not text.endswith("\n"):
        raise ReportValidationError(
            "Node TAP report is truncated without a final newline"
        )
    raw_lines = text[:-1].split("\n")
    if len(raw_lines) > MAX_LINES:
        raise ReportValidationError("Node TAP exceeds the line limit")
    if any(len(line.encode("utf-8")) > MAX_LINE_BYTES for line in raw_lines):
        raise ReportValidationError("Node TAP contains an overlong line")
    if not raw_lines or raw_lines[0] != "TAP version 13":
        raise ReportValidationError("Node TAP must start with 'TAP version 13'")
    counts = _TapParser(raw_lines).parse()
    return NodeTapCounts(
        tests=counts.tests,
        suites=counts.suites,
        passed=counts.tests,
        failed=0,
        cancelled=0,
        skipped=0,
        todo=0,
    )
