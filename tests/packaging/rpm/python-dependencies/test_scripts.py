from __future__ import annotations

import importlib.util
import io
import subprocess
import tarfile
import urllib.error
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
resolve_srpms = _load_script("resolve_srpms")


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


def test_download_retries_transient_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contents = b"source"
    attempts = 0

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            self.close()

        def geturl(self) -> str:
            return "https://files.pythonhosted.org/source.tar.gz"

    def urlopen(*_args: object, **_kwargs: object) -> Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionResetError("reset")
        return Response(contents)

    monkeypatch.setattr(prepare_sources.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(prepare_sources.time, "sleep", lambda _seconds: None)
    output = tmp_path / "source.tar.gz"
    prepare_sources._download(
        {
            "url": "https://files.pythonhosted.org/source.tar.gz",
            "filename": output.name,
            "bytes": len(contents),
            "sha256": prepare_sources.hashlib.sha256(contents).hexdigest(),
        },
        output,
    )

    assert attempts == 2
    assert output.read_bytes() == contents


def test_download_rejects_bytes_that_differ_from_the_pinned_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    served = b"forged"

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            self.close()

        def geturl(self) -> str:
            return "https://files.pythonhosted.org/source.tar.gz"

    monkeypatch.setattr(
        prepare_sources.urllib.request, "urlopen", lambda *_a, **_k: Response(served)
    )
    output = tmp_path / "source.tar.gz"
    # The declared size matches, so only the pinned hash can reject the bytes.
    with pytest.raises(RuntimeError, match="source hash mismatch"):
        prepare_sources._download(
            {
                "url": "https://files.pythonhosted.org/source.tar.gz",
                "filename": output.name,
                "bytes": len(served),
                "sha256": prepare_sources.hashlib.sha256(b"source").hexdigest(),
            },
            output,
        )


def test_download_retries_server_http_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contents = b"source"
    attempts = 0
    sleeps: list[int] = []

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            self.close()

        def geturl(self) -> str:
            return "https://files.pythonhosted.org/source.tar.gz"

    def urlopen(*_args: object, **_kwargs: object) -> object:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise urllib.error.HTTPError(
                "https://files.pythonhosted.org/source.tar.gz",
                503,
                "Service Unavailable",
                {},
                None,
            )
        return Response(contents)

    monkeypatch.setattr(prepare_sources.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(prepare_sources.time, "sleep", sleeps.append)
    output = tmp_path / "source.tar.gz"
    prepare_sources._download(
        {
            "url": "https://files.pythonhosted.org/source.tar.gz",
            "filename": output.name,
            "bytes": len(contents),
            "sha256": prepare_sources.hashlib.sha256(contents).hexdigest(),
        },
        output,
    )

    assert attempts == 3
    assert sleeps == [1, 2]
    assert output.read_bytes() == contents


def test_download_does_not_retry_client_http_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = 0

    def urlopen(*_args: object, **_kwargs: object) -> object:
        nonlocal attempts
        attempts += 1
        raise urllib.error.HTTPError(
            "https://files.pythonhosted.org/missing.tar.gz", 404, "Not Found", {}, None
        )

    monkeypatch.setattr(prepare_sources.urllib.request, "urlopen", urlopen)

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        prepare_sources._download(
            {
                "url": "https://files.pythonhosted.org/missing.tar.gz",
                "filename": "missing.tar.gz",
                "bytes": 1,
                "sha256": "unused",
            },
            tmp_path / "missing.tar.gz",
        )

    assert exc_info.value.code == 404
    assert attempts == 1


def test_download_raises_last_transient_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    errors = [urllib.error.URLError(f"failure-{attempt}") for attempt in range(3)]

    def urlopen(*_args: object, **_kwargs: object) -> object:
        raise errors.pop(0)

    monkeypatch.setattr(prepare_sources.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(prepare_sources.time, "sleep", lambda _seconds: None)

    with pytest.raises(urllib.error.URLError, match="failure-2"):
        prepare_sources._download(
            {
                "url": "https://files.pythonhosted.org/source.tar.gz",
                "filename": "source.tar.gz",
                "bytes": 1,
                "sha256": "unused",
            },
            tmp_path / "source.tar.gz",
        )

    assert errors == []


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


def test_resolve_srpms_uses_exact_names_with_prefix_overlap(tmp_path: Path) -> None:
    filenames = [
        "python-sigstore-4.5.0-1.fc44.src.rpm",
        "python-sigstore-models-0.0.6-1.fc44.src.rpm",
        "python-sigstore-rekor-types-0.0.18-1.fc44.src.rpm",
    ]
    for filename in filenames:
        (tmp_path / filename).touch()
    metadata = (
        "python-sigstore|4.5.0|1.fc44|noarch|"
        "python-sigstore-4.5.0-1.fc44.src.rpm\n"
        "python-sigstore-models|0.0.6|1.fc44|noarch|"
        "python-sigstore-models-0.0.6-1.fc44.src.rpm\n"
        "python-sigstore-rekor-types|0.0.18|1.fc44|noarch|"
        "python-sigstore-rekor-types-0.0.18-1.fc44.src.rpm"
    )

    resolved = resolve_srpms.resolve_srpms(
        ["sigstore", "sigstore-models", "sigstore-rekor-types"],
        metadata,
        tmp_path,
    )

    assert [path.name for _, path in resolved] == filenames


def test_resolve_srpm_uses_queried_filename_with_binary_header_arch(
    tmp_path: Path,
) -> None:
    filename = "python-rfc3161-client-1.0.8-1.fc44.src.rpm"
    (tmp_path / filename).touch()

    resolved = resolve_srpms.resolve_srpms(
        ["rfc3161-client"],
        "python-rfc3161-client|1.0.8|1.fc44|x86_64|"
        "python-rfc3161-client-1.0.8-1.fc44.src.rpm",
        tmp_path,
    )

    assert resolved == [("rfc3161-client", tmp_path / filename)]


def test_resolve_srpm_rejects_filename_traversal(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="unsafe SRPM filename"):
        resolve_srpms.resolve_srpms(
            ["sigstore"],
            "python-sigstore|4.5.0|1.fc44|noarch|../source.src.rpm",
            tmp_path,
        )
