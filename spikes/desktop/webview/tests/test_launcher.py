"""Unit tests for bridge safety, lifecycle ownership, and smoke reporting."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tongs_desktop_webview.launcher import (
    Bridge,
    HostConfig,
    _smoke_script,
    _validate_sandbox_environment,
    run_application,
)


class FakeEvent:
    def __init__(self) -> None:
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def fire(self) -> None:
        for handler in self.handlers:
            handler()


class FakeWindow:
    def __init__(self, smoke_result: dict | None = None) -> None:
        self.events = SimpleNamespace(loaded=FakeEvent())
        self.destroyed = False
        self.smoke_result = smoke_result or {"ok": True}
        self.scripts = []

    def run_js(self, script):
        self.scripts.append(script)

    def evaluate_js(self, script, callback=None):
        assert "window.tongs.invoke('health')" in script
        if callback is not None:
            callback(self.smoke_result)

    def destroy(self) -> None:
        self.destroyed = True


class FakeWebview:
    def __init__(self, smoke_result: dict | None = None) -> None:
        self.settings = {}
        self.window = FakeWindow(smoke_result)
        self.created = None
        self.started = None

    def create_window(self, title, **kwargs):
        self.created = (title, kwargs)
        return self.window

    def start(self, **kwargs):
        self.started = kwargs
        self.window.events.loaded.fire()


class FakeBackend:
    def __init__(self) -> None:
        self.calls = []

    def invoke(self, method, params=None):
        self.calls.append((method, params))
        return {"method": method}


class FakeAssets:
    url = "http://127.0.0.1:43210/"

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_bridge_exposes_only_invoke_and_validates_method() -> None:
    backend = FakeBackend()
    bridge = Bridge(backend)

    assert bridge.invoke("health") == {"method": "health"}
    assert backend.calls == [("health", None)]
    with pytest.raises(TypeError, match="must be a string"):
        bridge.invoke(3)  # type: ignore[arg-type]
    assert not hasattr(bridge, "plugins")


@pytest.mark.parametrize(
    "environment",
    [
        {"QTWEBENGINE_DISABLE_SANDBOX": "1"},
        {"QTWEBENGINE_DISABLE_SANDBOX": "true"},
        {"QTWEBENGINE_DISABLE_SANDBOX": "2"},
        {"QTWEBENGINE_CHROMIUM_FLAGS": "--disable-gpu --no-sandbox"},
        {"QTWEBENGINE_CHROMIUM_FLAGS": "--no-sandbox=1"},
    ],
)
def test_explicit_sandbox_disabling_is_rejected(environment) -> None:
    with pytest.raises(RuntimeError, match="sandbox disabling"):
        _validate_sandbox_environment(environment)


def test_window_configuration_and_clean_shutdown(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("fixture")
    assets = FakeAssets()
    webview = FakeWebview()

    result = run_application(
        HostConfig(frontend=frontend),
        backend_factory=FakeBackend,
        assets_factory=lambda _frontend, _backend: assets,
        webview_module=webview,  # type: ignore[arg-type]
        platform_name=lambda: "test-platform",
    )

    assert result == 0
    assert assets.closed
    _, options = webview.created
    assert options["url"].startswith("http://127.0.0.1:")
    assert options["resizable"] is True
    assert options["confirm_close"] is False
    assert set(vars(options["js_api"])) == {"_backend"}
    assert webview.started == {
        "gui": "qt",
        "debug": False,
        "private_mode": True,
        "http_server": False,
        "icon": str(
            Path(__file__).resolve().parents[1]
            / "packaging/io.github.andre_motta.tongs.png"
        ),
    }
    assert len(webview.window.scripts) == 1
    assert "window.pywebview.api.invoke" in webview.window.scripts[0]
    assert "tongs-ready" in webview.window.scripts[0]
    assert webview.settings == {
        "ALLOW_DOWNLOADS": False,
        "ALLOW_FILE_URLS": False,
        "OPEN_EXTERNAL_LINKS_IN_BROWSER": True,
        "SHOW_DEFAULT_MENUS": False,
    }


def test_ui_probe_is_evaluated_after_core_bridge_probe() -> None:
    probe = "(async () => ({ok: true, selected: 'large'}))()"

    script = _smoke_script(probe)

    assert "const core = await" in script
    assert "ui_probe: uiProbe" in script
    assert json.dumps(probe) in script


def test_asset_server_closes_when_native_start_fails(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    assets = FakeAssets()
    webview = FakeWebview()

    def fail_start(**_kwargs) -> None:
        raise RuntimeError("native failure")

    webview.start = fail_start
    with pytest.raises(RuntimeError, match="native failure"):
        run_application(
            HostConfig(frontend=frontend),
            backend_factory=FakeBackend,
            assets_factory=lambda _frontend, _backend: assets,
            webview_module=webview,  # type: ignore[arg-type]
            platform_name=lambda: "test-platform",
        )
    assert assets.closed


def test_smoke_report_records_ready_and_clean_close(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    probe = tmp_path / "probe.js"
    probe.write_text("(async () => ({ok: true}))()")
    screenshot = tmp_path / "native.png"
    report = tmp_path / "report.json"
    assets = FakeAssets()
    webview = FakeWebview(
        {
            "ok": True,
            "health": {"fixture": True, "protocol": "prototype-1"},
            "pluginHelpBundled": True,
        }
    )

    run_application(
        HostConfig(
            frontend=frontend,
            smoke_report=report,
            ui_probe=probe,
            screenshot=screenshot,
            smoke_timeout=2,
            smoke_close_after=0,
        ),
        backend_factory=FakeBackend,
        assets_factory=lambda _frontend, _backend: assets,
        webview_module=webview,  # type: ignore[arg-type]
        platform_name=lambda: "wayland",
        capture_window=lambda _window, path, done: (
            path.write_bytes(b"png"),
            done(True, None),
        ),
    )

    payload = json.loads(report.read_text())
    assert payload["ok"] is True
    assert payload["phase"] == "closed"
    assert payload["assets_stopped"] is True
    assert payload["renderer"] == "qtwebengine"
    assert payload["qt_platform"] == "wayland"
    assert payload["startup_ms"] >= 0
    assert payload["screenshot"] == str(screenshot)
    assert payload["screenshot_bytes"] == 3
