"""Dialog that opens a configured-host project without cloning it."""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, Static

from tongs.config import app_config
from tongs.scanner.remote import parse_repository_location
from tongs.services.errors import ServiceError, ServiceErrorCode
from tongs.tui_services import TUIOpenedRepository

_GROUP = "open-repository"

USAGE_MESSAGE = (
    "Enter a host and project, such as github.com/owner/project, or paste its URL."
)


def open_error_message(error: ServiceError, hostname: str, project_path: str) -> str:
    """Say why a project could not be opened, in terms of what was typed."""
    if error.code is ServiceErrorCode.INVALID_INPUT:
        return USAGE_MESSAGE
    if error.code is ServiceErrorCode.RESOURCE_NOT_ISSUED:
        return (
            f"{hostname} is not a configured forge host. "
            "Add it to config.toml to open its projects."
        )
    # Forges answer a project you cannot read as if it did not exist.
    if error.code in {ServiceErrorCode.NOT_FOUND, ServiceErrorCode.PERMISSION_DENIED}:
        return (
            f"{hostname}/{project_path} was not found, or you do not have access to it."
        )
    return error.message


class OpenRepositoryScreen(ModalScreen[TUIOpenedRepository | None]):
    """Ask for a project, check it on its forge, and return what to open.

    The dialog stays open with the reason when the project cannot be opened,
    so a typo can be fixed in place.
    """

    DEFAULT_CSS = """
    OpenRepositoryScreen {
        align: center middle;
    }
    OpenRepositoryScreen > Vertical {
        width: 80%;
        max-width: 90;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    OpenRepositoryScreen #open-repo-title {
        text-style: bold;
    }
    OpenRepositoryScreen #open-repo-help {
        color: $text-muted;
        margin-bottom: 1;
    }
    OpenRepositoryScreen #open-repo-message {
        height: auto;
        margin-top: 1;
    }
    OpenRepositoryScreen #open-repo-message.error {
        color: $warning;
    }
    """

    BINDINGS: ClassVar[list] = [
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Open repository", id="open-repo-title")
            yield Static(
                "Browse a project on a configured host without cloning it. "
                "It stays in the repository list until you remove it or quit.",
                id="open-repo-help",
            )
            yield Input(
                placeholder="github.com/owner/project or a URL",
                id="open-repo-input",
            )
            yield Static("", id="open-repo-message")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#open-repo-input", Input).focus()

    def action_cancel(self) -> None:
        self.workers.cancel_group(self, _GROUP)
        self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        config = app_config(self.app)
        location = parse_repository_location(
            event.value,
            extra_gitlab_hosts=config.extra_gitlab_hosts,
            extra_github_hosts=config.extra_github_hosts,
        )
        if location is None:
            self._show(USAGE_MESSAGE, error=True)
            return
        hostname, project_path = location
        event.input.disabled = True
        self._show(f"Checking {hostname}/{project_path}...")
        self._open(hostname, project_path)

    @work(exclusive=True, group=_GROUP)
    async def _open(self, hostname: str, project_path: str) -> None:
        try:
            result = await self.app.services.open_repository(hostname, project_path)
        except ServiceError as error:
            self._show(open_error_message(error, hostname, project_path), error=True)
            field = self.query_one("#open-repo-input", Input)
            field.disabled = False
            field.focus()
            return
        self.dismiss(result)

    def _show(self, message: str, *, error: bool = False) -> None:
        status = self.query_one("#open-repo-message", Static)
        # What was typed is shown literally, never as markup.
        status.update(Text(message))
        status.set_class(error, "error")
