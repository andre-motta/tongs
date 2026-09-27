"""Tests for configuration loader."""

from pathlib import Path

from tongs.config import Config, HostConfig, load_config


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


class TestScanRootPath:
    def test_expands_tilde(self):
        cfg = Config(scan_root="~/projects")
        assert cfg.scan_root_path == Path.home() / "projects"


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

    def test_untyped_or_misspelled_hosts_are_not_admitted(self):
        cfg = Config(
            extra_hosts={
                "x": HostConfig(hostname="x.corp", forge_type=""),
                "y": HostConfig(hostname="y.corp", forge_type="gitlb"),
            }
        )
        assert cfg.extra_gitlab_hosts == frozenset()
        assert cfg.extra_github_hosts == frozenset()
