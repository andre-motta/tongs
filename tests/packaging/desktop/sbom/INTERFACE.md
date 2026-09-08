# Desktop SBOM caller identity

The issue #53 caller must compute these values independently from its exact,
checked-out candidate before invoking `scripts/build_desktop_sbom.py`:

- the full source commit and tree IDs;
- the SHA-256 of a fresh `git archive` source tar;
- the checked commit timestamp used as `SOURCE_DATE_EPOCH`;
- the desktop archive SHA-256; and
- the Electron ZIP SHA-256 from the reviewed, pinned
  `packaging/desktop/archive/electron-runtime-<version>-linux-x64.json` source
  configuration.

Pass them through the corresponding `--expected-*` arguments. Do not derive
these expected values from the incoming transfer manifest, build provenance, or
prepared inventory. The generator verifies the retained source tar, selected
source files, source package lock, producer metadata, and actual Electron ZIP
against those caller-owned identities.

The final issue #53 assembly must regenerate the SBOM for its exact paired
source and desktop archive. Historical candidate evidence is diagnostic only.
