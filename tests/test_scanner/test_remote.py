"""Tests for git remote URL parsing and forge detection."""

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from tongs.scanner.remote import parse_remote_url
from tongs.scanner.repo import ForgeType, Remote, Repo


class TestParseRemoteUrl:
    def test_https_github(self):
        remote = parse_remote_url("origin", "https://github.com/andre-motta/tongs.git")
        assert remote is not None
        assert remote.hostname == "github.com"
        assert remote.repo_path == "andre-motta/tongs"
        assert remote.forge_type == ForgeType.GITHUB

    def test_https_github_no_dot_git(self):
        remote = parse_remote_url("origin", "https://github.com/andre-motta/tongs")
        assert remote is not None
        assert remote.repo_path == "andre-motta/tongs"

    def test_ssh_github(self):
        remote = parse_remote_url("origin", "git@github.com:andre-motta/tongs.git")
        assert remote is not None
        assert remote.hostname == "github.com"
        assert remote.repo_path == "andre-motta/tongs"
        assert remote.forge_type == ForgeType.GITHUB

    def test_https_gitlab(self):
        remote = parse_remote_url(
            "origin", "https://gitlab.com/redhat/rhel-ai/wheels/builder.git"
        )
        assert remote is not None
        assert remote.hostname == "gitlab.com"
        assert remote.repo_path == "redhat/rhel-ai/wheels/builder"
        assert remote.forge_type == ForgeType.GITLAB

    def test_ssh_gitlab(self):
        remote = parse_remote_url(
            "origin", "git@gitlab.com:redhat/rhel-ai/core/tools/alerts.git"
        )
        assert remote is not None
        assert remote.repo_path == "redhat/rhel-ai/core/tools/alerts"
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

    def test_internal_gitlab_auto_detect(self):
        remote = parse_remote_url(
            "origin",
            "https://gitlab.cee.redhat.com/alustosa/app-interface",
        )
        assert remote is not None
        assert remote.forge_type == ForgeType.GITLAB

    def test_altssh_gitlab_normalized(self):
        remote = parse_remote_url(
            "origin", "git@altssh.gitlab.com:redhat/rhel-ai/builder.git"
        )
        assert remote is not None
        assert remote.hostname == "gitlab.com"

    def test_unknown_host_returns_none(self):
        remote = parse_remote_url("origin", "https://bitbucket.org/user/repo.git")
        assert remote is None

    def test_local_path_returns_none(self):
        remote = parse_remote_url("origin", "/home/user/repo")
        assert remote is None

    def test_file_url_returns_none(self):
        remote = parse_remote_url("origin", "file:///home/user/repo")
        assert remote is None

    def test_ssh_protocol_url(self):
        remote = parse_remote_url(
            "origin", "ssh://git@github.com/andre-motta/tongs.git"
        )
        assert remote is not None
        assert remote.hostname == "github.com"
        assert remote.repo_path == "andre-motta/tongs"

    def test_github_enterprise(self):
        remote = parse_remote_url("origin", "https://github.mycompany.com/org/repo.git")
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
    def test_empty_url(self):
        assert parse_remote_url("origin", "") is None

    def test_deeply_nested_gitlab_path(self):
        remote = parse_remote_url(
            "origin",
            "https://gitlab.com/redhat/rhel-ai/core/base-images/app.git",
        )
        assert remote is not None
        assert remote.repo_path == "redhat/rhel-ai/core/base-images/app"

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

    def test_https_with_credentials_stripped(self):
        remote = parse_remote_url(
            "origin", "https://oauth2:glpat-secret@gitlab.com/org/repo.git"
        )
        assert remote is not None
        assert "glpat-secret" not in remote.url
        assert "oauth2" not in remote.url
        assert remote.hostname == "gitlab.com"
        assert remote.repo_path == "org/repo"

    def test_extra_github_hosts(self):
        remote = parse_remote_url(
            "origin",
            "https://git.mycorp.com/org/repo.git",
            extra_github_hosts=frozenset({"git.mycorp.com"}),
        )
        assert remote is not None
        assert remote.forge_type == ForgeType.GITHUB

    def test_scp_url_no_port(self):
        remote = parse_remote_url("origin", "user@gitlab.com:org/repo.git")
        assert remote is not None
        assert remote.hostname == "gitlab.com"
        assert remote.repo_path == "org/repo"


class TestDataclassImmutability:
    def test_remote_is_frozen(self):
        remote = Remote(
            name="origin",
            url="https://github.com/org/repo.git",
            hostname="github.com",
            repo_path="org/repo",
            forge_type=ForgeType.GITHUB,
        )
        with pytest.raises(FrozenInstanceError):
            remote.name = "changed"

    def test_repo_is_frozen(self):
        repo = Repo(path=Path("/tmp/repo"), remotes=())
        with pytest.raises(FrozenInstanceError):
            repo.path = Path("/tmp/other")
