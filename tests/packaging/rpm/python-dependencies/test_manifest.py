from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

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


def test_manifest_uses_only_hash_pinned_pypi_sources() -> None:
    manifest = json.loads(MANIFEST.read_text())

    for companion in manifest["companions"]:
        source = companion["source"]
        assert source["url"].startswith("https://files.pythonhosted.org/")
        assert len(source["sha256"]) == 64
        assert source["bytes"] > 0
        assert companion["license"]


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


def test_manifest_requires_system_python_and_fedora_providers() -> None:
    manifest = json.loads(MANIFEST.read_text())
    requirements = {item["requirement"] for item in manifest["system_requirements"]}

    assert "python(abi) >= 3.12" in requirements
    assert "python3dist(cryptography) >= 43" in requirements
    assert "python3dist(email-validator) >= 2" in requirements
    assert "python3dist(id) >= 1.1" in requirements
    assert not any("sigstore" in requirement for requirement in requirements)


def test_rekor_distribution_records_its_actual_import_package() -> None:
    manifest = json.loads(MANIFEST.read_text())
    rekor = next(
        item
        for item in manifest["companions"]
        if item["distribution"] == "sigstore-rekor-types"
    )

    assert rekor["module"] == "rekor_types"


def test_rust_license_sources_are_pinned_to_locked_git_commit() -> None:
    manifest = json.loads(MANIFEST.read_text())
    rust = next(
        item
        for item in manifest["companions"]
        if item["distribution"] == "rfc3161-client"
    )

    commit = rust["cargo"]["git_dependency"].rsplit("#", 1)[1]
    for source in rust["cargo"]["license_sources"]:
        assert f"/{commit}/LICENSE" in source["url"]
        assert len(source["sha256"]) == 64
        assert source["bytes"] > 0


def test_nested_and_linked_licenses_are_declared() -> None:
    manifest = json.loads(MANIFEST.read_text())
    licenses = {
        item["distribution"]: item["license"] for item in manifest["companions"]
    }

    assert licenses["securesystemslib"] == "MIT AND CC0-1.0"
    assert "BSD-3-Clause" in licenses["rfc3161-client"]
    assert "Unicode-3.0" in licenses["rfc3161-client"]


def test_manifest_rejects_unapproved_source_host() -> None:
    manifest = json.loads(MANIFEST.read_text())
    manifest["companions"][0]["source"]["url"] = "https://example.com/source.tar.gz"

    with pytest.raises(ValueError, match="unapproved source host"):
        validate_manifest.validate(manifest)
