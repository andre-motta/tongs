"""Configuration loader with platformdirs for cross-platform paths."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import platformdirs

APP_NAME = "tongs"


def config_dir() -> Path:
    return Path(platformdirs.user_config_dir(APP_NAME))


def cache_dir() -> Path:
    return Path(platformdirs.user_cache_dir(APP_NAME))


def data_dir() -> Path:
    return Path(platformdirs.user_data_dir(APP_NAME))


@dataclass
class HostConfig:
    hostname: str
    forge_type: str = ""


@dataclass
class Config:
    scan_root: str = "~/git"
    scan_depth: int = 5
    editor_command: str = ""
    external_editor_enabled: bool = True
    theme: str = "monokai"
    diff_style: str = "unified"
    show_draft_mrs: bool = True
    ascii_mode: bool = False
    mr_list_ttl: int = 60
    diff_ttl: int = 300
    max_cache_size_mb: int = 100
    max_parallel: int = 8
    request_timeout: int = 30
    extra_hosts: dict[str, HostConfig] = field(default_factory=dict)
    plugin_config: dict[str, dict] = field(default_factory=dict)

    @property
    def scan_root_path(self) -> Path:
        return Path(self.scan_root).expanduser()

    @property
    def extra_gitlab_hosts(self) -> frozenset[str]:
        return frozenset(
            h.hostname for h in self.extra_hosts.values() if h.forge_type == "gitlab"
        )

    @property
    def extra_github_hosts(self) -> frozenset[str]:
        return frozenset(
            h.hostname for h in self.extra_hosts.values() if h.forge_type == "github"
        )


def load_config(path: Path | None = None) -> Config:
    """Load config from TOML file. Returns defaults if file doesn't exist."""
    if path is None:
        path = config_dir() / "config.toml"

    if not path.exists():
        return Config()

    with open(path, "rb") as f:
        raw = tomllib.load(f)

    general = raw.get("general", {})
    editor = raw.get("editor", {})
    ui = raw.get("ui", {})
    cache = raw.get("cache", {})
    concurrency = raw.get("concurrency", {})

    extra_hosts = {}
    for key, host_data in raw.get("hosts", {}).items():
        extra_hosts[key] = HostConfig(
            hostname=host_data.get("hostname", ""),
            forge_type=host_data.get("forge_type", ""),
        )

    plugin_config = {}
    for key, plugin_data in raw.get("plugins", {}).items():
        plugin_config[key] = dict(plugin_data)

    return Config(
        scan_root=general.get("scan_root", "~/git"),
        scan_depth=general.get("scan_depth", 5),
        editor_command=editor.get("command", ""),
        external_editor_enabled=editor.get("external_editor_enabled", True),
        theme=ui.get("theme", "monokai"),
        diff_style=ui.get("diff_style", "unified"),
        show_draft_mrs=ui.get("show_draft_mrs", True),
        ascii_mode=ui.get("ascii_mode", False),
        mr_list_ttl=cache.get("mr_list_ttl", 60),
        diff_ttl=cache.get("diff_ttl", 300),
        max_cache_size_mb=cache.get("max_size_mb", 100),
        max_parallel=concurrency.get("max_parallel", 8),
        request_timeout=concurrency.get("request_timeout", 30),
        extra_hosts=extra_hosts,
        plugin_config=plugin_config,
    )
