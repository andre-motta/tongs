# Security Policy

## Supported Versions

tongs is released from the 1.x series and follows semantic versioning. Security fixes are applied to the latest 1.x release only; there are no backports to earlier releases, so upgrading to the latest release is the supported remedy.

| Version | Supported |
|---------|-----------|
| Latest  | Yes       |
| Older   | No        |

## Auth Model

tongs never stores authentication tokens. `resolve_token()` in `src/tongs/forges/auth.py` resolves one lazily per host through a four-step cascade:

1. The forge CLI credential store, via `gh auth token` or `glab auth token --hostname <host>`.
2. `~/.netrc` (`_netrc` on Windows), rejected on POSIX systems if any group or other read or write bit is set.
3. The optional system keyring, under service name `tongs` and the hostname. This requires the optional `keyring` package; when it is absent or its backend fails, the step is skipped rather than fatal.
4. A forge-specific `AuthError` naming the login command to run.

This means:

- No tokens are written to disk by tongs. A token held in the system keyring is owned by the keyring, not by tongs.
- No tokens appear in tongs configuration files, cache keys or values, protocol frames, or renderer state.
- Credential lifecycle (login, logout, refresh) is handled entirely by the upstream CLI tools or by the keyring backend.
- CLI credential reads use an argument vector with no shell, captured output, and a five-second timeout, and `redact_credentials()` in `src/tongs/errors.py` strips known token prefixes before errors are logged or displayed.

## Plugin Trust Boundary

tongs loads plugins from two independent Python entry-point groups, `tongs.plugins` for terminal plugins and `tongs.desktop_plugins` for desktop providers and their packaged ESM, CSS, and help resources. A plugin must be installed into the same Python environment as tongs.

An installed plugin is trusted code and runs with your full privileges. Plugin contexts, declaration allowlists, resource validation, plugin identifiers, and UI scoping are isolation boundaries against accidental access, not a sandbox, and they do not contain a hostile installed extension. Both registries honor `[plugins.<name>].enabled` before importing an entry point, so disabling a plugin prevents its import, but installing one is the trust decision. There is no plugin marketplace and no in-application installer.

A vulnerability that lets an *unprivileged* input cause plugin code to load or execute is in scope. A plugin that the user chose to install behaving maliciously is not.

## Artifact Signing and Verification

No desktop release has been published. There is no signed archive, production tag, attestation bundle, release asset, or signed RPM available today. The verification described here is implemented in `src/tongs/desktop/installer/` and is the contract the first production release must satisfy.

The desktop installer accepts releases only from the fixed `andre-motta/tongs` repository. The repository, its numeric repository and owner identifiers, the release workflow path, the GitHub OIDC issuer, and the `desktop-v` tag prefix are compile-time constants in `src/tongs/desktop/installer/metadata.py`. None is configurable and no flag relaxes the release, identity, attestation, or hash checks.

- Only immutable, non-draft, non-prerelease releases whose tag matches `desktop-v<major>.<minor>.<patch>` are eligible, and every asset must be fully uploaded with a `sha256:` digest and an asset URL equal to the exact `api.github.com` URL for that asset.
- The release manifest is covered by a GitHub-managed attestation: a Sigstore bundle carrying an in-toto statement with an SLSA provenance predicate, verified against the Sigstore public-good trust root.
- Verification requires an exact identity, not merely a valid signature: the workflow `.github/workflows/release-desktop.yml` at `refs/tags/desktop-v<version>` in that repository, issued by `https://token.actions.githubusercontent.com`, on a `github-hosted` runner from a `push` trigger, with the source repository digest equal to the commit that the tag resolves to.
- The attestation subjects must match exactly two hashes: the SHA-256 of the downloaded manifest and the manifest's declared SHA-256 for the selected archive. The archive is hash-checked before it is opened and validated against the manifest layout before extraction.
- The installer keeps a local accepted-release watermark. A verified but older release is refused unless the user passes both `--version` and `--allow-downgrade`.

Known limits, stated so they are not mistaken for guarantees:

- The watermark starts empty, so the first install on a machine has nothing to compare against.
- The watermark is local. It provides no global release freshness and cannot detect withheld updates.
- Every check reduces to trusting the `andre-motta/tongs` repository and its production signing workflow. Compromise of either defeats the policy.

See [docs/reference/security.md](docs/reference/security.md) for the full statement.

## Reporting a Vulnerability

If you discover a security vulnerability, please report it privately. **Do not open a public issue.**

To report:

1. Go to [Security Advisories](https://github.com/andre-motta/tongs/security/advisories) on the tongs GitHub repository.
2. Click **"New draft security advisory"**.
3. Fill in the details, including steps to reproduce if possible.

The maintainer will acknowledge receipt within 48 hours and work with you on a fix. You will be credited in the advisory unless you prefer otherwise.

## Scope

The following are in scope for security reports, in the terminal application, the desktop application, the Python sidecar, and the desktop installer:

- Token leakage through logs, error messages, cache files, protocol frames, renderer state, or desktop exports
- Unsafe handling of `.netrc` or keyring credentials
- Path traversal or arbitrary file access via the repo scanner, the desktop editor export path, or archive extraction
- Remote code execution through malicious git remotes, forge API responses, or release metadata
- A bypass of the desktop installer's release, identity, attestation, hash, or downgrade checks, or acceptance of an artifact that does not satisfy them
- Escape from the Electron main, preload, or sidecar protocol boundary, including IPC that a non-owning frame can reach, or widening of the external-link channel into arbitrary shell, clipboard, or filesystem authority
- Loading or executing plugin code without the user having installed and enabled that plugin
- Vulnerabilities in dependencies that are exploitable through tongs

Out of scope:

- Vulnerabilities in `gh`, `glab`, or other external CLI tools (report those upstream)
- Behavior of a plugin the user chose to install, which is trusted code by design
- Issues requiring physical access to the machine
- Social engineering attacks
