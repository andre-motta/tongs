"""Launch the shared desktop prototype in pywebview with the Qt backend."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import shlex
import sys
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from importlib.resources import as_file, files
from pathlib import Path
from threading import Lock, Timer
from time import monotonic
from types import ModuleType
from typing import Any, Protocol

_TITLE = "Tongs Desktop Prototype (fixture data)"
_SANDBOX_DISABLE_ENV = "QTWEBENGINE_DISABLE_SANDBOX"
_CHROMIUM_FLAGS_ENV = "QTWEBENGINE_CHROMIUM_FLAGS"
_ADAPTER_SCRIPT = r"""
window.tongs = Object.freeze({
  invoke(method, params = {}) {
    return window.pywebview.api.invoke(method, params);
  }
});
window.dispatchEvent(new Event('tongs-ready'));
"""


class BackendLike(Protocol):
    """The fixed synchronous backend interface used by both shell prototypes."""

    def invoke(self, method: str, params: dict[str, object] | None = None) -> object:
        """Invoke one prototype RPC method."""


class AssetsLike(Protocol):
    """A running loopback asset server."""

    url: str

    def close(self) -> None:
        """Stop the server and join its worker thread."""


class WindowEventsLike(Protocol):
    """Subset of pywebview window events used by the launcher."""

    loaded: Any


class WindowLike(Protocol):
    """Subset of a pywebview window used by smoke automation."""

    events: WindowEventsLike

    def run_js(self, script: str) -> object:
        """Evaluate JavaScript after the page loads."""

    def evaluate_js(
        self, script: str, callback: Callable[[object], None] | None = None
    ) -> object:
        """Evaluate JavaScript in the native renderer."""

    def destroy(self) -> None:
        """Close the native window."""


@dataclass(frozen=True)
class HostConfig:
    """Resolved launcher settings."""

    frontend: Path | None = None
    width: int = 1440
    height: int = 900
    debug: bool = False
    smoke_report: Path | None = None
    ui_probe: Path | None = None
    screenshot: Path | None = None
    smoke_timeout: float = 20.0
    smoke_close_after: float = 0.5


class Bridge:
    """Expose only the fixed ``invoke`` operation to browser content."""

    def __init__(self, backend: BackendLike) -> None:
        self._backend = backend

    def invoke(self, method: str, params: dict[str, object] | None = None) -> object:
        if not isinstance(method, str):
            raise TypeError("RPC method must be a string")
        return self._backend.invoke(method, params)


def _validate_sandbox_environment(environment: dict[str, str]) -> None:
    disabled = environment.get(_SANDBOX_DISABLE_ENV, "").strip().lower()
    flags = environment.get(_CHROMIUM_FLAGS_ENV, "")
    try:
        parsed_flags = shlex.split(flags)
    except ValueError as error:
        raise RuntimeError("Invalid Qt WebEngine Chromium flags") from error
    sandbox_flag = any(flag.startswith("--no-sandbox") for flag in parsed_flags)
    sandbox_env = disabled not in {"", "0", "false", "no"}
    if sandbox_env or sandbox_flag:
        raise RuntimeError(
            "Qt WebEngine sandbox disabling is not supported by this prototype"
        )


def _load_runtime() -> tuple[
    Callable[[], BackendLike], Callable[[Path, BackendLike], AssetsLike]
]:
    try:
        backend = importlib.import_module("tongs_desktop_webview._fixture_backend")
    except ModuleNotFoundError as error:
        raise RuntimeError(
            "Build and install the prototype wheel before launching it"
        ) from error
    return backend.Backend, backend.start_assets


def _load_webview() -> ModuleType:
    os.environ.setdefault("QT_API", "pyside6")
    try:
        importlib.import_module("qtpy")
        importlib.import_module("PySide6.QtWebChannel")
        importlib.import_module("PySide6.QtWebEngineWidgets")
        return importlib.import_module("webview")
    except ImportError as error:
        raise RuntimeError(
            "Install the desktop runtime with PySide6 and Qt WebEngine support"
        ) from error


def _packaged_frontend() -> AbstractContextManager[Path]:
    resource = files("tongs_desktop_webview").joinpath("assets", "frontend")
    return as_file(resource)


def _packaged_icon() -> AbstractContextManager[Path]:
    resource = files("tongs_desktop_webview").joinpath(
        "assets", "frontend", "assets", "tongs-icon.png"
    )
    if not resource.is_file():
        development_icon = (
            Path(__file__).resolve().parents[2]
            / "packaging"
            / "io.github.andre_motta.tongs.png"
        )
        return _PathContext(development_icon)
    return as_file(resource)


def _configure_webview(webview_module: ModuleType) -> None:
    settings = webview_module.settings
    settings["ALLOW_DOWNLOADS"] = False
    settings["ALLOW_FILE_URLS"] = False
    settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    settings["SHOW_DEFAULT_MENUS"] = False


def _install_adapter(window: WindowLike) -> None:
    """Install the fixed frontend adapter after pywebview exposes its API."""
    window.run_js(_ADAPTER_SCRIPT)


_SMOKE_SCRIPT = r"""
(async () => {
  const waitForAdapter = () => new Promise((resolve, reject) => {
    if (window.tongs && typeof window.tongs.invoke === 'function') {
      resolve();
      return;
    }
    const timer = setTimeout(() => reject(new Error('window.tongs was not installed')), 10000);
    window.addEventListener('tongs-ready', () => {
      clearTimeout(timer);
      resolve();
    }, {once: true});
  });
  await waitForAdapter();
  const health = await window.tongs.invoke('health');
  const plugins = await window.tongs.invoke('list_plugins');
  const sample = plugins.find((plugin) => plugin.id === 'sample-desktop');
  const module = sample && sample.modules && sample.modules[0];
  let pluginCall = null;
  let help = null;
  if (sample && sample.status === 'ready' && module) {
    pluginCall = await window.tongs.invoke('plugin_invoke', {
      plugin: sample.id,
      method: 'echo',
      params: {text: 'native smoke'}
    });
    help = await window.tongs.invoke('plugin_help', {
      plugin: sample.id,
      module: module.id
    });
  }
  return {
    ok: health.fixture === true &&
      health.protocol === 'prototype-1' &&
      sample && sample.status === 'ready' &&
      pluginCall && pluginCall.message.includes('native smoke') &&
      typeof help === 'string' && help.includes('bundled'),
    pageTitle: document.title,
    adapter: 'window.tongs',
    health,
    plugins: plugins.map((plugin) => ({id: plugin.id, status: plugin.status})),
    pluginCall,
    pluginHelpBundled: typeof help === 'string' && help.includes('bundled')
  };
})()
"""


def _smoke_script(ui_probe: str | None) -> str:
    if ui_probe is None:
        return _SMOKE_SCRIPT
    encoded_probe = json.dumps(ui_probe)
    return f"""
