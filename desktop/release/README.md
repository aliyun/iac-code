# Desktop release source contract

Stable Desktop packages are built and published by the private `iac-code-publish` system on native macOS, Windows,
and Linux machines. GitHub Actions in this repository validates Desktop builds but does not retain or publish their
packages and never receives production signing material.

This directory and the public build scripts expose the source-side interfaces consumed from an immutable `vX.Y.Z`
tag:

- `publisher-contract.json` declares the minimum compatible publisher contract and required capabilities.
- `PRIVACY_NOTICE.template.md` is rendered with approved release values by the publisher.
- `desktop/scripts/build_desktop.py --skip-updater-artifacts` builds an updater-enabled application without creating or
  signing the final updater payload.
- `desktop/scripts/signing_handoff.py` exports, consumes, and bundles native components without changing release
  identity.
- `desktop/scripts/generate_release_metadata.py` produces the SBOM, third-party notices, and rendered privacy notice.
- `desktop/scripts/verify_release.py` validates generated metadata and supports manual inspection of staged packages.

When a Desktop change alters an interface consumed by `iac-code-publish`, update `publisher-contract.json` in the same
commit. Raise `minimumPublisherContract` whenever an older publisher cannot safely build the tagged source. Signing
keys, certificates, passwords, tokens, cloud credentials, generated packages, and private release records must never be
committed to this repository.

Operational setup, worker coordination, signing, notarization, upload, retry, retention, and rollback procedures belong
to the private publisher repository rather than this source contract.
