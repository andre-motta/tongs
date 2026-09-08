from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

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
