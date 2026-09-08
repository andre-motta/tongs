# Pinned archive builder environment

The archive producer runs inside the image built by `Containerfile.build`. Its
base is the official Fedora 44 image manifest digest
`sha256:f59ce614aba37211165e711d732fd4785f1c0329c4a3a4289cf29e333cf36c52`,
queried from `registry.fedoraproject.org` on 2026-09-08. The container downloads
five exact Fedora NEVRAs, verifies their package bytes against
`builder-rpms.sha256`, installs them with weak dependencies disabled, and then
asserts every compression and JavaScript tool version enforced by
`contract.json`.

Issue 53 should build this image on a disposable GitHub-hosted x86_64 runner and
record the resulting image digest plus the version assertions before using it.
The source checkout, exact npm lock and verified Electron zip are mounted as
inputs. Run `npm ci --ignore-scripts` in each clean source root, then invoke
`python3.12 scripts/build_desktop_archive.py` inside the container. A second
clean root and fresh npm install must reproduce every output byte. Network access
is unnecessary after the exact npm packages and Electron zip are present.

Fedora mirrors may retire an update RPM. Before that happens, issue 53 should
retain the five Fedora-signed RPM bytes in its approved build-input store under
these exact SHA-256 identities. Fedora Koji builds with the same NVRs are an
acceptable source only when every downloaded RPM matches this committed list;
changing a package or base-image digest creates a new toolchain and requires two
new clean builds. No package is silently substituted from a moving repository.

The official Electron input is
`electron-v44.2.0-linux-x64.zip`, SHA-256
`574f7d8cd2a82d77812849729a282b86639b050de120d58b138a126d16b48692`.
The producer verifies that archive before extraction, then verifies all 72
member paths, modes, lengths and hashes against the committed runtime inventory.
