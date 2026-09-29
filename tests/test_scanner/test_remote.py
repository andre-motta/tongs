"""Tests for git remote URL parsing and forge detection."""

import pytest

from tongs.scanner.remote import parse_remote_url
from tongs.scanner.repo import ForgeType


class TestParseRemoteUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "https://github.com/andre-motta/tongs.git",
            "https://github.com/andre-motta/tongs",
        ],
    )
    def test_https_github(self, url):
        remote = parse_remote_url("origin", url)
        assert remote is not None
        assert remote.hostname == "github.com"
        assert remote.repo_path == "andre-motta/tongs"
        assert remote.forge_type == ForgeType.GITHUB

    @pytest.mark.parametrize(
        ("url", "hostname", "repo_path", "forge_type"),
        [
            (
                "git@github.com:andre-motta/tongs.git",
                "github.com",
                "andre-motta/tongs",
                ForgeType.GITHUB,
            ),
            (
                "git@gitlab.com:redhat/rhel-ai/core/tools/alerts.git",
                "gitlab.com",
                "redhat/rhel-ai/core/tools/alerts",
                ForgeType.GITLAB,
            ),
            (
                "user@gitlab.com:org/repo.git",
                "gitlab.com",
                "org/repo",
                ForgeType.GITLAB,
            ),
        ],
    )
    def test_scp_remote(self, url, hostname, repo_path, forge_type):
        remote = parse_remote_url("origin", url)
        assert remote is not None
        assert remote.hostname == hostname
        assert remote.repo_path == repo_path
        assert remote.forge_type == forge_type

    def test_https_gitlab(self):
        remote = parse_remote_url(
            "origin", "https://gitlab.com/redhat/rhel-ai/wheels/builder.git"
        )
        assert remote is not None
        assert remote.hostname == "gitlab.com"
        assert remote.repo_path == "redhat/rhel-ai/wheels/builder"
        assert remote.forge_type == ForgeType.GITLAB

    def test_internal_gitlab(self):
        remote = parse_remote_url(
            "origin",
            "https://gitlab.cee.redhat.com/alustosa/app-interface",
            extra_gitlab_hosts=frozenset({"gitlab.cee.redhat.com"}),
        )
        assert remote is not None
        assert remote.hostname == "gitlab.cee.redhat.com"
        assert remote.forge_type == ForgeType.GITLAB

    def test_unconfigured_gitlab_like_host_not_guessed(self):
        remote = parse_remote_url(
            "origin",
            "https://gitlab.cee.redhat.com/alustosa/app-interface",
        )
        assert remote is None

    def test_altssh_gitlab_normalized(self):
        remote = parse_remote_url(
            "origin", "git@altssh.gitlab.com:redhat/rhel-ai/builder.git"
        )
        assert remote is not None
        assert remote.hostname == "gitlab.com"

    def test_ssh_protocol_url(self):
        remote = parse_remote_url(
            "origin", "ssh://git@github.com/andre-motta/tongs.git"
        )
        assert remote is not None
        assert remote.hostname == "github.com"
        assert remote.repo_path == "andre-motta/tongs"

    def test_unconfigured_github_like_host_not_guessed(self):
        remote = parse_remote_url("origin", "https://github.mycompany.com/org/repo.git")
        assert remote is None

    def test_configured_github_enterprise(self):
        remote = parse_remote_url(
            "origin",
            "https://github.mycompany.com/org/repo.git",
            extra_github_hosts=frozenset({"github.mycompany.com"}),
        )
        assert remote is not None
        assert remote.forge_type == ForgeType.GITHUB

    def test_remote_name_preserved(self):
        remote = parse_remote_url("upstream", "https://github.com/org/repo.git")
        assert remote is not None
        assert remote.name == "upstream"

    def test_url_preserved(self):
        url = "git@github.com:andre-motta/tongs.git"
        remote = parse_remote_url("origin", url)
        assert remote is not None
        assert remote.url == url


class TestParseRemoteUrlEdgeCases:
    def test_hostname_case_insensitive(self):
        remote = parse_remote_url("origin", "https://GitHub.COM/user/repo.git")
        assert remote is not None
        assert remote.hostname == "github.com"
        assert remote.forge_type == ForgeType.GITHUB

    def test_ssh_with_explicit_port(self):
        remote = parse_remote_url("origin", "ssh://git@gitlab.com:2222/org/repo.git")
        assert remote is not None
        assert remote.hostname == "gitlab.com"
        assert remote.repo_path == "org/repo"

    @pytest.mark.parametrize(
        ("url", "secret"),
        [
            ("https://oauth2:glpat-secret@gitlab.com/org/repo.git", "glpat-secret"),
            ("https://ghp_tokenonly@gitlab.com/org/repo.git", "ghp_tokenonly"),
        ],
    )
    def test_https_with_credentials_stripped(self, url, secret):
        remote = parse_remote_url("origin", url)
        assert remote is not None
        assert secret not in remote.url
        assert "oauth2" not in remote.url
        assert remote.hostname == "gitlab.com"
        assert remote.repo_path == "org/repo"

    def test_https_trailing_slash_removed_from_repo_path(self):
        remote = parse_remote_url("origin", "https://gitlab.com/group/repo/")
        assert remote is not None
        assert remote.repo_path == "group/repo"

    @pytest.mark.parametrize(
        "url",
        [
            "https://gitlab.corp:8443/g/r.git",
            "https://user:token@gitlab.corp:8443/g/r.git",
        ],
    )
    def test_https_port_removed_from_configured_hostname(self, url):
        remote = parse_remote_url(
            "origin", url, extra_gitlab_hosts=frozenset({"gitlab.corp"})
        )
        assert remote is not None
        assert remote.hostname == "gitlab.corp"
        assert remote.repo_path == "g/r"
