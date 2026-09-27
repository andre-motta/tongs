from __future__ import annotations

import importlib.util
import json
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).parents[4]
PACKAGING = ROOT / "packaging" / "rpm" / "python-dependencies"
MANIFEST = PACKAGING / "manifest.json"


def _load_script(name: str) -> ModuleType:
    path = PACKAGING / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"tongs_rpm_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


validate_manifest = _load_script("validate_manifest")


def test_manifest_is_valid_and_build_order_is_explicit() -> None:
    manifest = json.loads(MANIFEST.read_text())

    validate_manifest.validate(manifest)

    assert manifest["target"]["python_minimum"] == "3.12"
    assert manifest["build_order"][-1] == "sigstore"
    assert len(manifest["companions"]) == 7
    binary_identities = {
        (
            item["binary_rpm"]["name"],
            item["version"],
            item["binary_rpm"]["release"],
            item["binary_rpm"]["architecture"],
        )
        for item in manifest["companions"]
    }
    assert len(binary_identities) == 7
    assert all(item["binary_rpm"]["epoch"] == 0 for item in manifest["companions"])


def test_manifest_binary_identities_match_reviewed_specs() -> None:
    manifest = json.loads(MANIFEST.read_text())

    for companion in manifest["companions"]:
        spec = (
            PACKAGING / "specs" / f"python-{companion['distribution']}.spec"
        ).read_text()
        binary = companion["binary_rpm"]
        assert f"Name:           {companion['rpm_source_name']}" in spec
        assert f"Version:        {companion['version']}" in spec
        assert f"%package -n {binary['name']}" in spec
        assert "Release:        1%{?dist}" in spec
        if binary["architecture"] == "noarch":
            assert "BuildArch:      noarch" in spec
        else:
            assert "BuildArch:      noarch" not in spec


def _rust(manifest: dict[str, Any]) -> dict[str, Any]:
    return next(
        item
        for item in manifest["companions"]
        if item["distribution"] == "rfc3161-client"
    )


def _other_commit_license(manifest: dict[str, Any]) -> None:
    source = _rust(manifest)["cargo"]["license_sources"][0]
    commit = _rust(manifest)["cargo"]["git_dependency"].rsplit("#", 1)[1]
    source["url"] = source["url"].replace(commit, "0" * len(commit))


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda manifest: manifest["companions"][0]["source"].update(
                url="https://example.com/source.tar.gz"
            ),
            "unapproved source host",
        ),
        (
            lambda manifest: manifest["companions"][0]["source"].update(
                url=manifest["companions"][0]["source"]["url"].replace(
                    "https://", "http://", 1
                )
            ),
            "unapproved source host",
        ),
        (
            lambda manifest: manifest["companions"][0]["source"].update(
                sha256="a" * 63
            ),
            "invalid source hash",
        ),
        (
            lambda manifest: manifest["companions"][0]["source"].update(
                sha256="g" * 64
            ),
            "invalid source hash",
        ),
        (
            lambda manifest: manifest["companions"][0]["source"].update(bytes=0),
            "invalid source size",
        ),
        (_other_commit_license, "unapproved Cargo license source"),
    ],
    ids=["host", "scheme", "short-hash", "non-hex-hash", "size", "license-commit"],
)
def test_manifest_rejects_unpinned_sources(
    mutate: Callable[[dict[str, Any]], None], message: str
) -> None:
    manifest = json.loads(MANIFEST.read_text())
    mutate(manifest)

    with pytest.raises(ValueError, match=message):
        validate_manifest.validate(manifest)
