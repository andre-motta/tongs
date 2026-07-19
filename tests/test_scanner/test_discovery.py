"""Tests for repository discovery via filesystem scanning."""

import os
import subprocess
from pathlib import Path

import pytest

from tongs.scanner.discovery import discover_repos, _pick_primary_remote
from tongs.scanner.repo import ForgeType, Remote, Repo


def _init_repo(repo_dir: Path, remotes: dict[str, str]) -> None:
    """Initialize a real git repo with the given remotes."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
    for name, url in remotes.items():
        subprocess.run(["git", "remote", "add", name, url], cwd=repo_dir, check=True)


@pytest.fixture
def mock_repo_tree(tmp_path):
    """Create a mock directory tree with real git repos."""
    _init_repo(
        tmp_path / "org/repo-a",
        {"origin": "https://github.com/org/repo-a.git"},
    )
    _init_repo(
        tmp_path / "group/subgroup/repo-b",
        {"origin": "git@gitlab.com:group/subgroup/repo-b.git"},
    )
    _init_repo(
        tmp_path / "solo-repo",
        {"origin": "https://github.com/user/solo-repo.git"},
    )
    (tmp_path / "not-a-repo").mkdir()
    (tmp_path / "random-file.md").write_text("hello")

    return tmp_path


class TestDiscoverRepos:
    def test_finds_repos_in_tree(self, mock_repo_tree):
        repos = discover_repos(mock_repo_tree)
        assert len(repos) == 3

    def test_skips_non_repo_dirs(self, mock_repo_tree):
        repos = discover_repos(mock_repo_tree)
        paths = {r.path.name for r in repos}
        assert "not-a-repo" not in paths

    def test_respects_max_depth(self, mock_repo_tree):
        repos = discover_repos(mock_repo_tree, max_depth=1)
        paths = {r.path.name for r in repos}
        assert "solo-repo" in paths
        assert "repo-b" not in paths

    def test_repos_sorted_by_display_name(self, mock_repo_tree):
        repos = discover_repos(mock_repo_tree)
        names = [r.display_name for r in repos]
        assert names == sorted(names, key=str.lower)

    def test_nonexistent_root_returns_empty(self, tmp_path):
        repos = discover_repos(tmp_path / "nonexistent")
        assert repos == []

    def test_empty_dir_returns_empty(self, tmp_path):
        repos = discover_repos(tmp_path)
        assert repos == []


class TestPickPrimaryRemote:
    def _remote(self, name: str) -> Remote:
        return Remote(
            name=name,
            url=f"https://github.com/x/{name}.git",
            hostname="github.com",
            repo_path=f"x/{name}",
            forge_type=ForgeType.GITHUB,
        )

    def test_prefers_origin(self):
        remotes = [self._remote("upstream"), self._remote("origin"), self._remote("fork")]
        assert _pick_primary_remote(remotes).name == "origin"

    def test_falls_back_to_upstream(self):
        remotes = [self._remote("upstream"), self._remote("fork")]
        assert _pick_primary_remote(remotes).name == "upstream"

    def test_falls_back_to_first(self):
        remotes = [self._remote("fork"), self._remote("other")]
        assert _pick_primary_remote(remotes).name == "fork"

    def test_empty_list_returns_none(self):
        assert _pick_primary_remote([]) is None


class TestRepoProperties:
    def test_display_name_from_primary(self):
        remote = Remote(
            name="origin",
            url="https://gitlab.com/redhat/rhel-ai/builder.git",
            hostname="gitlab.com",
            repo_path="redhat/rhel-ai/builder",
            forge_type=ForgeType.GITLAB,
        )
        repo = Repo(path=Path("/tmp/builder"), remotes=(remote,), primary_remote=remote)
        assert repo.display_name == "redhat/rhel-ai/builder"
        assert repo.namespace == "redhat/rhel-ai"
        assert repo.forge_type == ForgeType.GITLAB
        assert repo.hostname == "gitlab.com"

    def test_display_name_fallback_to_dirname(self):
        repo = Repo(path=Path("/tmp/my-repo"), remotes=())
        assert repo.display_name == "my-repo"
        assert repo.namespace == ""
        assert repo.forge_type is None


class TestDiscoverReposEdgeCases:
    def test_repo_with_only_local_remote_excluded(self, tmp_path):
        _init_repo(tmp_path / "local-only", {"origin": "/home/user/other-repo"})
        repos = discover_repos(tmp_path)
        assert len(repos) == 0

    def test_repo_with_mixed_remotes(self, tmp_path):
        repo_dir = tmp_path / "mixed"
        _init_repo(repo_dir, {
            "local": "/home/user/local",
            "origin": "https://github.com/org/mixed.git",
        })
        repos = discover_repos(tmp_path)
        assert len(repos) == 1
        assert repos[0].primary_remote.name == "origin"
        assert len(repos[0].remotes) == 1

    def test_multi_remote_picks_origin(self, tmp_path):
        _init_repo(tmp_path / "multi", {
            "upstream": "https://github.com/upstream/repo.git",
            "origin": "https://github.com/user/repo.git",
            "fork": "https://github.com/fork/repo.git",
        })
        repos = discover_repos(tmp_path)
        assert len(repos) == 1
        assert repos[0].primary_remote.name == "origin"
        assert len(repos[0].remotes) == 3

    def test_permission_denied_dir_skipped(self, tmp_path):
        _init_repo(tmp_path / "good-repo", {"origin": "https://github.com/org/good.git"})
        blocked = tmp_path / "blocked"
        blocked.mkdir()
        os.chmod(blocked, 0o000)
        try:
            repos = discover_repos(tmp_path)
            assert len(repos) == 1
            assert repos[0].display_name == "org/good"
        finally:
            os.chmod(blocked, 0o755)

    def test_symlink_to_repo_skipped(self, tmp_path):
        real_repo = tmp_path / "real"
        _init_repo(real_repo, {"origin": "https://github.com/org/real.git"})
        (tmp_path / "link-to-repo").symlink_to(real_repo)
        repos = discover_repos(tmp_path)
        assert len(repos) == 1
        assert repos[0].path == real_repo

    def test_broken_symlink_skipped(self, tmp_path):
        _init_repo(tmp_path / "good", {"origin": "https://github.com/org/good.git"})
        (tmp_path / "broken-link").symlink_to(tmp_path / "nonexistent")
        repos = discover_repos(tmp_path)
        assert len(repos) == 1