(async () => {{
  const core = await ({_SMOKE_SCRIPT});
  const uiProbe = await (0, eval)({encoded_probe});
  return {{
    ...core,
    ok: core.ok === true && uiProbe && uiProbe.ok === true,
    ui_probe: uiProbe
  }};
}})()
"""


class SmokeController:
    """Record renderer RPC evidence and close the window deterministically."""

    def __init__(
        self,
        window: WindowLike,
        report_path: Path,
        *,
        started_at: float,
        timeout: float,
        close_after: float,
        platform_name: Callable[[], str],
        ui_probe: str | None = None,
        screenshot: Path | None = None,
        capture_window: Callable[
            [WindowLike, Path, Callable[[bool, str | None], None]], None
        ]
        | None = None,
    ) -> None:
        self._window = window
        self._report_path = report_path
        self._started_at = started_at
        self._timeout = timeout
        self._close_after = close_after
        self._platform_name = platform_name
        self._ui_probe = ui_probe
        self._screenshot = screenshot
        self._capture_window = capture_window
        self._lock = Lock()
        self._result: dict[str, object] | None = None
        self._watchdog: Timer | None = None

    def start(self) -> None:
        self._window.events.loaded += self._on_loaded
        self._watchdog = Timer(self._timeout, self._on_timeout)
        self._watchdog.daemon = True
        self._watchdog.start()

    def _on_loaded(self) -> None:
        self._window.evaluate_js(
            _smoke_script(self._ui_probe), callback=self._on_result
        )

    def _on_result(self, result: object) -> None:
        if isinstance(result, dict):
            payload = dict(result)
        else:
            payload = {"ok": False, "error": "Native smoke returned invalid data"}
        payload.update(
            {
                "phase": "ready",
                "renderer": "qtwebengine",
                "qt_platform": self._platform_name(),
                "startup_ms": round((monotonic() - self._started_at) * 1000, 1),
            }
        )
        with self._lock:
            if self._result is not None:
                return
            self._result = payload
        if self._watchdog is not None:
            self._watchdog.cancel()
        if self._screenshot is not None and self._capture_window is not None:
            self._capture_window(self._window, self._screenshot, self._on_screenshot)
            return
        self._complete_ready()

    def _on_screenshot(self, succeeded: bool, error: str | None) -> None:
        with self._lock:
            if self._result is None:
                return
            self._result["screenshot"] = str(self._screenshot) if succeeded else None
            self._result["screenshot_bytes"] = (
                self._screenshot.stat().st_size
                if succeeded and self._screenshot is not None
                else 0
            )
            if error is not None:
                self._result["ok"] = False
                self._result["screenshot_error"] = error
        self._complete_ready()

    def _complete_ready(self) -> None:
        with self._lock:
            if self._result is None:
                return
            self._write(self._result)
        close_timer = Timer(self._close_after, self._window.destroy)
        close_timer.daemon = True
        close_timer.start()

    def _on_timeout(self) -> None:
        payload: dict[str, object] = {
            "ok": False,
            "error": "Native smoke timed out",
            "phase": "ready",
            "renderer": "qtwebengine",
            "qt_platform": self._platform_name(),
            "startup_ms": round((monotonic() - self._started_at) * 1000, 1),
        }
        with self._lock:
            if self._result is not None:
                return
            self._result = payload
            self._write(payload)
        self._window.destroy()

    def finish(self, *, assets_stopped: bool) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
        with self._lock:
            if self._result is None:
                self._result = {
                    "ok": False,
                    "error": "Native window closed before smoke completed",
                    "renderer": "qtwebengine",
                }
            self._result.update({"phase": "closed", "assets_stopped": assets_stopped})
            self._write(self._result)

    def _write(self, payload: dict[str, object]) -> None:
        self._report_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._report_path.with_suffix(self._report_path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        temporary.replace(self._report_path)


def run_application(
    config: HostConfig,
    *,
    backend_factory: Callable[[], BackendLike] | None = None,
    assets_factory: Callable[[Path, BackendLike], AssetsLike] | None = None,
    webview_module: ModuleType | None = None,
    platform_name: Callable[[], str] | None = None,
    capture_window: Callable[
        [WindowLike, Path, Callable[[bool, str | None], None]], None
    ]
    | None = None,
) -> int:
    """Run one native window and always stop its loopback asset server."""
    _validate_sandbox_environment(dict(os.environ))
    if backend_factory is None or assets_factory is None:
        runtime_backend, runtime_assets = _load_runtime()
        backend_factory = backend_factory or runtime_backend
        assets_factory = assets_factory or runtime_assets
    webview_module = webview_module or _load_webview()
    _configure_webview(webview_module)

    frontend_context = (
        _packaged_frontend()
        if config.frontend is None
        else _PathContext(config.frontend.resolve())
    )
    started_at = monotonic()
    with frontend_context as frontend, _packaged_icon() as icon:
        ui_probe = config.ui_probe.read_text() if config.ui_probe is not None else None
        backend = backend_factory()
        assets = assets_factory(frontend, backend)
        smoke: SmokeController | None = None
        assets_stopped = False
        try:
            window = webview_module.create_window(
                _TITLE,
                url=assets.url,
                js_api=Bridge(backend),
                width=config.width,
                height=config.height,
                min_size=(800, 600),
                resizable=True,
                text_select=True,
                zoomable=True,
                confirm_close=False,
                background_color="#111827",
            )
            if window is None:
                raise RuntimeError("pywebview did not create a native window")

            def install_adapter() -> None:
                _install_adapter(window)

            window.events.loaded += install_adapter
            if config.smoke_report is not None:
                smoke = SmokeController(
                    window,
                    config.smoke_report,
                    started_at=started_at,
                    timeout=config.smoke_timeout,
                    close_after=config.smoke_close_after,
                    platform_name=platform_name or _qt_platform_name,
                    ui_probe=ui_probe,
                    screenshot=config.screenshot,
                    capture_window=capture_window or _qt_capture_window,
                )
                smoke.start()
            webview_module.start(
                gui="qt",
                debug=config.debug,
                private_mode=True,
                http_server=False,
                icon=str(icon),
            )
        finally:
            assets.close()
            assets_stopped = True
            if smoke is not None:
                smoke.finish(assets_stopped=assets_stopped)
    return 0


def _qt_platform_name() -> str:
    """Read Qt's active platform plugin after QApplication initialization."""
    qt_gui = importlib.import_module("PySide6.QtGui")
    application = qt_gui.QGuiApplication.instance()
    return application.platformName() if application is not None else "unknown"


