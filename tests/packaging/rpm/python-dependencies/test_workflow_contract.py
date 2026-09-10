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
    assert "dnf-python-bootstrap.log" in installer
    assert "! -name '*-debuginfo-*'" in installer


def test_clean_install_retains_container_package_capabilities() -> None:
    harness = (PACKAGING / "run_hosted.sh").read_text()
    install_invocation = harness[harness.index('"$script_dir/rebuild_srpms.sh"') :]

    assert '"$base_image"' in install_invocation
    assert "--cap-drop=all" not in install_invocation
    assert "--security-opt=no-new-privileges" in install_invocation
    assert "! -name ALL-SHA256SUMS" in harness


def test_rust_spec_uses_locked_sources_and_fedora_openssl() -> None:
    spec = (PACKAGING / "specs" / "python-rfc3161-client.spec").read_text()

    assert "rfc3161-client-%{version}-cargo-vendor.tar.gz" in spec
    assert "OPENSSL_NO_VENDOR=1" in spec
    assert 'openssl = "0.10.80"' in spec
    assert "cp Cargo.system-openssl.lock Cargo.lock" in spec
    assert "CARGO_NET_OFFLINE=true" in spec
    assert "rfc3161-cargo-inventory.json" in spec
    assert "cargo-licenses.tar.gz" in spec
    assert "%license %{_licensedir}/python3-rfc3161-client" in spec


def test_rekor_spec_maps_python_extra_to_fedora_providers() -> None:
    spec = (PACKAGING / "specs" / "python-sigstore-rekor-types.spec").read_text()

    assert '"pydantic >=2,<3", "email-validator >=2"' in spec
    assert "%pyproject_save_files -l rekor_types" in spec


def test_builder_uses_only_fedora_packaged_python_build_tools() -> None:
    containerfile = (PACKAGING / "Containerfile").read_text()

    assert "python3-pip" in containerfile
    assert "curl" not in containerfile
    assert "rustup" not in containerfile


def test_specs_declare_python_rpm_macro_and_frontend_build_requirements() -> None:
    for spec_path in (PACKAGING / "specs").glob("*.spec"):
        spec = spec_path.read_text()
        assert "BuildRequires:  pyproject-rpm-macros" in spec
        assert "BuildRequires:  python3-pip" in spec


def test_securesystemslib_marks_nested_vendor_license() -> None:
    spec = (PACKAGING / "specs" / "python-securesystemslib.spec").read_text()

    assert "License:        MIT AND CC0-1.0" in spec
    assert (
        "%license %{python3_sitelib}/securesystemslib/_vendor/ed25519/LICENSE" in spec
    )


def test_clean_install_verifies_license_file_flags_and_inventory() -> None:
    installer = (PACKAGING / "install_and_verify.sh").read_text()
    verifier = (PACKAGING / "verify_install.py").read_text()

    assert "license-file-flags.txt" in installer
    assert "cargo-inventory.json" in verifier
    assert "upstream_cargo_lock_sha256" in verifier
    assert "resolved Cargo package lacks bundled license" in verifier
    assert "vendored OpenSSL appears" in verifier
    assert "%{=NAME}|%{FILENAMES}|%{FILEFLAGS:fflags}" in installer


def test_offline_build_keeps_tool_caches_in_disposable_topdir() -> None:
    builder = (PACKAGING / "rebuild_one_srpm.sh").read_text()

    assert 'export XDG_CACHE_HOME="$topdir/cache"' in builder
    assert 'export CARGO_HOME="$topdir/cargo-home"' in builder
    assert "export HOME=" not in builder


def test_each_srpm_gets_clean_builddep_environment_and_offline_rebuild() -> None:
    srpm_builder = (PACKAGING / "build_rpms.sh").read_text()
    rebuilder = (PACKAGING / "rebuild_srpms.sh").read_text()

    assert 'cp -- "$source_dir"/rfc3161-cargo-inventory.json' in srpm_builder
    assert "resolve_srpms.py" in rebuilder
    assert '-name "python-${distribution}-*.src.rpm"' not in rebuilder
    assert "%{NAME}|%{VERSION}|%{RELEASE}|%{ARCH}" in rebuilder
    assert "dnf builddep" in rebuilder
    assert "--network=none" in rebuilder
    assert rebuilder.index("dnf builddep") < rebuilder.rindex("--network=none")


def test_workflow_has_minimal_permissions_and_retains_artifacts() -> None:
    workflow = WORKFLOW.read_text()

    assert "permissions:\n  contents: read" in workflow
    assert "ref: ${{ env.TONGS_HEAD_SHA }}" in workflow
    assert "persist-credentials: false" in workflow
    assert (
        "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1"
        in workflow
    )
    assert "secrets" not in workflow
