from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).parents[4]
SCRIPT = ROOT / "packaging" / "rpm" / "desktop" / "verify_rpm_state.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "tongs_desktop_verify_rpm_state", SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


verify_rpm_state = _load_script()


def test_rpm_purelib_uses_fedora_scheme_instead_of_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    abi = f"python{sys.version_info.major}.{sys.version_info.minor}"
    default = f"/usr/local/lib/{abi}/site-packages"
    rpm = f"/usr/lib/{abi}/site-packages"
    calls: list[tuple[str, str | None]] = []

    def get_path(name: str, scheme: str | None = None) -> str:
        calls.append((name, scheme))
        return default if scheme is None else rpm

    monkeypatch.setattr(verify_rpm_state.sysconfig, "get_path", get_path)
    monkeypatch.setattr(
        verify_rpm_state.sysconfig,
        "get_scheme_names",
        lambda: ("posix_prefix", "rpm_prefix"),
    )

    assert default != rpm
    assert verify_rpm_state._rpm_purelib() == rpm
    assert calls == [("purelib", "rpm_prefix")]


@pytest.mark.parametrize(
    "purelib_template",
    [
        "relative/site-packages",
        "/opt/lib/{abi}/site-packages",
        "/usr/local/lib/{abi}/site-packages",
        "/usr/lib/python0.0/site-packages",
        "/usr/lib/{abi}/vendor-packages",
    ],
)
def test_rpm_purelib_rejects_non_system_or_wrong_abi_path(
    monkeypatch: pytest.MonkeyPatch, purelib_template: str
) -> None:
    abi = f"python{sys.version_info.major}.{sys.version_info.minor}"
    purelib = purelib_template.format(abi=abi)
    monkeypatch.setattr(
        verify_rpm_state.sysconfig, "get_scheme_names", lambda: ("rpm_prefix",)
    )
    monkeypatch.setattr(
        verify_rpm_state.sysconfig,
        "get_path",
        lambda _name, *, scheme: purelib,
    )

    with pytest.raises(RuntimeError, match="invalid Fedora RPM purelib path"):
        verify_rpm_state._rpm_purelib()


def test_rpm_purelib_requires_fedora_scheme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        verify_rpm_state.sysconfig, "get_scheme_names", lambda: ("posix_prefix",)
    )

    with pytest.raises(
        RuntimeError, match="does not provide the Fedora RPM path scheme"
    ):
        verify_rpm_state._rpm_purelib()


def test_test_plugin_allows_exact_interpreter_cache_files() -> None:
    abi = f"python{sys.version_info.major}.{sys.version_info.minor}"
    purelib = f"/usr/lib/{abi}/site-packages"
    cache_tag = sys.implementation.cache_tag
    assert cache_tag == f"cpython-{sys.version_info.major}{sys.version_info.minor}"

    for suffix in (".pyc", ".opt-1.pyc"):
        path = f"{purelib}/__pycache__/tongs_rpm_test_plugin.{cache_tag}{suffix}"
        assert verify_rpm_state._allowed_path(
            "tongs-desktop-test-plugin", path, purelib
        )


def test_test_plugin_rejects_unexpected_interpreter_cache_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    abi = f"python{sys.version_info.major}.{sys.version_info.minor}"
    purelib = f"/usr/lib/{abi}/site-packages"
    monkeypatch.setattr(
        verify_rpm_state.sys,
        "implementation",
        SimpleNamespace(cache_tag="cpython-000"),
    )

    with pytest.raises(RuntimeError, match="unexpected system Python cache tag"):
        verify_rpm_state._test_plugin_pyc_paths(purelib)


@pytest.mark.parametrize(
    "path_template",
    [
        "{purelib}/__pycache__/unrelated.{cache_tag}.pyc",
        "{purelib}/__pycache__/tongs_rpm_test_plugin.{cache_tag}.opt-2.pyc",
        "{purelib}/__pycache__/tongs_rpm_test_plugin.cpython-000.pyc",
        "{purelib}/__pycache__/../__pycache__/tongs_rpm_test_plugin.{cache_tag}.pyc",
        "{purelib}/nested/__pycache__/tongs_rpm_test_plugin.{cache_tag}.pyc",
    ],
)
def test_test_plugin_rejects_unrelated_or_traversing_cache_paths(
    path_template: str,
) -> None:
    abi = f"python{sys.version_info.major}.{sys.version_info.minor}"
    purelib = f"/usr/lib/{abi}/site-packages"
    cache_tag = sys.implementation.cache_tag
    path = path_template.format(purelib=purelib, cache_tag=cache_tag)

    assert not verify_rpm_state._allowed_path(
        "tongs-desktop-test-plugin", path, purelib
    )


def test_inventory_tracks_private_cache_directory_but_not_shared_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    abi = f"python{sys.version_info.major}.{sys.version_info.minor}"
    purelib = f"/usr/lib/{abi}/site-packages"
    cache_dir = f"{purelib}/__pycache__"
    private_cache_dir = f"{purelib}/tongs_rpm_test_plugin_assets/__pycache__"
    cache_paths = sorted(verify_rpm_state._test_plugin_pyc_paths(purelib))
    records: list[dict[str, str]] = [
        {"path": cache_dir, "mode": "drwxr-xr-x"},
        {"path": private_cache_dir, "mode": "drwxr-xr-x"},
        *({"path": path, "mode": "-rw-r--r--"} for path in cache_paths),
    ]
    monkeypatch.setattr(verify_rpm_state, "_rpm_purelib", lambda: purelib)
    monkeypatch.setattr(
        verify_rpm_state, "_package_file_records", lambda _name: records
    )
    output = tmp_path / "inventory.json"

    verify_rpm_state.write_inventory(["tongs-desktop-test-plugin"], output)

    inventory = verify_rpm_state.json.loads(output.read_text())
    assert inventory["paths"] == [
        {
            "package": "tongs-desktop-test-plugin",
            "path": private_cache_dir,
            "type": "directory",
        },
        *[
            {
                "package": "tongs-desktop-test-plugin",
                "path": path,
                "type": "file",
            }
            for path in cache_paths
        ],
    ]


def test_metadata_requires_test_plugin_private_cache_directory() -> None:
    abi = f"python{sys.version_info.major}.{sys.version_info.minor}"
    purelib = f"/usr/lib/{abi}/site-packages"

    assert verify_rpm_state._required_owned_directories(
        ["python3-tongs", "tongs-desktop-test-plugin"], purelib
    ) == {
        f"{purelib}/tongs_rpm_test_plugin_assets/__pycache__": (
            "tongs-desktop-test-plugin"
        )
    }
    assert (
        verify_rpm_state._required_owned_directories(["python3-tongs"], purelib) == {}
    )
