"""Tests for configuration loader."""

from pathlib import Path

from tongs.config import (
    Config,
    HostConfig,
    cache_dir,
    config_dir,
    data_dir,
    load_config,
)


class TestConfigDefaults:
    def test_scan_root_default(self):
        cfg = Config()
        assert cfg.scan_root == "~/git"

    def test_scan_depth_default(self):
        cfg = Config()
        assert cfg.scan_depth == 5

    def test_editor_command_default(self):
        cfg = Config()
        assert cfg.editor_command == ""

    def test_external_editor_enabled_default(self):
        cfg = Config()
        assert cfg.external_editor_enabled is True

    def test_theme_default(self):
        cfg = Config()
        assert cfg.theme == "monokai"

    def test_diff_style_default(self):
        cfg = Config()
        assert cfg.diff_style == "unified"

    def test_show_draft_mrs_default(self):
        cfg = Config()
        assert cfg.show_draft_mrs is True

    def test_ascii_mode_default(self):
        cfg = Config()
        assert cfg.ascii_mode is False

    def test_mr_list_ttl_default(self):
        cfg = Config()
        assert cfg.mr_list_ttl == 60

    def test_diff_ttl_default(self):
        cfg = Config()
        assert cfg.diff_ttl == 300

    def test_max_cache_size_mb_default(self):
        cfg = Config()
        assert cfg.max_cache_size_mb == 100

    def test_max_parallel_default(self):
        cfg = Config()
        assert cfg.max_parallel == 8

    def test_request_timeout_default(self):
        cfg = Config()
        assert cfg.request_timeout == 30

    def test_extra_hosts_default(self):
        cfg = Config()
        assert cfg.extra_hosts == {}

    def test_plugin_config_default(self):
        cfg = Config()
        assert cfg.plugin_config == {}

    def test_all_15_fields_present(self):
        cfg = Config()
        field_names = [f.name for f in cfg.__dataclass_fields__.values()]
        assert len(field_names) == 15


class TestLoadConfig:
    def test_nonexistent_path_returns_defaults(self, tmp_path):
        cfg = load_config(tmp_path / "missing.toml")
        assert cfg.scan_root == "~/git"
        assert cfg.scan_depth == 5

    def test_complete_toml(self, tmp_path):
        toml_file = tmp_path / "config.toml"
        toml_file.write_text(
            """\
[general]
scan_root = "/projects"
scan_depth = 3

[editor]
command = "nvim"
external_editor_enabled = false

[ui]
theme = "dracula"
diff_style = "side-by-side"
show_draft_mrs = false
ascii_mode = true

[cache]
mr_list_ttl = 120
diff_ttl = 600
max_size_mb = 200

[concurrency]
max_parallel = 4
request_timeout = 15

[hosts.internal]
hostname = "git.corp.com"
forge_type = "gitlab"

[hosts.gh-enterprise]
hostname = "github.corp.com"
forge_type = "github"

[plugins.jira]
url = "https://jira.corp.com"
"""
        )
        cfg = load_config(toml_file)
        assert cfg.scan_root == "/projects"
        assert cfg.scan_depth == 3
        assert cfg.editor_command == "nvim"
        assert cfg.external_editor_enabled is False
        assert cfg.theme == "dracula"
        assert cfg.diff_style == "side-by-side"
        assert cfg.show_draft_mrs is False
        assert cfg.ascii_mode is True
        assert cfg.mr_list_ttl == 120
        assert cfg.diff_ttl == 600
        assert cfg.max_cache_size_mb == 200
        assert cfg.max_parallel == 4
        assert cfg.request_timeout == 15
        assert "internal" in cfg.extra_hosts
        assert cfg.extra_hosts["internal"].hostname == "git.corp.com"
        assert cfg.extra_hosts["internal"].forge_type == "gitlab"
        assert "gh-enterprise" in cfg.extra_hosts
        assert cfg.extra_hosts["gh-enterprise"].hostname == "github.corp.com"
        assert cfg.extra_hosts["gh-enterprise"].forge_type == "github"
        assert cfg.plugin_config["jira"]["url"] == "https://jira.corp.com"

    def test_partial_toml_falls_back_to_defaults(self, tmp_path):
        toml_file = tmp_path / "config.toml"
        toml_file.write_text(
            """\
[general]
scan_root = "/work"
"""
        )
        cfg = load_config(toml_file)
        assert cfg.scan_root == "/work"
        assert cfg.scan_depth == 5
        assert cfg.editor_command == ""
        assert cfg.theme == "monokai"
        assert cfg.mr_list_ttl == 60
        assert cfg.max_parallel == 8
        assert cfg.extra_hosts == {}
        assert cfg.plugin_config == {}

    def test_empty_toml_returns_defaults(self, tmp_path):
        toml_file = tmp_path / "config.toml"
        toml_file.write_text("")
        cfg = load_config(toml_file)
        assert cfg.scan_root == "~/git"
        assert cfg.scan_depth == 5


class TestScanRootPath:
    def test_expands_tilde(self):
        cfg = Config(scan_root="~/projects")
        assert cfg.scan_root_path == Path.home() / "projects"

    def test_absolute_path_unchanged(self):
        cfg = Config(scan_root="/opt/repos")
        assert cfg.scan_root_path == Path("/opt/repos")


class TestExtraHosts:
    def test_extra_gitlab_hosts_filters_correctly(self):
        cfg = Config(
            extra_hosts={
                "gl1": HostConfig(hostname="gl.corp.com", forge_type="gitlab"),
                "gh1": HostConfig(hostname="gh.corp.com", forge_type="github"),
                "gl2": HostConfig(hostname="gl2.corp.com", forge_type="gitlab"),
            }
        )
        result = cfg.extra_gitlab_hosts
        assert result == frozenset({"gl.corp.com", "gl2.corp.com"})

    def test_extra_github_hosts_filters_correctly(self):
        cfg = Config(
            extra_hosts={
                "gl1": HostConfig(hostname="gl.corp.com", forge_type="gitlab"),
                "gh1": HostConfig(hostname="gh.corp.com", forge_type="github"),
                "gh2": HostConfig(hostname="gh2.corp.com", forge_type="github"),
            }
        )
        result = cfg.extra_github_hosts
        assert result == frozenset({"gh.corp.com", "gh2.corp.com"})

    def test_extra_gitlab_hosts_empty_when_no_gitlab(self):
        cfg = Config(
            extra_hosts={
                "gh1": HostConfig(hostname="gh.corp.com", forge_type="github"),
            }
        )
        assert cfg.extra_gitlab_hosts == frozenset()

    def test_extra_github_hosts_empty_when_no_github(self):
        cfg = Config(
            extra_hosts={
                "gl1": HostConfig(hostname="gl.corp.com", forge_type="gitlab"),
            }
        )
        assert cfg.extra_github_hosts == frozenset()

    def test_extra_hosts_empty_when_no_hosts(self):
        cfg = Config()
        assert cfg.extra_gitlab_hosts == frozenset()
        assert cfg.extra_github_hosts == frozenset()


class TestDirectoryHelpers:
    def test_config_dir_returns_path(self):
        assert isinstance(config_dir(), Path)

    def test_cache_dir_returns_path(self):
        assert isinstance(cache_dir(), Path)

    def test_data_dir_returns_path(self):
        assert isinstance(data_dir(), Path)
