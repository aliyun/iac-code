# iac-code Desktop Stable Release Playbook

This playbook covers Desktop packages only. It does not change the normal Web, CLI, provider, permission, or business
runtime behavior. A stable release is blocked until every applicable platform gate below has produced real evidence.
Development and pull-request bundles are intentionally unsigned and must never be published to the stable updater.

## GitHub pre-build and private publication boundary

This repository builds only pre packages and signable intermediate components. It does not publish Desktop assets to
GitHub Releases or OSS and it does not contain Alibaba Cloud upload credentials or commercial platform signing logic.

- `.github/workflows/desktop-release.yml` starts from the repository's existing `vX.Y.Z` GitHub Release. It builds a
  complete macOS arm64, Windows x64, and Linux x64 pre package set, verifies the updater signatures, and uploads
  short-lived `desktop-pre-*` Actions artifacts plus `desktop-pre-manifest`.
- The same workflow exports `desktop-signing-input-macos-aarch64` and
  `desktop-signing-input-windows-x64`. These archives contain only the exact host, native helper, and frozen sidecar
  files needed by the private signing pipeline, with tag, commit, file mode, and SHA-256 evidence.
- Commercial signing happens in the private `iac-code-publish` repository. Signed components are placed under the
  configured OSS `desktop/staging/` handoff prefix and trigger `.github/workflows/desktop-signed-package.yml` with an
  exact archive SHA-256. GitHub verifies the handoff, restores the signed components, and bundles without recompiling
  or outer-signing them. The macOS `.app` is wrapped with `ditto` before Actions artifact upload so file modes and
  symlinks survive the return to the private signer.
- The private pipeline performs the final Windows installer signature and macOS app/DMG signing, notarization, and
  stapling. It also owns final updater signing, immutable GitHub Release assets, versioned OSS objects, and the last
  atomic update of `desktop/stable/latest.json`.
- When commercial certificates are unavailable, the private pipeline explicitly selects unsigned mode and promotes
  the verified complete pre packages. Signing failures never silently downgrade a signed-required release.

The former GitHub-to-FC release broker was removed. The public release root is
`https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code`; its corresponding OSS prefix is
owned exclusively by the private publisher.

One-time updater key setup (the signer prompts for a password; never pass it on the command line):

```text
cd desktop
npm ci
npm run tauri -- signer generate --write-keys /a/private/location/iac-code-desktop-updater.key
gh secret set TAURI_SIGNING_PRIVATE_KEY < /a/private/location/iac-code-desktop-updater.key
gh secret set TAURI_SIGNING_PRIVATE_KEY_PASSWORD
gh variable set IAC_CODE_DESKTOP_UPDATER_PUBKEY \
  --body "$(tr -d '\r\n' < /a/private/location/iac-code-desktop-updater.key.pub)"
gh variable set IAC_CODE_DESKTOP_UPDATER_ENDPOINT \
  --body "https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/desktop/stable/latest.json"
```

Back up the private key and its password independently. Losing either prevents installed Desktop clients from accepting
future updater payloads. The GitHub pre-build key and the private final-publisher key must represent the same updater
identity while both pre and signed channels are supported.

The obsolete `IAC_CODE_DESKTOP_RELEASE_BROKER_URL`, `IAC_CODE_DESKTOP_RELEASE_BROKER_AUDIENCE`, and
`IAC_CODE_DESKTOP_DOWNLOAD_BASE_URL` repository variables are not used by this workflow and should not be restored.

## 1. Freeze release inputs

1. Select the release tag and confirm the Python package, Tauri Host, native helpers, and `desktop/package.json` use the
   same version.
2. Build from a clean tagged checkout with the committed `uv.lock`, both Cargo lockfiles, and
   `desktop/package-lock.json`. Use CPython 3.12 and the pinned Tauri/updater versions.
3. Record the legal publisher, Apple Team ID, Windows certificate SimpleName, Linux signing identity, updater endpoint,
   updater public key, supported channels, and artifact storage location in the private release run configuration.
4. Never place Apple certificates, Windows PFX files, GPG/Sigstore keys, Tauri updater private keys, passwords, tokens,
   or temporary credentials in the repository, command line, generated metadata, uploaded diagnostics, or release notes.

## 2. Build and compliance metadata

Run the normal Desktop test/build matrix first. On each target platform, after dependencies have been installed and the
Rust workspaces have built, generate and reproduce-check platform metadata:

```text
uv run --python 3.12 python desktop/scripts/generate_release_metadata.py --output-dir <metadata-dir>
uv run --python 3.12 python desktop/scripts/generate_release_metadata.py --output-dir <metadata-dir> --verify
```

Render the release privacy notice only with approved values. Missing values fail:

