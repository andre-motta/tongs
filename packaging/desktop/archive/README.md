# Reproducible desktop archive inputs

`scripts/build_desktop_archive.py` produces the Fedora 44 x86_64 per-user
archive consumed by the version-1 desktop artifact and installer contracts.
Every release, compatibility, source, runtime and timestamp value is an explicit
input. The initial `0.5.0` value is an unpublished candidate and is not a tag or
published release declaration.

`BUILD_ENVIRONMENT.md` and `Containerfile.build` define the reproducible hosted
builder. The package hashes are retained independently of Fedora mirror URLs.

The pinned Electron input is the official
`electron-v44.2.0-linux-x64.zip` payload. Its upstream SHA-256 and the exact
extracted file inventory are recorded in
`electron-runtime-44.2.0-linux-x64.json`. The archive replaces the stock
`electron` filename with `runtime/tongs-desktop`, replaces
`resources/default_app.asar` with the production `resources/app.asar`, and
retains the other runtime files. The app ASAR contains only the paths in
`app-asar-paths.txt`; source maps, declarations, tests and development packages
are excluded.

The producer uses `@electron/asar` 4.3.0 from the exact npm lock, Python 3.12.14
with zlib-ng 1.3.1, Node 22.23.1, USTAR headers, level-9 raw DEFLATE, and a fixed
gzip header. The contract checks these tool versions before building. Tar
ownership is `0:0`, names are `root:root`, member order is lexical, and every
tar timestamp and the gzip mtime use `SOURCE_DATE_EPOCH`. The gzip filename is
empty, XFL is `2`, and OS is `255`. Directories and runtime executables are mode
`0755`; data is mode `0644`.

The measured Electron 44.2.0 candidate has 91 archive entries, 80 files,
305,108,733 uncompressed file bytes, and a 228,556,104-byte largest file. The
declared 256-entry, 512 MiB total and 256 MiB per-file limits provide bounded
headroom for this fixed runtime without approaching the S0 hard bounds.

Candidate build example:

```bash
npm --prefix desktop ci --ignore-scripts
.venv/bin/python scripts/build_desktop_archive.py \
  --source-root . \
  --electron-archive /path/to/electron-v44.2.0-linux-x64.zip \
  --output-dir /path/to/output \
  --release-version 0.5.0 \
  --core-minimum 0.4.2-dev.183 \
  --core-maximum-exclusive 0.5.0 \
  --source-commit FULL_GIT_SHA \
  --source-date-epoch UNIX_SECONDS
```

Run that sequence from two clean source roots with the same verified Electron
ZIP archive and pinned toolchain. Compare the two `SHA256SUMS` files and every
named output byte-for-byte. The producer refuses an unlisted Electron file,
changed runtime mode or digest, lockfile drift, compiler drift, an unsafe source
link, an incomplete payload, or a nonempty output directory.

The compatibility values accept the exact development wheel used for candidate
testing. Release work must revalidate them against the production core build and
tag. The external manifest remains unsigned here; release CI owns its official
GitHub attestation and publication.
