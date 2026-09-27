"""Tests for auth token resolution."""

import logging
import subprocess
import sys
from unittest.mock import patch

import pytest

from tongs.errors import AuthError
from tongs.forges.auth import (
    _token_from_cli,
    _token_from_netrc,
    refresh_token,
    resolve_token,
)
from tongs.scanner.repo import ForgeType


class TestTokenFromNetrc:
    def test_reads_valid_netrc(self, tmp_path):
        netrc_file = tmp_path / ".netrc"
        netrc_file.write_text(
            "machine gitlab.com\n  login __token__\n  password glpat-test123\n"
        )
        netrc_file.chmod(0o600)
        with patch("tongs.forges.auth.Path.home", return_value=tmp_path):
            token = _token_from_netrc("gitlab.com")
        assert token == "glpat-test123"

    def test_returns_none_when_no_file(self, tmp_path):
        with patch("tongs.forges.auth.Path.home", return_value=tmp_path):
            assert _token_from_netrc("gitlab.com") is None

    def test_returns_none_when_host_not_found(self, tmp_path):
        netrc_file = tmp_path / ".netrc"
        netrc_file.write_text("machine other.com\n  login user\n  password pass\n")
        netrc_file.chmod(0o600)
        with patch("tongs.forges.auth.Path.home", return_value=tmp_path):
            assert _token_from_netrc("gitlab.com") is None

    def test_ignores_default_entry(self, tmp_path):
        """A default entry belongs to another service and is never sent."""
        netrc_file = tmp_path / ".netrc"
        netrc_file.write_text("default\n  login ftpuser\n  password ftp-secret\n")
        netrc_file.chmod(0o600)
        with patch("tongs.forges.auth.Path.home", return_value=tmp_path):
            assert _token_from_netrc("gitlab.com") is None
            assert _token_from_netrc("default") is None

    def test_machine_entry_wins_over_default(self, tmp_path):
        netrc_file = tmp_path / ".netrc"
        netrc_file.write_text(
            "machine gitlab.com\n  login __token__\n  password glpat-host\n"
            "default\n  login ftpuser\n  password ftp-secret\n"
        )
        netrc_file.chmod(0o600)
        with patch("tongs.forges.auth.Path.home", return_value=tmp_path):
            assert _token_from_netrc("gitlab.com") == "glpat-host"
            assert _token_from_netrc("github.com") is None

    def test_parse_error_does_not_quote_the_secret(self, tmp_path):
        """A missing password keyword makes the parser quote the token."""
        netrc_file = tmp_path / ".netrc"
        netrc_file.write_text(
            "machine gitlab.com\n  login __token__\n  zqSECRETzq-value\n"
        )
        netrc_file.chmod(0o600)
        with (
            patch("tongs.forges.auth.Path.home", return_value=tmp_path),
            pytest.raises(AuthError) as excinfo,
        ):
            _token_from_netrc("gitlab.com")
        message = str(excinfo.value)
        assert "SECRET" not in message
        assert "near line" in message
        assert excinfo.value.__cause__ is None
        assert excinfo.value.__suppress_context__ is True

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions only")
    def test_rejects_wrong_permissions(self, tmp_path):
        netrc_file = tmp_path / ".netrc"
        netrc_file.write_text(
            "machine gitlab.com\n  login __token__\n  password secret\n"
        )
        netrc_file.chmod(0o644)
        with (
            patch("tongs.forges.auth.Path.home", return_value=tmp_path),
            pytest.raises(AuthError, match="permissions"),
        ):
            _token_from_netrc("gitlab.com")


class TestResolveToken:
    def test_raises_auth_error_when_no_credentials(self, tmp_path):
        with (
            patch("tongs.forges.auth._token_from_cli", return_value=None),
            patch("tongs.forges.auth._token_from_netrc", return_value=None),
            pytest.raises(AuthError, match="No credentials found"),
        ):
            resolve_token("gitlab.com", ForgeType.GITLAB)

    def test_prefers_cli_over_netrc(self):
        with (
            patch("tongs.forges.auth._token_from_cli", return_value="cli-token"),
            patch("tongs.forges.auth._token_from_netrc", return_value="netrc-token"),
        ):
            token = resolve_token("gitlab.com", ForgeType.GITLAB)
        assert token == "cli-token"

    def test_falls_back_to_netrc(self):
        with (
            patch("tongs.forges.auth._token_from_cli", return_value=None),
            patch("tongs.forges.auth._token_from_netrc", return_value="netrc-token"),
        ):
            token = resolve_token("gitlab.com", ForgeType.GITLAB)
        assert token == "netrc-token"

    def test_error_message_includes_glab_for_gitlab(self):
        with (
            patch("tongs.forges.auth._token_from_cli", return_value=None),
            patch("tongs.forges.auth._token_from_netrc", return_value=None),
            pytest.raises(AuthError, match="glab auth login"),
        ):
            resolve_token("gitlab.com", ForgeType.GITLAB)

    def test_error_message_includes_gh_for_github(self):
        with (
            patch("tongs.forges.auth._token_from_cli", return_value=None),
            patch("tongs.forges.auth._token_from_netrc", return_value=None),
            pytest.raises(AuthError, match="gh auth login"),
        ):
            resolve_token("github.com", ForgeType.GITHUB)


