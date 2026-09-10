"""Verify the common fixture and opt-in plugin boundary before shell work."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

from backend import Backend, start_assets
from tongs_desktop_sample import DesktopPlugin, TerminalPlugin


class BackendTests(unittest.TestCase):
    def test_fixture_methods_and_large_diff(self) -> None:
        backend = Backend()
        self.assertTrue(backend.invoke("health")["fixture"])
        self.assertEqual(len(backend.invoke("list_reviews")), 2)
        lines = backend.invoke("get_diff", {"id": "large"})["lines"]
        self.assertEqual(len(lines), 20000)
        self.assertIsNone(
            next(line for line in lines if line["kind"] == "addition")["old_line"]
        )
        self.assertIsNone(
            next(line for line in lines if line["kind"] == "deletion")["new_line"]
        )
        for method, params in [
            ("unknown", {}),
            ("get_diff", {"id": "missing"}),
            ("health", []),
        ]:
            with self.assertRaises(ValueError):
                backend.invoke(method, params)

    def test_independently_installed_desktop_and_terminal_plugins(self) -> None:
        backend = Backend()
        plugins = {item["id"]: item for item in backend.invoke("list_plugins")}
        self.assertEqual(plugins["sample-desktop"]["status"], "ready")
        self.assertEqual(plugins["sample-terminal"]["status"], "terminal_only")
        self.assertEqual(plugins["sample-terminal"]["modules"], [])
        result = backend.invoke(
            "plugin_invoke",
            {
                "plugin": "sample-desktop",
                "method": "echo",
                "params": {"text": "round trip"},
            },
        )
        self.assertEqual(
            result, {"message": "Python plugin received: round trip", "calls": 1}
        )
        self.assertIn(
            "bundled",
            backend.invoke(
                "plugin_help", {"plugin": "sample-desktop", "module": "inspector"}
            ),
        )
        with self.assertRaises(ValueError):
            backend.invoke(
                "plugin_invoke", {"plugin": "sample-terminal", "method": "echo"}
            )

    def test_disabled_plugin_has_no_assets_or_backend(self) -> None:
        backend = Backend(disabled=frozenset({"sample-desktop"}))
        plugin = next(
            item for item in backend.plugins if item["id"] == "sample-desktop"
        )
        self.assertEqual(plugin["status"], "disabled")
        self.assertFalse(backend.asset_roots)
        with self.assertRaises(ValueError):
            backend.invoke(
                "plugin_help", {"plugin": "sample-desktop", "module": "inspector"}
            )

    def test_terminal_hooks_are_never_called(self) -> None:
        with (
            patch.object(TerminalPlugin, "get_commands") as commands,
            patch.object(TerminalPlugin, "get_screens") as screens,
            patch.object(TerminalPlugin, "on_app_ready") as ready,
            patch.object(TerminalPlugin, "on_app_shutdown") as shutdown,
        ):
            backend = Backend()
            backend.invoke("list_plugins")
        for hook in (commands, screens, ready, shutdown):
            hook.assert_not_called()

    def test_sidecar_frames_errors_ids_and_shuts_down_on_eof(self) -> None:
        with tempfile.TemporaryDirectory() as frontend:
            (Path(frontend) / "index.html").write_text("<h1>fixture</h1>")
            completed = subprocess.run(
                [sys.executable, "-m", "backend", "--frontend", frontend],
                cwd=Path(__file__).resolve().parents[1],
                input='{"id":31,"method":"health"}\nnot-json\n'
                '{"id":33,"method":"missing"}\n{"id":34,"method":"health"}\n',
                text=True,
                capture_output=True,
                timeout=15,
                check=True,
            )
        ready, health, malformed, unknown, recovered = map(
            json.loads, completed.stdout.splitlines()
        )
        self.assertEqual(ready["event"], "ready")
        self.assertEqual(health["id"], 31)
        self.assertTrue(health["result"]["fixture"])
        self.assertIsNone(malformed["id"])
        self.assertIn("error", malformed)
        self.assertEqual(unknown["id"], 33)
        self.assertIn("error", unknown)
        self.assertEqual(recovered["id"], 34)
        with self.assertRaises(OSError):
            urlopen(ready["url"], timeout=1)

    def test_invalid_plugin_assets_fail_without_breaking_host(self) -> None:
        with patch.object(
            DesktopPlugin,
            "get_desktop_modules",
            return_value=[{"id": "broken", "api_version": "incompatible"}],
        ):
            backend = Backend()
        plugin = next(
            item for item in backend.plugins if item["id"] == "sample-desktop"
        )
        self.assertEqual(plugin["status"], "error")
        self.assertEqual(plugin["modules"], [])
        self.assertTrue(backend.invoke("health")["fixture"])

    def test_assets_are_confined_and_plugin_bundle_is_available(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            frontend = root / "frontend"
            frontend.mkdir()
            (frontend / "index.html").write_text("<h1>fixture</h1>")
            (root / "private.txt").write_text("do not serve")
            (frontend / "escape.txt").symlink_to(root / "private.txt")
            assets = start_assets(frontend, Backend())
            try:
                with urlopen(assets.url) as response:
                    self.assertIn(b"fixture", response.read())
                with urlopen(
                    assets.url + "plugins/sample-desktop/inspector/module.js"
                ) as response:
                    self.assertIn(b"export function mount", response.read())
                for path in [
                    "%2e%2e/private.txt",
                    "escape.txt",
                    "plugins/sample-desktop/inspector/%2e%2e/__init__.py",
                ]:
                    with self.assertRaises(HTTPError) as error:
                        urlopen(assets.url + path)
                    self.assertEqual(error.exception.code, 404)
                    error.exception.close()
            finally:
                assets.close()
            self.assertFalse(assets.thread.is_alive())


if __name__ == "__main__":
    unittest.main()
