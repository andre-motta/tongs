"""Repository list screen grouped by namespace."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, Header, Tree

from tongs.scanner.repo import ForgeType, Repo


def _forge_icon(repo: Repo) -> str:
    if repo.forge_type == ForgeType.GITLAB:
        return "[blue]GL[/]"
    if repo.forge_type == ForgeType.GITHUB:
        return "[white]GH[/]"
    return "[dim]--[/]"


def _host_suffix(repo: Repo, has_multiple_instances: bool) -> str:
    if not has_multiple_instances or not repo.hostname:
        return ""
    if repo.hostname in ("github.com", "gitlab.com"):
        return ""
    return f" [dim]({repo.hostname})[/]"


class RepoListScreen(Screen):
    """Tree view of discovered repos grouped by namespace/org."""

    BINDINGS = [
        Binding("escape", "go_back", "Back", show=True),
        Binding("q", "go_back", "Back", show=False),
        Binding("ctrl+r", "refresh", "Refresh", show=True),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Tree("Repositories", id="repo-tree")
        yield Footer()

    def on_mount(self) -> None:
        tree = self.query_one("#repo-tree", Tree)
        tree.root.expand()
        self.load_repos()

    def load_repos(self) -> None:
        tree = self.query_one("#repo-tree", Tree)
        tree.root.remove_children()

        repos: list[Repo] = getattr(self.app, "repos", [])
        if not repos:
            tree.root.add_leaf("[dim]No repositories found[/]")
            tree.root.set_label("Repositories (0)")
            return

        tree.root.set_label(f"Repositories ({len(repos)})")

        hostnames = {r.hostname for r in repos if r.hostname}
        has_multiple_instances = len(hostnames) > 1

        namespaces: dict[str, list[Repo]] = {}
        for repo in repos:
            ns = repo.namespace or "(ungrouped)"
            namespaces.setdefault(ns, []).append(repo)

        for ns in sorted(namespaces.keys()):
            ns_repos = namespaces[ns]
            branch = tree.root.add(f"[bold]{ns}[/] ({len(ns_repos)})")
            for repo in ns_repos:
                name = repo.path.name
                icon = _forge_icon(repo)
                suffix = _host_suffix(repo, has_multiple_instances)
                branch.add_leaf(f"{icon} {name}{suffix}", data=repo)
            branch.expand()

    def action_go_back(self) -> None:
        self.app.pop_screen()

    def action_refresh(self) -> None:
        self.load_repos()

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        if event.node.data and isinstance(event.node.data, Repo):
            from tongs.views.mr_list import MRListScreen

            self.app.push_screen(MRListScreen(event.node.data))
