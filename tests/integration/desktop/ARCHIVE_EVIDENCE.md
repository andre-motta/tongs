# Desktop archive lifecycle evidence adapter

`archive_evidence.py` is the issue #53 boundary around the existing reproducible
archive producer and the existing unsigned transfer validation. It is not a
packager, a receipt schema, or a signing policy. Nothing in it verifies a
certificate or a signature, so a passing receipt is never a signing claim.

The producer callable and the `produce` command require an exact clean subject
checkout, the retained transfer root, caller-owned expectations, caller-owned
receipt policy, and a fresh output path. The consumer callable and the `consume`
command read every semantic input through the issue #130 bound reader and
independently revalidate the retained transfer before returning any binding.

## Four separate identities

Caller policy fixes all four and none is ever read from the report under check.

| Identity | Fixes |
| --- | --- |
| Subject source | commit, tree, `git archive` digest, commit epoch |
| Subject archive | archive name, artifact ID, digest, release version, release and install manifest digests, license inventory digest, Electron version, reviewed Electron configuration and upstream archive digests |
| Trusted tool | adapter, transfer validator, receipt reader program digests and the artifact contract package digest |
| Upstream transfer execution | repository, repository IDs, ref, event, run ID, run attempt of the authentic run that produced the retained tree |

The receipt policy is separate again and fixes the current verification
repository, run, attempt, environment, provenance, and check ID. That separation
is what permits an explicitly labeled diagnostic over a retained historical
transfer without claiming that the current adapter came from the old source.

Event and ref are validated as a pair by the issue #138
`UnsignedTransferIdentity`, so a `pull_request` transfer cannot be relabeled as a
`push`, and a branch ref cannot be presented as a merge ref. Structural validity
of that type is not provenance; the caller supplies every expected value.

## Reused, not reimplemented

- `UnsignedTransferIdentity` and `validate_transfer_manifest` from issue #138 own
  the exact transfer file set, the manifest binding, the archive contract, and
  the retained license bytes. This adapter contains no tar reader and no second
  transfer parser.
- `ReceiptPolicy`, `validate_receipt`, `validate_receipt_file`, `BoundFile` and
  `read_bound_bytes` from issues #110 and #130 own receipt structure and every
  bounded read of a staged file.
- `parse_release_manifest`, `parse_install_manifest`, `validate_manifest_pair`,
  `INSTALL_MANIFEST_PATH`, `FIXED_LAUNCHER_PATH`, `FIXED_APP_ASAR_PATH` and
  `FIXED_LICENSE_INVENTORY_PATH` from `tongs.desktop.artifact_contract` own the
  manifest semantics. `RELEASE_MANIFEST_NAME` and `InstallerLimits` supply the
  release manifest name and the large file bound.
- Source-defined values come from the subject checkout, not from literals here.
  `packaging/desktop/archive/contract.json` supplies the toolchain and the
  compression policy, and it names the reviewed Electron runtime configuration.

Source lock, npm, and SPDX parsing stay with issues #129 and #135. The archive
receipt feeds the SBOM adapter, so this adapter never reads an SBOM receipt and
never invokes that adapter.

## Five stages

| Stage | Work |
| --- | --- |
| `source-admission` | Trusted tool origins and digests, then the independently derived clean `HEAD`, tree, commit epoch, and bounded streamed `git archive` digest compared with caller policy |
| `producer-transfer-validation` | Preflight of file kinds, paths, and declared and observed sizes against per-class bounds before any hashing, then the issue #138 validator over the exact file set, manifest, archive contract, and retained license bytes |
| `archive-contract-license-validation` | Release and install manifest pairing, launcher and application archive declarations, runtime inventory equality with the install declarations, and license inventory closure |
| `reproducibility-output-binding` | Build A and build B lists byte identical, then their complete basename to digest map equal to the validated final archive directory records, including `SHA256SUMS` |
| `source-tool-metadata-validation` | Provenance, prepared source inventory, `inputs.env` and `toolchain.txt` cross-checked against the caller epoch, the archive, install and release identities, the reviewed Electron configuration, and the source-defined toolchain, then the trusted tool identity again |

Matching build A and build B lists alone are insufficient. Lists that agree with
each other but describe outputs unrelated to the validated archive directory, or
that omit the checksum file, are rejected.

## Published evidence root

The producer writes into a temporary sibling directory, runs the separate
consumer against it, and only then renames it into place. Any failure removes
the temporary directory, so no acceptably published success receipt survives a
failure.

