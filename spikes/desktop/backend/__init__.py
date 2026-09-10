"""Synthetic services shared by both desktop shell prototypes."""

from __future__ import annotations

import re
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import entry_points
from pathlib import Path
from threading import Lock, Thread
from urllib.parse import unquote, urlsplit

PROTOCOL = "prototype-1"


class Backend:
    """Expose fixture data and opt-in plugin contributions without TUI hooks."""

    def __init__(self, disabled: frozenset[str] = frozenset()) -> None:
        self.plugins: list[dict] = []
        self.asset_roots: dict[str, Path] = {}
        self._providers: dict[str, object] = {}
        self._help: dict[tuple[str, str], Path] = {}
        self._lock = Lock()
        for ep in entry_points(group="tongs.plugins"):
            record = {
                "id": ep.name,
                "title": ep.name,
                "status": "terminal_only",
                "modules": [],
            }
            self.plugins.append(record)
            if ep.name in disabled:
                record["status"] = "disabled"
                continue
            try:
                if not re.fullmatch(r"[a-zA-Z0-9_-]+", ep.name):
                    raise ValueError("Invalid plugin identifier")
                provider = ep.load()()
                if not hasattr(provider, "get_desktop_modules"):
                    continue
                modules = provider.get_desktop_modules()
                pending_roots = {}
                pending_help = {}
                for module in modules:
                    name = module["id"]
                    if not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
                        raise ValueError("Invalid module identifier")
                    if module["api_version"] != PROTOCOL:
                        raise ValueError("Incompatible prototype API")
                    root = Path(module["asset_dir"]).resolve()
                    entry = (root / module["entry"]).resolve()
                    help_file = (root / module["help"]).resolve()
                    if not all(
                        p.is_relative_to(root) and p.is_file()
                        for p in (entry, help_file)
                    ):
                        raise ValueError("Missing or invalid plugin assets")
                    prefix = f"/plugins/{ep.name}/{name}/"
                    if prefix in pending_roots:
                        raise ValueError("Duplicate plugin module")
                    pending_roots[prefix] = root
                    pending_help[(ep.name, name)] = help_file
                    record["modules"].append(
                        {
                            "id": name,
                            "title": module["title"],
                            "entry_url": prefix + entry.relative_to(root).as_posix(),
                        }
                    )
                self.asset_roots.update(pending_roots)
                self._help.update(pending_help)
                self._providers[ep.name] = provider
                record["status"] = "ready"
            except Exception:  # noqa: BLE001 - isolate plugin failures at the RPC boundary.
                record["status"] = "error"
                record["modules"] = []

    def invoke(self, method: str, params: dict | None = None) -> object:
        """Dispatch the bounded, fixture-only RPC contract."""
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise ValueError("RPC params must be an object")  # noqa: TRY004 - protocol validation error.
        if method == "health":
            return {"fixture": True, "protocol": PROTOCOL}
        if method == "list_reviews":
            return [
                {
                    "id": "normal",
                    "number": 42,
                    "title": "Improve review navigation",
                    "author": "sam",
                    "repo": "acme/review-tools",
                    "forge": "GitHub",
                    "status": "Passing",
                },
                {
                    "id": "large",
                    "number": 84,
                    "title": "Large generated diff (20,000 lines)",
                    "author": "alex",
                    "repo": "acme/platform",
                    "forge": "GitLab",
                    "status": "Running",
                },
            ]
        if method == "get_diff":
            if params.get("id") not in {"normal", "large"}:
                raise ValueError("Unknown fixture review")
            count = 20000 if params["id"] == "large" else 80
            lines = []
            old = new = 0
            for index in range(count):
                kind = (
                    "deletion"
                    if index % 9 == 3
                    else "addition"
                    if index % 9 == 4
                    else "context"
                )
                old += kind != "addition"
                new += kind != "deletion"
                lines.append(
                    {
                        "old_line": old if kind != "addition" else None,
                        "new_line": new if kind != "deletion" else None,
                        "kind": kind,
                        "text": f"    render_review_item({index}, status='ready')",
                    }
                )
            return {"path": "src/review/workspace.py", "lines": lines}
        if method == "list_plugins":
            return self.plugins
        if method == "plugin_help":
            path = self._help.get((params.get("plugin"), params.get("module")))
            if path is None:
                raise ValueError("Unknown plugin module")
            return path.read_text()
        if method == "plugin_invoke":
            provider = self._providers.get(params.get("plugin"))
            if provider is None:
                raise ValueError("Plugin is not available for desktop")
            with self._lock:
                return provider.desktop_call(
                    params.get("method"), params.get("params", {})
                )
        raise ValueError("Unknown prototype method")


class AssetHandler(SimpleHTTPRequestHandler):
    """Serve only declared roots; no API calls or directory listings."""

    def __init__(
        self, *args: object, frontend: Path, backend: Backend, **kwargs: object
    ) -> None:
        self.frontend = frontend
        self.backend = backend
        super().__init__(*args, **kwargs)

    def translate_path(self, path: str) -> str:
        request_path = unquote(urlsplit(path).path)
        root = self.frontend
        relative = request_path.lstrip("/") or "index.html"
        for prefix, plugin_root in self.backend.asset_roots.items():
            if request_path.startswith(prefix):
                root, relative = plugin_root, request_path[len(prefix) :]
                break
        target = (root / relative).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            return str(root / ".unavailable-prototype-resource")
        return str(target)

    def log_message(self, format: str, *args: object) -> None:
        pass


class Assets:
    """Own the loopback HTTP server and deterministic shutdown."""

    def __init__(self, frontend: Path, backend: Backend) -> None:
        if not (frontend / "index.html").is_file():
            raise ValueError("Build the shared frontend before starting a shell")
        handler = partial(AssetHandler, frontend=frontend.resolve(), backend=backend)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def start_assets(frontend_dir: str | Path, backend: Backend) -> Assets:
    return Assets(Path(frontend_dir), backend)
