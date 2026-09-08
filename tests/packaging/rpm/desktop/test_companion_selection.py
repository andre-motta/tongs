from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).parents[4]
PACKAGING = ROOT / "packaging" / "rpm" / "desktop"
DEPENDENCY_MANIFEST = (
    ROOT / "packaging" / "rpm" / "python-dependencies" / "manifest.json"
)


def _load_script() -> ModuleType:
    path = PACKAGING / "select_companion_rpms.py"
    spec = importlib.util.spec_from_file_location("tongs_select_companions", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


selector = _load_script()


def _fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, dict[Path, str]]:
    manifest = json.loads(DEPENDENCY_MANIFEST.read_text())
    rpm_dir = tmp_path / "rpms"
    output_dir = tmp_path / "selected"
    rpm_dir.mkdir()
    output_dir.mkdir()
    identities = selector.expected_identities(manifest)
    by_path = {}
    for index, identity in enumerate(identities):
        path = rpm_dir / f"companion-{index}.rpm"
        path.write_bytes(f"companion {index}".encode())
        by_path[path] = identity
    for name in (
        "python3-rfc3161-client-debuginfo",
        "python3-rfc3161-client-debugsource",
    ):
        path = rpm_dir / f"{name}.rpm"
        path.write_bytes(name.encode())
        by_path[path] = f"{name}|0|1.0.8|1.fc44|x86_64"
    monkeypatch.setattr(selector, "_rpm_identity", by_path.__getitem__)
    return rpm_dir, output_dir, by_path


def test_selects_only_exact_manifest_companions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rpm_dir, output_dir, _ = _fixture(tmp_path, monkeypatch)
    report = tmp_path / "selection.json"

    selector.select(DEPENDENCY_MANIFEST, rpm_dir, output_dir, report)

    selected = sorted(output_dir.glob("*.rpm"))
    assert len(selected) == 7
    assert len((output_dir / "expected-packages.tsv").read_text().splitlines()) == 7
    document = json.loads(report.read_text())
    assert len(document["selected"]) == 7
    assert len(document["excluded_debug_packages"]) == 2


def test_rejects_an_unexpected_consumer_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rpm_dir, output_dir, by_path = _fixture(tmp_path, monkeypatch)
    extra = rpm_dir / "python3-unreviewed.rpm"
    extra.write_bytes(b"unreviewed")

    def identity(path: Path) -> str:
        if path == extra:
            return "python3-unreviewed|0|1|1.fc44|noarch"
        return by_path[path]

    monkeypatch.setattr(selector, "_rpm_identity", identity)

    with pytest.raises(ValueError, match="unexpected"):
        selector.select(
            DEPENDENCY_MANIFEST, rpm_dir, output_dir, tmp_path / "report.json"
        )


def test_rejects_a_missing_exact_companion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rpm_dir, output_dir, _ = _fixture(tmp_path, monkeypatch)
    (rpm_dir / "companion-0.rpm").unlink()

    with pytest.raises(ValueError, match="missing"):
        selector.select(
            DEPENDENCY_MANIFEST, rpm_dir, output_dir, tmp_path / "report.json"
        )


def test_rejects_a_duplicate_exact_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rpm_dir, output_dir, by_path = _fixture(tmp_path, monkeypatch)
    duplicate = rpm_dir / "duplicate.rpm"
    duplicate.write_bytes(b"duplicate")
    by_path[duplicate] = by_path[rpm_dir / "companion-0.rpm"]

    with pytest.raises(ValueError, match="duplicate"):
        selector.select(
            DEPENDENCY_MANIFEST, rpm_dir, output_dir, tmp_path / "report.json"
        )


def test_rejects_a_nonempty_consumer_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rpm_dir, output_dir, _ = _fixture(tmp_path, monkeypatch)
    (output_dir / "stale.rpm").write_bytes(b"stale")

    with pytest.raises(ValueError, match="not empty"):
        selector.select(
            DEPENDENCY_MANIFEST, rpm_dir, output_dir, tmp_path / "report.json"
        )
