# Sigstore verification fixture

These files exercise Sigstore 4.5.0's real offline certificate, transparency log,
DSSE, and policy verification. They are test data only and are never loaded by the
production installer.

- `sigstore-python-4.5.0.intoto.sigstore.json` is GitHub's public attestation for
  `sigstore-4.5.0-py3-none-any.whl`, retrieved from the GitHub attestations API for
  `sigstore/sigstore-python` on 2026-09-07. SHA-256:
  `6b034d6a6046beffec171b4b087001e97029b0bf97b42feea9e3a3deb3fdcffe`.
- `sigstore-production-client-trust-config.json` is the Sigstore production client
  trust configuration retained on 2026-09-07. SHA-256:
  `53553bc92bfb7e0d408c01c84a03df573b33b9900b82c9e3062e186f7094c728`.

The fixture records repository `sigstore/sigstore-python`, workflow
`.github/workflows/release.yml`, tag `v4.5.0`, and source commit
`181074f4dc11b7e85ef44556e25248ef14fcb554`. The tests verify it only with that
explicit test identity and separately prove the fixed Tongs production policy
rejects it.
