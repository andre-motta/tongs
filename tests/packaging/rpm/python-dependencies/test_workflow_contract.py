from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).parents[4]
PACKAGING = ROOT / "packaging" / "rpm" / "python-dependencies"
WORKFLOW = ROOT / ".github" / "workflows" / "desktop-python-rpms.yml"


def test_every_companion_has_one_reviewable_spec() -> None:
    manifest = json.loads((PACKAGING / "manifest.json").read_text())
    expected = {f"python-{name}.spec" for name in manifest["build_order"]}
    actual = {path.name for path in (PACKAGING / "specs").glob("*.spec")}

    assert actual == expected


def test_hosted_harness_separates_networked_preparation_and_offline_build() -> None:
    harness = (PACKAGING / "run_hosted.sh").read_text()
    installer = (PACKAGING / "install_and_verify.sh").read_text()

    assert "RUNNER_ENVIRONMENT:-} == github-hosted" in harness
    assert "--network=none" in harness
    assert harness.index("prepare_sources.py") < harness.index("--network=none")
    assert "dnf install" in installer
    assert "pip install" not in installer
    assert "! -name ALL-SHA256SUMS" in harness


def test_rust_spec_uses_locked_sources_and_fedora_openssl() -> None:
    spec = (PACKAGING / "specs" / "python-rfc3161-client.spec").read_text()

    assert "rfc3161-client-%{version}-cargo-vendor.tar.gz" in spec
    assert "OPENSSL_NO_VENDOR=1" in spec
    assert 'openssl = "0.10.80"' in spec
    assert "CARGO_NET_OFFLINE=true" in spec


def test_builder_uses_only_fedora_packaged_python_build_tools() -> None:
    containerfile = (PACKAGING / "Containerfile").read_text()

    assert "python3-pip" in containerfile
    assert "curl" not in containerfile
    assert "rustup" not in containerfile


def test_workflow_has_minimal_permissions_and_retains_artifacts() -> None:
    workflow = WORKFLOW.read_text()

    assert "permissions:\n  contents: read" in workflow
    assert "ref: ${{ env.TONGS_HEAD_SHA }}" in workflow
    assert "persist-credentials: false" in workflow
    assert "actions/upload-artifact@v7" in workflow
    assert "secrets" not in workflow
