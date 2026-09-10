# Desktop SBOM evidence adapter

`sbom_evidence.py` is the issue #53 boundary around the existing deterministic
SPDX generator. The producer callable and `produce` command require an exact
clean checkout, the verified candidate transfer root, a fresh output path, the
immutable bytes and independently expected SHA-256 of the archive receipt, and
caller-owned receipt and source policy.

Four identities stay separate. Candidate subject policy fixes the historical or
current source commit, tree, epoch, source archive, desktop archive, Electron
archive, and Electron configuration. Trusted tool policy fixes the adapter
checkout commit/tree and exact adapter, generator, receipt-reader, and schema
hashes. Upstream transfer policy fixes its authentic repository, repository
IDs, ref, event, run, and attempt. Receipt policy fixes the current adapter
verification repository, run, attempt, environment, provenance, and check ID.
This separation permits an explicitly labeled diagnostic over a retained older
candidate without claiming that its newer adapter came from the old source.

The adapter recomputes `HEAD`, its tree, commit epoch, and `git archive` digest
from the checkout. It reads the Electron archive digest from the versioned
configuration in that checkout and compares every value with caller policy. It
then invokes `scripts/build_desktop_sbom.py` twice. That generator performs the
transfer, artifact contract, source, archive, package, and license checks. The
adapter publishes evidence only when the complete SPDX bytes are equal.

The fresh evidence root contains:

| Path | Receipt kind and meaning |
| --- | --- |
| `reports/sbom-evidence.json` | `artifact-lifecycle-v1` report with the four exact successful stages |
| `artifacts/tongs-desktop.spdx.json` | SPDX 2.3 artifact, role `desktop-archive-sbom` |
| `inputs/archive-receipt.json` | Immutable small copy of the separately validated archive receipt |
| `inputs/candidate-attestation-transfer-v1.json` | Immutable small copy of the transfer manifest validated by the generator |
| `sbom-receipt.json` | Existing issue #110 receipt over the four files above |

`consume_sbom_evidence` and the `consume` command validate the receipt with the
caller-owned `ReceiptPolicy`, obtain all semantic inputs through issue #130
`read_bound_bytes`, and require the exact successful stages, source and
generator identities, input hashes, output binding, SPDX summary, and archive
scope. The caller must also retain and pass the original transfer root. Before
and after semantic reproduction, its manifest must equal the receipt-bound
immutable bytes. The consumer runs the existing generator against that root
under the same caller policy and requires its complete output bytes to equal
the bound SPDX document. A structurally valid receipt or a producer `result`
value alone is not accepted.

Parent issue #53 has three downstream obligations. It must separately validate
the referenced archive receipt against its own archive policy and compare that
receipt's SHA-256 and desktop archive binding with the returned SBOM binding. It
must require this check ID in the complete aggregate and require the producer
job result to be `success`. Candidate signing must use this exact SPDX path as
the `sbom-path` predicate for the covered archive subject. The large archive,
source tar, and Electron ZIP stay in the archive/transfer evidence and on their
existing streaming validation paths. Parent #53 must retain that exact transfer
root through aggregate consumption; the adapter copies only its small manifest
and the small archive receipt into its own evidence root.

The SBOM covers the desktop user archive. The separately installed core wheel,
optional plugins, host operating system, and RPM package set remain outside its
component inventory. npm integrity values identify registry distributions and
do not represent installed ASAR bytes. Historical retained candidates exercise
the adapter diagnostically, but final issue #53 must regenerate and bind the
SBOM for its exact source-paired archive.
