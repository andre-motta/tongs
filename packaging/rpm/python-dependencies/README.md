# Fedora 44 system-Python companion RPM inputs

This directory prepares the Python dependency RPM slice consumed by issue #52.
It is independent of the Electron archive and does not define the final Tongs or
desktop RPM.

`manifest.json` records the Fedora target, Python 3.12 minimum, expected Fedora
providers, source-built companion frontier, source hashes, licenses, and build
order. Each companion also declares its exact binary RPM name, epoch, release,
and architecture in `binary_rpm`, plus a sibling `version` field, for the
issue 52 consumer allowlist.
`prepare_sources.py` downloads only those exact source distributions,
checks their byte counts and SHA-256 hashes, and prepares the Cargo inputs for
`rfc3161-client`. The Rust preparation preserves its upstream `Cargo.lock`,
records every package source/checksum/license, and creates a deterministic vendor
archive. The RPM spec removes the upstream vendored-OpenSSL feature so the build
links Fedora's OpenSSL provider. Target-resolved Cargo packages must have license
metadata and actual license text. Their inventory and texts ship in the binary
RPM, while the complete vendored source archive remains in the source RPM.

The GitHub Actions workflow is the supported execution environment. It uses a
disposable Fedora 44 container on a GitHub-hosted runner to refresh `repoquery`
and prepare sources. It creates SRPMs offline, provisions one clean Fedora image
per SRPM with `dnf builddep`, rebuilds each SRPM with networking disabled, and
installs the result with DNF in another clean Fedora container. No install step
uses pip or a private Python runtime.
The retained artifact includes provider resolution, source and Cargo inventories,
RPM metadata, DNF logs, smoke results, and SHA-256 sums.

Local tests validate the manifest and evidence parsers without invoking DNF,
Podman, RPM, Cargo, or the network:

```bash
.venv/bin/pytest tests/packaging/rpm/python-dependencies -v
```
