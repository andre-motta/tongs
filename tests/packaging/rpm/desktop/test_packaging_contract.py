from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).parents[4]
PACKAGING = ROOT / "packaging" / "rpm" / "desktop"
WORKFLOW = ROOT / ".github" / "workflows" / "desktop-rpm.yml"


def test_two_srpms_and_optional_mcp_ownership_are_explicit() -> None:
    core = (PACKAGING / "templates" / "python-tongs.spec.in").read_text()
    desktop = (PACKAGING / "templates" / "tongs-desktop.spec.in").read_text()

    assert "%package -n python3-tongs+mcp" in core
    assert "%{_bindir}/tongs-mcp" in core
    assert "Requires:       python3-tongs = %{version}-%{release}" in core
    assert "python3dist(mcp[cli])" in core
    assert "%license %{_licensedir}/python3-tongs/LICENSE" in core
    assert "mcp[cli]" not in core.split("%package -n python3-tongs+mcp", 1)[0]
    exact_core = (
        "Requires:       python3-tongs = @CORE_RPM_VERSION@-@RPM_RELEASE@%{?dist}"
    )
    assert exact_core in desktop
    assert "SETUPTOOLS_SCM_PRETEND_VERSION=@CORE_PEP440_VERSION@" in core
    assert "SETUPTOOLS_SCM_PRETEND_VERSION_FOR_TONGS=@CORE_PEP440_VERSION@" in core


def test_desktop_retains_runtime_and_has_no_scriptlets() -> None:
    spec = (PACKAGING / "templates" / "tongs-desktop.spec.in").read_text()

    assert "cp -a payload/runtime/." in spec
    assert "BuildRequires:  diffutils" in spec
    assert "%{_libexecdir}/tongs-desktop" in spec
    assert "chrome-sandbox" in spec and "test ! -u" in spec
    assert "cmp %{SOURCE11} %{buildroot}%{_bindir}/tongs-desktop" in spec
    for scriptlet in ("%pre\n", "%post\n", "%preun\n", "%postun\n"):
        assert scriptlet not in spec


def test_system_launcher_uses_fixed_system_contract() -> None:
    launcher = (PACKAGING / "templates" / "tongs-desktop.in").read_text()

    assert launcher.startswith("#!/usr/bin/sh\n")
    assert 'if [ "$#" -ne 0 ]' in launcher
    assert "--ozone-platform=x11" in launcher
    assert "--tongs-python-executable /usr/bin/python3" in launcher
    assert "--tongs-core-version @CORE_PEP440_VERSION@" in launcher
    assert "--tongs-safe-cwd /usr/libexec/tongs-desktop" in launcher


def test_hosted_harness_is_disposable_and_rebuilds_offline() -> None:
    harness = (PACKAGING / "run_hosted.sh").read_text()
    rebuilder = (PACKAGING / "rebuild_srpms.sh").read_text()
    installer = (PACKAGING / "install_and_verify.sh").read_text()

    assert "RUNNER_ENVIRONMENT:-} == github-hosted" in harness
    assert "gh run download" in harness
    assert "--network=none" in harness
    assert "dnf builddep" in rebuilder
    assert rebuilder.index("dnf builddep") < rebuilder.index("--network=none")
    assert "pip install" not in installer
    assert "dnf-failed-upgrade.log" in installer
    assert "dnf-corrupt-upgrade.log" in installer
    assert "dnf-reinstall.log" in installer
    assert "dnf-uninstall.log" in installer
    assert "verify_sidecar_plugin.py" in installer
    assert "verify_mcp_command.py" in installer
    assert "python-dependencies/verify_install.py" in installer
    assert "rpm -V" in (PACKAGING / "verify_rpm_state.py").read_text()
    assert "[[ $status -eq 124 ]]" in installer
    assert "preinstall-sentinels.txt" in installer
    assert "user-archive-sentinel" in installer
    assert "tongs_user_plugin_sentinel.py" in installer
    assert "verify_mcp_provider.py" in installer
    assert "post-mcp-sidecar-plugin.ndjson" in installer
    assert "run_desktop_smoke post-mcp-hosted-launch" in installer
    assert "package-file-metadata-with-mcp.json" in installer
    assert "for query in scripts triggers filetriggers" in installer
    assert 'cmp "$evidence_dir/clean-final.json"' in installer
    assert harness.index("preflight_core_version.sh") < harness.index(
        "python-dependencies/prepare_sources.py"
    )
    assert 'cmp "$evidence_dir/installed-previous.json"' in installer
    assert "--setopt=install_weak_deps=False" in installer
    pre_bootstrap = installer[: installer.index("dnf-bootstrap.log")]
    assert "sentinel_snapshot preinstall" in pre_bootstrap
    assert installer.index("sentinel_snapshot preinstall") < installer.index("\ndnf ")
    assert "core_version=$(python3" not in pre_bootstrap
    assert "user_site=$(/usr/bin/python3" not in pre_bootstrap
    assert "expected-companion-packages.txt" in installer
    assert "clean-install-closure.txt" in installer
    assert "|tongs-final" in installer
    assert "companion-consumer-rpms" in harness
    assert "select_companion_rpms.py" in harness
    assert "base-prerequisite-probe.txt" in harness
    assert harness.index("base-prerequisite-probe.txt") < harness.index(
        "podman build --pull=never"
    )
    assert 'sha256sum "$evidence_dir/expected-companion-packages.txt"' in installer
    assert "diffutils" in installer
    assert "dnf_transaction_options=(" in installer
    assert "--setopt=tsflags=" in installer
    assert "dnf-lifecycle-policy.txt" in installer
    transactions = re.findall(
        r"^dnf (?:install|reinstall|upgrade|remove) [^\n]+$", installer, re.MULTILINE
    )
    assert transactions
    assert all('"${dnf_transaction_options[@]}"' in line for line in transactions)


def test_workflow_binds_exact_head_and_has_read_only_permissions() -> None:
    workflow = WORKFLOW.read_text()

    assert "actions: read" in workflow
    assert "contents: read" in workflow
    assert "persist-credentials: false" in workflow
    assert "fetch-depth: 0" in workflow
    assert "TONGS_HEAD_SHA" in workflow
    assert "actions/upload-artifact@v7" in workflow
    assert "secrets" not in workflow
    assert "    paths:" not in workflow


def test_manifest_keeps_core_and_mcp_closures_separate() -> None:
    manifest = json.loads((PACKAGING / "manifest.json").read_text())

    assert all(
        "mcp" not in requirement
        for requirement in manifest["core_runtime_requirements"]
    )
    assert "mcp[cli]" in manifest["mcp_requirement"]


def test_provider_audit_queries_the_mcp_extra_capability() -> None:
    audit = (PACKAGING / "audit_providers.py").read_text()

    assert '"python3dist(mcp[cli])"' in audit
    assert '"provider_query": query' in audit
    assert '"python3-mcp+cli", "python3-mcp"' in audit
    assert '"enabled_repositories"' in audit
    assert "args.output.write_text" in audit
    assert audit.index("args.output.write_text") < audit.index(
        'raise RuntimeError(f"missing Fedora providers: {missing}")'
    )
    assert '"direct-package-provides" if usable else "unresolved"' in audit
