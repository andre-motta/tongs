# Pinned archive builder environment

The archive producer runs inside the image built by `Containerfile.build`. Its
base is the official Fedora 44 image manifest digest
`sha256:f59ce614aba37211165e711d732fd4785f1c0329c4a3a4289cf29e333cf36c52`,
queried from `registry.fedoraproject.org` on 2026-09-08.

The builder installs the complete RPM closure that the base image lacks for
Python 3.12, zlib-ng-compat and Node 22. `builder-rpms.nevra` names every
package as `NAME EPOCH:VERSION-RELEASE ARCH`, and `builder-rpms.sha256` holds
the SHA-256 of each package file. The closure is 13 Fedora-signed RPMs:

| Package | EPOCH:VERSION-RELEASE | Arch |
|---|---|---|
| `expat` | `0:2.8.3-1.fc44` | x86_64 |
| `libb2` | `0:0.98.1-15.fc44` | x86_64 |
| `libuv` | `1:1.52.1-2.fc44` | x86_64 |
| `mpdecimal` | `0:4.0.1-3.fc44` | x86_64 |
| `nodejs22` | `1:22.23.1-2.fc44` | x86_64 |
| `nodejs22-bin` | `1:22.23.1-2.fc44` | noarch |
| `nodejs22-libs` | `1:22.23.1-2.fc44` | x86_64 |
| `nodejs22-npm` | `1:10.9.8-1.22.23.1.2.fc44` | noarch |
| `nodejs22-npm-bin` | `1:22.23.1-2.fc44` | noarch |
| `python-pip-wheel` | `0:26.0.1-3.fc44` | noarch |
| `python3.12` | `0:3.12.14-1.fc44` | x86_64 |
| `python3.12-libs` | `0:3.12.14-1.fc44` | x86_64 |
| `zlib-ng-compat` | `0:2.3.3-3.fc44` | x86_64 |

The base image already ships the pinned `zlib-ng-compat`; it stays in the list
so its bytes are verified and its version is asserted after installation.

`Containerfile.build` has two stages. The `fetch` stage is the only step that
needs the network: it runs `dnf download` for each exact committed NEVRA and
fails unless `sha256sum --check --strict` passes and the downloaded file set
equals the committed list, with nothing missing or extra. The final stage
copies those verified files, checks them again, verifies their Fedora OpenPGP
signatures against the base image's trusted RPM keyring, compares their
embedded NEVRAs with `builder-rpms.nevra`, and installs them with
`dnf install --disablerepo='*'`, local-package signature checking and weak
dependencies disabled. No live repository is consulted during installation,
so a dependency Fedora later updates or retires cannot enter the builder. The
stage then asserts that every committed NEVRA is installed, records the whole
installed package set in `/usr/share/tongs-archive-builder/rpm-qa.txt`, and
asserts every compression and JavaScript tool version enforced by
`contract.json`.

Build the image with `packaging/desktop/archive` as its context:

```bash
docker build --file Containerfile.build --tag tongs-archive-builder:local .
```

The narrow, manually dispatched `.github/workflows/desktop-archive.yml`
validation job builds this
image with Podman on a disposable GitHub-hosted x86_64 runner. It records the
image identity and asserted tool versions, downloads the exact official Electron
zip, and uses `git archive` to prepare two clean source roots. Each root gets an
independent `npm ci --ignore-scripts` and producer invocation in a fresh
container. The job compares every output byte and retains one complete output,
both checksum lists, source and Electron inputs, and toolchain evidence for 14
days. It has read-only repository permissions and does not publish an archive.
The retained evidence also includes the exact builder RPM NEVRA, SHA-256 check,
and `rpmkeys --checksig --verbose` results for all 13 RPMs, copied from the
built image.

Run the same proof on a compatible disposable host with:

```bash
TONGS_HEAD_SHA=$(git rev-parse HEAD) \
  packaging/desktop/archive/run_hosted.sh \
  --source-root "$PWD" \
  --output-dir /path/to/new/evidence-directory
```

The packaging lane of the CI aggregate (the `archive` job in
`desktop-production.yml`) and `release-desktop.yml` call the same
`run_hosted.sh` seam, keeping the exact candidate checkout, pinned inputs,
two-clean-root comparison, and bounded evidence retention intact.

Fedora mirrors may retire an update RPM. The builder downloads the 13
Fedora-signed RPMs with `dnf download` and keeps no retained copy; each must
match these exact SHA-256 identities. Fedora Koji builds with the same NVRs are
an acceptable source only when every downloaded RPM matches this committed
list; changing a package or base-image digest creates a new toolchain and
requires two new clean builds. No package is silently substituted from a
moving repository.

To refresh the closure after changing a pinned package or the base digest,
resolve it inside the pinned base image, then regenerate both lists from the
downloaded files:

```bash
podman run --rm --volume "$PWD/rpms:/out:Z" \
  registry.fedoraproject.org/fedora@sha256:<base digest> \
  dnf download --resolve --setopt=install_weak_deps=False --destdir=/out \
  <each top-level NAME-EPOCH:VERSION-RELEASE.ARCH>
(cd rpms && LC_ALL=C sha256sum *.rpm | LC_ALL=C sort -k2) > builder-rpms.sha256
rpm --query --package --nosignature \
  --queryformat '%{NAME} %{EPOCHNUM}:%{VERSION}-%{RELEASE} %{ARCH}\n' \
  rpms/*.rpm | LC_ALL=C sort > builder-rpms.nevra
```

`dnf download --resolve` adds only the dependencies the base image does not
already provide. Add any top-level package the base image already has, such as
`zlib-ng-compat`, explicitly. Prove the offline install by building the `fetch`
stage with network access and then the whole image with `--network=none`:

```bash
podman build --target fetch --file Containerfile.build --tag tongs-archive-fetch:local .
podman build --network=none --file Containerfile.build --tag tongs-archive-builder:local .
```

The official Electron input is
`electron-v44.2.0-linux-x64.zip`, SHA-256
`574f7d8cd2a82d77812849729a282b86639b050de120d58b138a126d16b48692`.
The producer verifies that archive before extraction, then verifies all 72
member paths, modes, lengths and hashes against the committed runtime inventory.