```text
uv run --python 3.12 python desktop/scripts/generate_release_metadata.py \
  --output-dir <metadata-dir> --verify --render-privacy \
  --legal-entity <approved-entity> --privacy-contact <approved-contact> \
  --telemetry-retention <approved-retention> --effective-date <YYYY-MM-DD>
```

Legal/release owners must review `PRIVACY_NOTICE.md`, `THIRD_PARTY_NOTICES.txt`, and `desktop-sbom.cdx.json`. Do not
publish when a dependency has no license evidence or when the privacy notice no longer matches the product.

## 3. Platform signing and packaging

### macOS

1. Sign nested Python/native Mach-O files and helpers with the approved Developer ID and frozen entitlements, then sign
   the outer `.app`; do not substitute `codesign --deep` for the inner-to-outer sequence.
2. Build the `.dmg`, submit the final deliverables for Apple notarization, wait for success, and staple the tickets.
3. Run strict `codesign`, Gatekeeper, and stapler validation through `verify_release.py` on the exact uploaded bytes.
4. Run the signed package on the minimum supported Apple Silicon macOS/WebKit runner and assert that the business
   `app.js` UI marker appears. If that runner is unavailable, the claimed minimum version cannot ship.

### Windows

1. Sign the GUI Host, console-subsystem sidecar/helper executables, updater bootstrap helper, and NSIS installer with the
   approved certificate. The configured publisher value is the certificate SimpleName, not its full Subject DN.
2. For the stable build set `IAC_CODE_DESKTOP_STABLE_SIGNED_RELEASE=1`, provide the already signed staged updater helper
   through `IAC_CODE_DESKTOP_SIGNED_UPDATER_HELPER`, and set `IAC_CODE_WINDOWS_SIGNING_PUBLISHER`. The build fails if
   Authenticode or the publisher does not match.
3. Verify Authenticode on the exact published first-party PE files and installer. Run the real N-1→N NSIS zip/helper
   handoff tests on a clean Windows 10/11 VM with no additional visible console window.

### Linux

1. Build the AppImage updater flavor and `.deb` external-update flavor from separate Host builds.
2. Publish SHA-256 checksums. Sign the checksum set and repository metadata using the approved GPG or Sigstore identity.
   Keep Tauri updater `.AppImage.sig`, GPG `.AppImage.asc`, and Sigstore `.sigstore.json` names distinct.
3. Verify the AppImage, `.deb`, checksum signature, package-manager channel, and clean Ubuntu 22.04/Debian 12 install.

## 4. Updater and recovery acceptance

1. Sign updater payloads with the production Tauri updater key and verify wrong signatures, wrong platform/architecture,
   downgrade attempts, and corrupted artifacts are rejected. Install `minisign` on the release verifier and provide the
   public updater key through `IAC_CODE_DESKTOP_UPDATER_PUBKEY`; `verify_release.py` cryptographically checks every
   updater payload against its adjacent `.sig` file.
2. Exercise N-1→N for every updater channel. Confirm localStorage origin continuity, only one Host/sidecar, Windows helper
   `complete` handshake cleanup, and macOS/AppImage sidecar recovery after an injected install/relaunch API failure.
3. Confirm `.deb` contains no native updater capability and only presents the external package-management path.
4. Confirm failed or interrupted updates leave a usable bundled recovery page and diagnostic record.

## 5. Privacy, credentials, and artifact inspection

1. Run `verify_release.py --strict` on the exact publication directory and generated metadata.
   The invocation is platform-specific and must include all strict inputs:

   ```text
   uv run --python 3.12 python desktop/scripts/verify_release.py \
     --strict --channel <macos|windows|appimage|deb> \
     --artifact-dir <exact-publication-directory> --metadata-dir <metadata-dir> \
     --privacy-notice <metadata-dir>/PRIVACY_NOTICE.md
   ```

2. Confirm bundles contain no private key, PFX/P12, signing password, updater test key, real credential fixture, cookie,
   personal checkout path, or CI temporary path.
3. Confirm the SBOM, notices, privacy notice, checksums, signatures, release notes, and support/rollback instructions are
   uploaded beside the installers. These metadata files do not replace notices that a platform installer must embed.

## 6. Publish, monitor, and roll back

1. Upload immutable versioned artifacts first. Verify their hashes and signatures after upload, then update the stable
   channel metadata last. An explicitly approved unsigned-mode release may promote the verified pre artifacts; a
   signed-required run must never silently fall back to them.
2. Perform clean-machine install/launch/update smoke before announcing the release.
3. Retain the prior signed version and updater metadata for rollback. If rollout fails, stop publishing the new channel
   entry; do not reuse a version number or replace bytes under an existing signed URL.
4. Record release approvers, verification logs, hashes, signature identities, notarization result, VM/runner versions,
   privacy/legal approval, known issues, and rollback decision in the private release record.
