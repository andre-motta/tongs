from __future__ import annotations

import importlib.util
import io
import subprocess
import tarfile
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).parents[4]
PACKAGING = ROOT / "packaging" / "rpm" / "python-dependencies"


def _load_script(name: str) -> ModuleType:
    path = PACKAGING / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"tongs_rpm_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit_providers = _load_script("audit_providers")
prepare_sources = _load_script("prepare_sources")


def test_repoquery_requests_one_record_per_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    def fake_run(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        captured.extend(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(audit_providers.subprocess, "run", fake_run)

    assert audit_providers._query("python(abi) >= 3.12") == []
    query_format = captured[captured.index("--queryformat") + 1]
    assert query_format.endswith("\n")


def test_audit_rejects_missing_system_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audit_providers, "_query", lambda _requirement: [])
    manifest = {
        "system_requirements": [{"requirement": "python(abi) >= 3.12"}],
        "companions": [],
    }

    with pytest.raises(RuntimeError, match="missing Fedora system providers"):
        audit_providers.audit(manifest)


def test_audit_rejects_gratuitous_companion(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = {
        "name": "python3-sigstore",
        "epoch": "0",
        "version": "4.5.0",
        "release": "1.fc44",
        "arch": "noarch",
        "repo": "updates",
    }
    monkeypatch.setattr(audit_providers, "_query", lambda _requirement: [provider])
    manifest = {
        "system_requirements": [],
        "companions": [{"distribution": "sigstore", "version": "4.5.0"}],
    }

    with pytest.raises(RuntimeError, match="reuse them"):
        audit_providers.audit(manifest)


def test_safe_extract_rejects_parent_path(tmp_path: Path) -> None:
    archive_path = tmp_path / "unsafe.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        member = tarfile.TarInfo("../escape")
        contents = b"bad"
        member.size = len(contents)
        archive.addfile(member, io.BytesIO(contents))

    with pytest.raises(RuntimeError, match="unsafe source member"):
        prepare_sources._safe_extract(archive_path, tmp_path / "output")


def test_prepare_refuses_nonempty_evidence_directory(tmp_path: Path) -> None:
    output = tmp_path / "evidence"
    output.mkdir()
    (output / "old.json").write_text("{}")

    with pytest.raises(RuntimeError, match="must be empty"):
        prepare_sources.prepare({"companions": []}, output)


def test_cargo_vendor_config_is_relocatable() -> None:
    vendor = Path("/tmp/preparation/source/vendor")
    config = f'[source.vendored-sources]\ndirectory = "{vendor}"\n'

    result = prepare_sources._relativize_vendor_config(config, vendor)

    assert result == '[source.vendored-sources]\ndirectory = "vendor"\n'
    assert str(vendor) not in result


@pytest.mark.parametrize(
    "final_url",
    [
        "http://files.pythonhosted.org/source.tar.gz",
        "https://example.com/source.tar.gz",
    ],
)
def test_download_redirect_requires_same_https_host(final_url: str) -> None:
    with pytest.raises(RuntimeError, match="outside HTTPS host"):
        prepare_sources._validate_download_url(
            "https://files.pythonhosted.org/source.tar.gz", final_url
        )


def test_download_accepts_same_https_host() -> None:
    prepare_sources._validate_download_url(
        "https://files.pythonhosted.org/source.tar.gz",
        "https://files.pythonhosted.org/redirected/source.tar.gz",
    )


def test_cargo_package_identity_excludes_temporary_path_id() -> None:
    package = {
        "id": "path+file:///tmp/random/rfc3161#1.0.8",
        "name": "rfc3161-client",
        "version": "1.0.8",
        "source": None,
    }

    assert prepare_sources._cargo_package_identity(package) == (
        "rfc3161-client",
        "1.0.8",
        None,
    )