def _qt_capture_window(
    window: WindowLike,
    path: Path,
    finished: Callable[[bool, str | None], None],
) -> None:
    """Capture only this application's Qt widget on the Qt GUI thread."""
    qt_core = importlib.import_module("PySide6.QtCore")
    native = getattr(window, "native", None)
    if native is None:
        finished(False, "pywebview did not expose its native Qt window")
        return

    def capture() -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            succeeded = bool(native.grab().save(str(path), "PNG"))
            finished(
                succeeded,
                None if succeeded else "Qt could not encode the native window capture",
            )
        except (OSError, RuntimeError) as error:
            finished(False, str(error))

    qt_core.QTimer.singleShot(0, native, capture)


class _PathContext(AbstractContextManager[Path]):
    def __init__(self, path: Path) -> None:
        self._path = path

    def __enter__(self) -> Path:
        return self._path

    def __exit__(self, *exc_info: object) -> None:
        return None


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _nonnegative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Launch the Tongs Python webview comparison prototype"
    )
    parser.add_argument(
        "--frontend",
        type=Path,
        help="development override for the frontend directory",
    )
    parser.add_argument("--width", type=_positive_int, default=1440)
    parser.add_argument("--height", type=_positive_int, default=900)
    parser.add_argument(
        "--debug",
        action="store_true",
        help="enable pywebview developer diagnostics",
    )
    parser.add_argument(
        "--smoke-report",
        type=Path,
        help="run native bridge checks, write JSON, and close",
    )
    parser.add_argument(
        "--ui-probe",
        type=Path,
        help="async JavaScript expression evaluated during native smoke",
    )
    parser.add_argument(
        "--screenshot",
        type=Path,
        help="capture this Qt window during native smoke",
    )
    parser.add_argument("--smoke-timeout", type=float, default=20.0)
    parser.add_argument(
        "--smoke-close-after",
        type=_nonnegative_float,
        default=0.5,
        help="seconds to leave a successful smoke window open",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.smoke_timeout <= 0:
        print("error: --smoke-timeout must be positive", file=sys.stderr)
        return 2
    if args.screenshot is not None and args.smoke_report is None:
        print("error: --screenshot requires --smoke-report", file=sys.stderr)
        return 2
    config = HostConfig(
        frontend=args.frontend,
        width=args.width,
        height=args.height,
        debug=args.debug,
        smoke_report=args.smoke_report,
        ui_probe=args.ui_probe,
        screenshot=args.screenshot,
        smoke_timeout=args.smoke_timeout,
        smoke_close_after=args.smoke_close_after,
    )
    try:
        return run_application(config)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
