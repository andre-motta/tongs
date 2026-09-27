---
title: Security and signing
description: "What tongs trusts, how it handles forge credentials, and how the desktop installer verifies a release."
lead: What tongs trusts, what it verifies, and what it does not protect against.
---

Report a vulnerability through
[SECURITY.md](https://github.com/andre-motta/tongs/blob/main/SECURITY.md); do not
open a public issue.

## Credentials

tongs never stores forge tokens. It looks one up for each host when it first
needs it, in this order (`resolve_token()` in `src/tongs/forges/auth.py`):

1. The forge CLI credential store: `gh auth token` for GitHub, or
   `glab config get token --host <host>` for GitLab. Enterprise GitHub hosts add
   `--hostname`.
2. `~/.netrc` (`_netrc` on Windows). On POSIX systems tongs refuses the file and
   raises an error if any group or other read or write bit is set. `0600`
   satisfies this.
3. The optional system keyring, looked up under service name `tongs` and the
   hostname. This requires the `keyring` package; if it is not installed, or the
   backend fails, the step is skipped rather than fatal.
4. A forge-specific `AuthError` naming the login command to run.

What follows from this:

- tongs writes no token to disk and puts no token in any tongs configuration
  file. When a token comes from the keyring, the keyring owns it, not tongs.
- Login, logout, and refresh remain the responsibility of the CLI or keyring that
  holds the credential.
- When a request gets a 401, tongs re-resolves the token once through the same
  cascade and retries. For GitLab it first runs `glab auth status --hostname
  <host>`, which makes glab refresh and save an expired OAuth token. A second
  401 is reported as an authentication error, so a rotated token is picked up
  without restarting tongs.
- CLI credential reads run without a shell, capture their output, and time out
  after five seconds. The GitLab refresh check times out after fifteen.
- Tokens live only in process memory and the outgoing `Authorization` header.
  `redact_credentials()` in `src/tongs/errors.py` strips known GitLab and GitHub
  token prefixes and generic `Bearer` and `PRIVATE-TOKEN` values before errors are
  logged or displayed.

## Plugins are trusted code

tongs loads plugins from two independent Python entry-point groups:
`tongs.plugins` for terminal `TongsPlugin` implementations, and
`tongs.desktop_plugins` for desktop `DesktopPluginProvider` implementations with
their packaged ESM, CSS, and help resources. A plugin must be installed into the
same Python environment as tongs.

**An installed plugin is trusted code and runs with your full privileges.** The
plugin contexts, declaration allowlists, resource validation, plugin identifiers,
and UI scoping are isolation boundaries against accidental access. They are not a
sandbox, and they do not contain a hostile installed extension. Both registries
check `[plugins.<name>].enabled` before importing an entry point, so disabling a
plugin prevents its import, but installing one is the trust decision.

There is no plugin marketplace and no in-application installer. Install a plugin
only if you would run its author's code directly.

## Desktop artifact verification

:::note[Beta]
The desktop app is a beta. Each GitHub Release from v1.0.0 on carries its
per-user archive with a release manifest and attestation, and unsigned Fedora
RPMs.
This section describes how `tongs desktop install` and `update` verify the archive. See
[Install the desktop app](/desktop/installation/).
:::

The desktop installer only accepts a release from the fixed repository
`andre-motta/tongs`. The repository, its numeric repository and owner IDs, the
release workflow path, the OIDC issuer, and the tag prefix are compile-time
constants in `src/tongs/desktop/installer/metadata.py`. None of them is
configurable, and no flag relaxes the release, identity, attestation, or hash
checks.

### Release selection

- Only tags matching `v<major>.<minor>.<patch>` are considered. These are the
  core's own release tags: one tag publishes the core to PyPI and the desktop
  assets to the GitHub Release of the same name.
- Draft and prerelease entries are skipped, and an accepted release must report
  `immutable: true`. A mutable release is rejected outright.
- Every asset must carry a `sha256:` digest, be fully uploaded, and have an asset
  URL equal to the exact `api.github.com` URL derived from the repository and the
  asset ID. Duplicate asset names or IDs abort the install.
- Each release must carry both `desktop-manifest-v1.json` and
  `desktop-manifest-v1.sigstore.json`.
- Without `--version`, both `install` and `update` take the release whose
  version equals the installed tongs core, and stop if there is none. With
  `--version`, exactly one release must match.
- Every download is an HTTPS request to an allowlisted GitHub host, size-bounded,
  and checked against the expected byte count and SHA-256.

### Provenance

The manifest is covered by a GitHub-managed attestation: a Sigstore bundle
carrying an in-toto statement with an SLSA provenance predicate, verified against
the Sigstore public-good trust root through `Verifier.production()`.

The installer requires an exact identity rather than merely a valid signature.
For a release tagged `v<version>`, the signing identity must be

```text
https://github.com/andre-motta/tongs/.github/workflows/release-desktop.yml@refs/tags/v<version>
```

issued by `https://token.actions.githubusercontent.com`. The verification policy
additionally requires all of:

- source repository `https://github.com/andre-motta/tongs`, with the expected
  repository and repository-owner identifiers;
- source repository ref `refs/tags/v<version>`;
- source repository digest equal to the commit that the tag resolves to, read
  back from the GitHub tag APIs through a bounded number of indirections;
- build signer URI and build config URI equal to that same workflow identity, and
  build config digest equal to that same commit;
- runner environment `github-hosted` and build trigger `push`.

The statement itself is then validated field by field: the statement type, the
SLSA provenance predicate type, the GitHub workflow build type, the workflow
`ref`, `repository`, and `path`, the internal GitHub parameters, a single
resolved dependency of `git+https://github.com/andre-motta/tongs@refs/tags/<tag>`
with the matching git commit, the builder identity, and a workflow invocation
identifier under that repository's `actions/runs/` prefix. Anything unexpected,
including an extra field, fails verification.

### Hashes

The attestation subjects must be exactly two entries and must match exactly:

- the SHA-256 of the manifest bytes as downloaded, and
- the SHA-256 that the manifest declares for the selected archive.

Independently of the attestation, the release version in the manifest must equal
the version in the tag, the manifest's source commit must equal the commit the
tag resolves to, and the archive asset's size and digest as reported by GitHub
must equal the manifest's values. The downloaded archive is hash-checked against
that same value before it is opened, and the archive contents are validated
against the manifest layout before anything is extracted into a private staging
directory.

### Downgrade protection

The installer records an accepted release watermark locally. A verified release
older than the recorded version is refused with a rollback error. The only way
past it is `tongs desktop install --version <version> --allow-downgrade`;
`--allow-downgrade` is rejected unless an explicit `--version` is also given, so
an unattended install can never silently move backwards.

### What this does not cover

- **First install.** The watermark starts empty, so the first accepted install
  has no previous version to compare against. Downgrade protection begins with
  the second install on a machine.
- **No global freshness.** The watermark is local. It cannot tell you that a
  newer release exists, and it does not detect an attacker who withholds updates
  and serves an older release to a machine that has never installed one.
- **No protection against a compromised source of truth.** Every check above
  reduces to trusting the `andre-motta/tongs` repository and its production
  signing workflow. If either is compromised, an attacker can produce an artifact
  that satisfies the full policy.
- **No transport-only trust.** Conversely, TLS to GitHub alone is never treated
  as sufficient; the attestation and hash checks are what admit an artifact.

### RPM packages

The Fedora RPMs are a separate channel, and the checks above do not apply to
them. The GitHub Release attaches `tongs-desktop`, `python3-tongs`,
`python3-tongs+mcp` and the companion packages they need, and you install them
together with `dnf install ./*.rpm`. They are not GPG signed, and there is no
package repository or COPR channel. The release's `SHA256SUMS` file lists a
checksum for every asset, so you can compare the files you downloaded before
you install them.

## Desktop process boundaries

- Electron main is the privileged boundary. The production window uses a private
  `tongs://app` origin with context isolation, a restrictive content security
  policy, no service workers, and an allowlisted preload bridge. Main accepts IPC
  only from the owning web contents, its main frame, and the exact app document,
  and validates method names, parameter keys, sizes, types, and result shapes.
- The desktop app's Python process (its sidecar) is launched with a trusted absolute interpreter, a verified
  working directory, and fixed arguments. Its protocol uses bounded NDJSON frames,
  a fixed protocol major version, declared capabilities and methods, request
  limits, and opaque connection-local handles.
- The renderer never receives credentials, Python package paths, local checkout
  paths, or resource paths.
- Opening an external link goes through a narrow channel that requires a
  credential-free HTTPS URL. It does not accept another scheme and it is not a
  shell escape.

## Cache and drafts

The cache directory is created privately and its SQLite database with mode
`0600`. Tokens are never part of a cache key or value, and job and stream logs are
excluded from the cache. Clearing the cache removes shared API response entries
only; it does not delete durable review drafts or other user files.
