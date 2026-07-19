"""Tests for error hierarchy and credential redaction."""

from tongs.errors import (
    AuthError,
    ConfigError,
    ConflictError,
    ForgeError,
    ForgePermissionError,
    NetworkError,
    NotFoundError,
    RateLimitError,
    redact_credentials,
)


class TestRedactCredentials:
    def test_gitlab_pat(self):
        assert (
            redact_credentials("token: glpat-abc123DEF_xyz")
            == "token: glpat-[REDACTED]"
        )

    def test_gitlab_deploy_token(self):
        assert redact_credentials("gldt-abc123") == "gldt-[REDACTED]"

    def test_gitlab_ci_job_token(self):
        assert redact_credentials("glcbt-64_abc123") == "glcbt-64_[REDACTED]"

    def test_gitlab_pipeline_trigger(self):
        assert redact_credentials("glptt-abc123def") == "glptt-[REDACTED]"

    def test_gitlab_feed_token(self):
        assert redact_credentials("glft-abc123") == "glft-[REDACTED]"

    def test_gitlab_scim_token(self):
        assert redact_credentials("glsoat-abc123") == "glsoat-[REDACTED]"

    def test_gitlab_incoming_mail(self):
        assert redact_credentials("glimt-abc123") == "glimt-[REDACTED]"

    def test_gitlab_oauth(self):
        assert redact_credentials("gloas-abc123") == "gloas-[REDACTED]"

    def test_github_classic_pat(self):
        assert redact_credentials("ghp_abc123DEF456") == "ghp_[REDACTED]"

    def test_github_oauth(self):
        assert redact_credentials("gho_abc123DEF456") == "gho_[REDACTED]"

    def test_github_app_installation(self):
        assert redact_credentials("ghs_abc123DEF456") == "ghs_[REDACTED]"

    def test_github_user_to_server(self):
        assert redact_credentials("ghu_abc123DEF456") == "ghu_[REDACTED]"

    def test_github_fine_grained(self):
        assert redact_credentials("github_pat_abc123DEF") == "github_pat_[REDACTED]"

    def test_bearer_token(self):
        assert redact_credentials("Authorization: Bearer eyJhbGciOiJIUzI") == (
            "Authorization: Bearer [REDACTED]"
        )

    def test_private_token_header(self):
        assert (
            redact_credentials("PRIVATE-TOKEN: glpat-abc123")
            == "PRIVATE-TOKEN: [REDACTED]"
        )

    def test_no_token_unchanged(self):
        assert (
            redact_credentials("normal text with no secrets")
            == "normal text with no secrets"
        )

    def test_empty_string(self):
        assert redact_credentials("") == ""

    def test_multiple_tokens(self):
        text = "gitlab: glpat-abc123 github: ghp_def456"
        result = redact_credentials(text)
        assert "abc123" not in result
        assert "def456" not in result
        assert "glpat-[REDACTED]" in result
        assert "ghp_[REDACTED]" in result

    def test_token_in_url(self):
        result = redact_credentials("https://x-token:ghp_secret123@github.com/org/repo")
        assert "secret123" not in result

    def test_prefix_alone_not_mangled(self):
        assert redact_credentials("glpat-") == "glpat-"


class TestErrorHierarchy:
    def test_all_errors_inherit_from_forge_error(self):
        for cls in [
            AuthError,
            RateLimitError,
            NetworkError,
            NotFoundError,
            ConflictError,
            ForgePermissionError,
            ConfigError,
        ]:
            assert issubclass(cls, ForgeError)

    def test_rate_limit_error_has_retry_after(self):
        err = RateLimitError("rate limited", retry_after=30)
        assert err.retry_after == 30
        assert str(err) == "rate limited"

    def test_rate_limit_error_default_retry_after(self):
        err = RateLimitError("rate limited")
        assert err.retry_after is None