| Path | Receipt kind and meaning |
| --- | --- |
| `reports/archive-evidence.json` | `artifact-lifecycle-v1` report with the five exact successful stages |
| `inputs/candidate-attestation-transfer-v1.json` | Immutable small copy of the validated transfer manifest |
| `archive-receipt.json` | Existing issue #110 receipt over the two files above |

The receipt declares no artifact. The 129 MB archive, the 37 MB source tar, and
the 123 MB Electron ZIP stay in the retained transfer tree on their existing
streaming validation paths and are never copied into the evidence root. Parent
issue #53 must retain that exact transfer root through consumption.

## Consumer contract

`consume_archive_evidence` validates the receipt with the caller-owned
`ReceiptPolicy`, requires `result` to be `success`, requires exactly one
`artifact-lifecycle-v1` report at the fixed path, exactly one input at the fixed
path, and no staged artifact. It reads both files through `read_bound_bytes`,
requires the retained transfer manifest to equal the receipt-bound bytes before
and after revalidation, recomputes the complete semantic state from the subject
checkout and the retained transfer, and requires every report block to equal that
recomputed state. A structurally valid receipt or a producer `result` value alone
is never accepted.

The returned binding carries only what parent issue #53 needs: the report,
transfer manifest and receipt paths and digests, the archive name, size, digest
and artifact ID, the release version, the release manifest, install manifest and
license inventory digests, and the subject source commit and tree.

## Exercised against retained genuine transfers

Both runs used the real producer and a separate consumer invocation. Both are
explicitly labeled historical subjects with their original upstream execution.
Evidence roots and their manifests:

- `/home/alustosa/git/tongs/.worktrees/evidence/desktop-139-fd33-archive/evidence`
  with `/home/alustosa/git/tongs/.worktrees/desktop-139-fd33-archive-manifest.sha256`
- `/home/alustosa/git/tongs/.worktrees/evidence/desktop-139-ce4-archive/evidence`
  with `/home/alustosa/git/tongs/.worktrees/desktop-139-ce4-archive-manifest.sha256`

The fd33 subject is hosted run `34276954588`, attempt 1, event `pull_request`,
ref `refs/pull/140/merge`, commit `fd33e41a40baf6dee7b6d824c624a5b7b792f34e`,
tree `63c50e47193b0d5ea7ac9117826eb8a3ef5cb82d`, source epoch `1788900146`,
archive SHA-256
`820586fa41e1ae59868c1bef9db14bad44585362e0e190c688e45b98a8a00cc1`. Its retained
tree carries no hosted transfer manifest, so the manifest was prepared locally
from the retained genuine bytes with the issue #138 producer callable. The
independently derived `git archive` digest for that commit reproduces the
retained `evidence/source.tar` exactly.

The ce4 subject is hosted run `34254445558`, attempt 1, event `push`, ref
`refs/heads/feat/desktop-app`, commit `ce4d67f16b0dd3ef9cc61c300c62410b5af47299`,
tree `7d838b6f59d44aa1b3ce3f3aa7bb1a7d111c381d`, source epoch `1788886781`,
archive SHA-256
`8402870da912ee27b91fd718121e9c2f72b99463c98bdcc24039cb19dcf37bad`. Its transfer
manifest is the genuine hosted one retained from that run.

**The ce4 archive is an archive-contract diagnostic only.** That archive omits
part of the packaged desktop runtime module set, and its native application
startup is known to fail. The adapter still reports `pass` on it, because every
stage it runs is a contract, reproducibility, and metadata statement about bytes.
Neither archive-contract validity nor bitwise reproducibility establishes
application startup or hardware GPU function, and the report says so in its
`scope.excluded` list.

Operator negatives observed against the genuine fd33 data, each exiting 1 with no
output root created and the earlier success receipt untouched: wrong subject
source tree, a `push` relabel of the `pull_request` transfer, a wrong upstream
run ID, and a wrong trusted adapter program digest.

## Parent issue #53 obligations

1. Supply all four expectation identities and the receipt policy independently,
   and retain the exact transfer root through aggregate consumption.
2. Require the real producer job result to be `success` and require this check ID
   in the complete required check set.
3. Compare the returned archive digest, release manifest digest and install
   manifest digest with the archive binding used by the SBOM, RPM and signing
   inputs, and require them identical.
4. Regenerate this evidence for the final exact source-paired archive. The
   retained fd33 and ce4 runs are diagnostics.
5. Treat application startup and hardware GPU function as separate mandatory
   gates. This adapter never establishes either, and never makes a signing claim.
