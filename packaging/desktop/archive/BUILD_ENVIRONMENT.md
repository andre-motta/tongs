# Pinned archive builder environment

The archive producer runs inside the image built by `Containerfile.build`. Its
base is the official Fedora 44 image manifest digest
`sha256:f59ce614aba37211165e711d732fd4785f1c0329c4a3a4289cf29e333cf36c52`,
queried from `registry.fedoraproject.org` on 2026-09-08. The container downloads
five exact Fedora NEVRAs, verifies their package bytes against
`builder-rpms.sha256`, installs them with weak dependencies disabled, and then
asserts every compression and JavaScript tool version enforced by
`contract.json`.

Build the image with `packaging/desktop/archive` as its context:

```bash
docker build --file Containerfile.build --tag tongs-archive-builder:local .
```

The narrow `.github/workflows/desktop-archive.yml` validation job builds this
image with Podman on a disposable GitHub-hosted x86_64 runner. It records the
image identity and asserted tool versions, downloads the exact official Electron
zip, and uses `git archive` to prepare two clean source roots. Each root gets an
independent `npm ci --ignore-scripts` and producer invocation in a fresh
container. The job compares every output byte and retains one complete output,
both checksum lists, source and Electron inputs, and toolchain evidence for 14
days. It has read-only repository permissions and does not publish an archive.

Run the same proof on a compatible disposable host with:

```bash
TONGS_HEAD_SHA=$(git rev-parse HEAD) \
  packaging/desktop/archive/run_hosted.sh \
  --source-root "$PWD" \
  --output-dir /path/to/new/evidence-directory
```

Issue 53 may call this validation seam from its aggregate workflow. It must keep
the exact candidate checkout, pinned inputs, two-clean-root comparison, and
bounded evidence retention intact.

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
