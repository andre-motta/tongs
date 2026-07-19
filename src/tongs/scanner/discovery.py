"""Discover git repositories by walking the filesystem."""

from __future__ import annotations

import subprocess
from pathlib import Path

from tongs.scanner.remote import parse_remote_url
from tongs.scanner.repo import Remote, Repo


def discover_repos(
    root: Path,
    max_depth: int = 5,
    extra_gitlab_hosts: frozenset[str] = frozenset(),
    extra_github_hosts: frozenset[str] = frozenset(),
) -> list[Repo]:
    """Walk root directory and discover git repositories.

    Scans up to max_depth levels deep looking for .git directories.
    Parses remotes from each repo to determine forge type.
    Skips nested repos (a .git inside another repo's working tree).
    """
    root = root.expanduser().resolve()
    if not root.is_dir():
        return []

    repos: list[Repo] = []
    seen_git_dirs: set[Path] = set()

    for git_dir in _find_git_dirs(root, max_depth):
        repo_path = git_dir.parent
        if repo_path in seen_git_dirs:
            continue

        if _is_nested_in(repo_path, seen_git_dirs):
            continue

        seen_git_dirs.add(repo_path)

        remotes = _read_remotes(repo_path, extra_gitlab_hosts, extra_github_hosts)
        if not remotes:
            continue

        primary = _pick_primary_remote(remotes)
        repos.append(
            Repo(path=repo_path, remotes=tuple(remotes), primary_remote=primary)
        )

    repos.sort(key=lambda r: r.display_name.lower())
    return repos


def _find_git_dirs(root: Path, max_depth: int) -> list[Path]:
    """Find all .git directories under root up to max_depth."""
    git_dirs: list[Path] = []

    def _walk(directory: Path, depth: int) -> None:
        if depth > max_depth:
            return

        try:
            entries = sorted(directory.iterdir())
        except PermissionError:
            return

        for entry in entries:
            try:
                is_dir = entry.is_dir()
            except PermissionError:
                continue
            if not is_dir:
                continue
            if entry.is_symlink():
                continue
            if entry.name == ".git":
                git_dirs.append(entry)
                return
            if entry.name.startswith("."):
                continue
            _walk(entry, depth + 1)

    _walk(root, 0)
    return git_dirs


def _is_nested_in(path: Path, known_repos: set[Path]) -> bool:
    """Check if path is inside an already-discovered repo."""
    for parent in path.parents:
        if parent in known_repos:
            return True
    return False


def _read_remotes(
    repo_path: Path,
    extra_gitlab_hosts: frozenset[str],
    extra_github_hosts: frozenset[str],
) -> list[Remote]:
    """Read git remotes from a repository."""
    try:
        result = subprocess.run(
            ["git", "remote", "-v"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []

    if result.returncode != 0:
        return []

    remotes: dict[str, Remote] = {}
    for line in result.stdout.strip().splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue

        name, url = parts[0], parts[1]
        if name in remotes:
            continue

        remote = parse_remote_url(name, url, extra_gitlab_hosts, extra_github_hosts)
        if remote is not None:
            remotes[name] = remote

    return list(remotes.values())


def _pick_primary_remote(remotes: list[Remote]) -> Remote | None:
    """Pick the primary remote. Prefer 'origin', then 'upstream', then first."""
    by_name = {r.name: r for r in remotes}
    return (
        by_name.get("origin")
        or by_name.get("upstream")
        or (remotes[0] if remotes else None)
    )
