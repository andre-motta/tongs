"""Per-user desktop entry ownership and escaping tests."""

from __future__ import annotations

import hashlib
import stat
from pathlib import Path

import pytest

from tongs.desktop.installer.menu import (
    install_user_menu,
    remove_user_menu,
    render_desktop_entry,
)
from tongs.desktop.installer.models import InstallerError, InstallerErrorCode


def test_exec_escapes_hostile_reserved_characters_and_percent_fields() -> None:
    entry = render_desktop_entry(Path('/home/me/Tongs $`\\" %f/bin/tongs'))
    text = entry.decode("ascii")

    assert 'Exec="' in text
    assert "%%f" in text
    assert "\\\\$" in text
    assert "\\\\`" in text
    assert "\\\\\\\\" in text
    assert text.endswith("StartupNotify=true\n")


@pytest.mark.parametrize(
    "value",
    [
        "relative/tongs",
        "/tmp/a=b/tongs",
        "/tmp/é/tongs",
        "/tmp/tab\tpath/tongs",
        "/tmp/control\x01path/tongs",
        "/tmp/delete\x7fpath/tongs",
    ],
)
def test_unrepresentable_console_path_is_rejected(value: str) -> None:
    with pytest.raises(InstallerError) as raised:
        render_desktop_entry(Path(value))

    assert raised.value.code is InstallerErrorCode.INCOMPATIBLE


def test_install_replace_and_remove_require_exact_owned_content(tmp_path: Path) -> None:
    path = tmp_path / "data" / "applications" / "tongs.desktop"
    first_console = Path("/opt/Tongs One/bin/tongs")
    second_console = Path("/opt/Tongs Two/bin/tongs")

    first = install_user_menu(path, first_console, replace_digest=None)

    assert first == hashlib.sha256(path.read_bytes()).hexdigest()
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert install_user_menu(path, first_console, replace_digest=None) == first

    path.write_text("[Desktop Entry]\nName=Unrelated\n")
    with pytest.raises(InstallerError, match="another installation"):
        install_user_menu(path, second_console, replace_digest=first)
    with pytest.raises(InstallerError, match="changed"):
        remove_user_menu(path, expected_digest=first)
    assert path.exists()


def test_owned_entry_can_be_replaced_and_uninstall_is_idempotent(
    tmp_path: Path,
) -> None:
    path = tmp_path / "applications" / "tongs.desktop"
    first = install_user_menu(path, Path("/opt/one/bin/tongs"), replace_digest=None)
    second = install_user_menu(path, Path("/opt/two/bin/tongs"), replace_digest=first)

    assert second != first
    assert remove_user_menu(path, expected_digest=second) is True
    assert remove_user_menu(path, expected_digest=second) is False
