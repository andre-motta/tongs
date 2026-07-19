# Security Policy

## Supported Versions

tongs is in early development (pre-1.0). Security fixes are applied to the latest release only.

| Version | Supported |
|---------|-----------|
| Latest  | Yes       |
| Older   | No        |

## Auth Model

tongs never stores authentication tokens. It delegates all credential management to the `gh` and `glab` CLI tools, reading tokens at runtime via `gh auth token` and `glab auth token`. If neither CLI is available, it falls back to reading `.netrc` entries with strict file permission checks.

This means:

- No tokens are written to disk by tongs.
- No tokens appear in tongs configuration files.
- Credential lifecycle (login, logout, refresh) is handled entirely by the upstream CLI tools.

## Reporting a Vulnerability

If you discover a security vulnerability, please report it privately. **Do not open a public issue.**

To report:

1. Go to [Security Advisories](https://github.com/andre-motta/tongs/security/advisories) on the tongs GitHub repository.
2. Click **"New draft security advisory"**.
3. Fill in the details, including steps to reproduce if possible.

The maintainer will acknowledge receipt within 48 hours and work with you on a fix. You will be credited in the advisory unless you prefer otherwise.

## Scope

The following are in scope for security reports:

- Token leakage through logs, error messages, or cache files
- Unsafe handling of `.netrc` credentials
- Path traversal or arbitrary file access via the repo scanner
- Remote code execution through malicious git remotes or forge API responses
- Vulnerabilities in dependencies that are exploitable through tongs

Out of scope:

- Vulnerabilities in `gh`, `glab`, or other external CLI tools (report those upstream)
- Issues requiring physical access to the machine
- Social engineering attacks