class TestTokenFromCli:
    def test_cli_returns_token_on_success(self):
        result = subprocess.CompletedProcess(
            args=["glab", "config", "get", "token", "--host", "gitlab.com"],
            returncode=0,
            stdout="glpat-abc123\n",
            stderr="",
        )
        with patch("tongs.forges.auth.subprocess.run", return_value=result):
            token = _token_from_cli("gitlab.com", ForgeType.GITLAB)
        assert token == "glpat-abc123"

    def test_cli_returns_none_on_nonzero_exit(self):
        result = subprocess.CompletedProcess(
            args=["glab", "config", "get", "token", "--host", "gitlab.com"],
            returncode=1,
            stdout="",
            stderr="not logged in",
        )
        with patch("tongs.forges.auth.subprocess.run", return_value=result):
            assert _token_from_cli("gitlab.com", ForgeType.GITLAB) is None

    def test_cli_returns_none_on_empty_stdout(self):
        result = subprocess.CompletedProcess(
            args=["glab", "config", "get", "token", "--host", "gitlab.com"],
            returncode=0,
            stdout="   \n",
            stderr="",
        )
        with patch("tongs.forges.auth.subprocess.run", return_value=result):
            assert _token_from_cli("gitlab.com", ForgeType.GITLAB) is None

    def test_cli_returns_none_when_not_installed(self):
        with patch(
            "tongs.forges.auth.subprocess.run",
            side_effect=FileNotFoundError("glab not found"),
        ):
            assert _token_from_cli("gitlab.com", ForgeType.GITLAB) is None

    def test_cli_returns_none_on_timeout(self):
        with patch(
            "tongs.forges.auth.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="glab", timeout=5),
        ):
            assert _token_from_cli("gitlab.com", ForgeType.GITLAB) is None

    def test_cli_gitlab_always_adds_hostname_flag(self):
        result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="token\n", stderr=""
        )
        with patch("tongs.forges.auth.subprocess.run", return_value=result) as mock_run:
            _token_from_cli("gitlab.cee.redhat.com", ForgeType.GITLAB)
        cmd = mock_run.call_args[0][0]
        assert cmd == [
            "glab",
            "config",
            "get",
            "token",
            "--host",
            "gitlab.cee.redhat.com",
        ]

    def test_cli_github_default_host_no_hostname_flag(self):
        result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ghp_token\n", stderr=""
        )
        with patch("tongs.forges.auth.subprocess.run", return_value=result) as mock_run:
            _token_from_cli("github.com", ForgeType.GITHUB)
        cmd = mock_run.call_args[0][0]
        assert cmd == ["gh", "auth", "token"]
        assert "--hostname" not in cmd

    def test_cli_github_enterprise_adds_hostname_flag(self):
        result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ghp_token\n", stderr=""
        )
        with patch("tongs.forges.auth.subprocess.run", return_value=result) as mock_run:
            _token_from_cli("github.corp.com", ForgeType.GITHUB)
        cmd = mock_run.call_args[0][0]
        assert cmd == ["gh", "auth", "token", "--hostname", "github.corp.com"]


class TestRefreshToken:
    def test_gitlab_triggers_glab_refresh_before_resolving(self):
        calls = []
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        def run(cmd, **kwargs):
            calls.append(cmd)
            return ok

        with (
            patch("tongs.forges.auth.subprocess.run", side_effect=run),
            patch("tongs.forges.auth.resolve_token", return_value="fresh") as resolve,
        ):
            token = refresh_token("gitlab.com", ForgeType.GITLAB)
        assert token == "fresh"
        assert calls == [["glab", "auth", "status", "--hostname", "gitlab.com"]]
        resolve.assert_called_once_with("gitlab.com", ForgeType.GITLAB)

    def test_gitlab_resolves_even_when_glab_missing(self):
        with (
            patch("tongs.forges.auth.subprocess.run", side_effect=FileNotFoundError),
            patch("tongs.forges.auth.resolve_token", return_value="netrc-token"),
        ):
            assert refresh_token("gitlab.com", ForgeType.GITLAB) == "netrc-token"

    def test_github_does_not_run_glab(self):
        with (
            patch("tongs.forges.auth.subprocess.run") as run,
            patch("tongs.forges.auth.resolve_token", return_value="gh-token"),
        ):
            assert refresh_token("github.com", ForgeType.GITHUB) == "gh-token"
        run.assert_not_called()

    def test_returns_none_when_no_credentials(self):
        with (
            patch("tongs.forges.auth.subprocess.run", side_effect=FileNotFoundError),
            patch(
                "tongs.forges.auth.resolve_token",
                side_effect=AuthError("No credentials found"),
            ),
        ):
            assert refresh_token("gitlab.com", ForgeType.GITLAB) is None

    def test_parse_error_is_not_logged_verbatim(self, tmp_path, caplog):
        """A malformed .netrc during refresh logs no token text."""
        netrc_file = tmp_path / ".netrc"
        netrc_file.write_text("machine github.com login x zqSECRETzq-value\n")
        netrc_file.chmod(0o600)
        with (
            patch("tongs.forges.auth.Path.home", return_value=tmp_path),
            patch("tongs.forges.auth._token_from_cli", return_value=None),
            patch("tongs.forges.auth._token_from_keyring", return_value=None),
            caplog.at_level(logging.DEBUG, logger="tongs.forges.auth"),
        ):
            assert refresh_token("github.com", ForgeType.GITHUB) is None
        assert "Failed to parse" in caplog.text
        assert "SECRET" not in caplog.text

    def test_refresh_log_is_redacted(self, caplog):
        with (
            patch(
                "tongs.forges.auth.resolve_token",
                side_effect=AuthError("bad token ghp_abcdef123456"),
            ),
            caplog.at_level(logging.DEBUG, logger="tongs.forges.auth"),
        ):
            assert refresh_token("github.com", ForgeType.GITHUB) is None
        assert "abcdef123456" not in caplog.text
        assert "ghp_[REDACTED]" in caplog.text
