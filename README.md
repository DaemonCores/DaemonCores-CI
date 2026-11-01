# DaemonCores-CI

<p align="center">
  <img src="https://raw.githubusercontent.com/DaemonCores/.github/refs/heads/main/assets/banner.svg" alt="DaemonCores Banner" width="100%"/>
</p>

<p>
  <strong align="left">Simplify and Innovate for Everyone.</strong>
  <a href="https://github.com/DaemonCores/debian-bootc/wiki"><img align="right" src="https://img.shields.io/badge/Wiki-FFFFFF?style=for-the-badge&logoColor=white" alt="Documentation"/></a>
  <a href="https://github.com/orgs/DaemonCores/discussions"><img align="right" src="https://img.shields.io/badge/Community-000000?style=for-the-badge&logoColor=white" alt="Community"/></a>
  <a href="https://github.com/DaemonCores/debian-bootc"><img align="right" src="https://img.shields.io/badge/Base_debian_for_all_project-A81D33?style=for-the-badge&logo=debian&logoColor=white" alt="Debian Bootc"/></a>
  
  <em>Identify gaps and fill them, make improvements where possible, but above all, empower developers to offer more to users.</em>
</p>

---

DaemonCores-CI contains the reusable GitHub Actions workflows, composite actions, and shell tooling used to build DaemonCores bootc operating-system images.

It centralizes the pipeline implementation while product repositories keep their own Containerfiles, package manifests, tests, documentation, and release triggers.

## Pipeline overview

The reusable `full-pipeline.yml` workflow coordinates the following stages:

1. determine which stages are affected by the caller's changes;
2. optionally rebuild an upstream base repository;
3. build per-architecture package-builder images;
4. build Debian packages in dependency waves and publish a signed APT repository;
5. discover root-level `Containerfile*` variants;
6. build and boot-test image variants on native amd64 and arm64 runners;
7. publish per-architecture images and multi-architecture manifests to GHCR;
8. build online and offline installer ISOs;
9. optionally build Raspberry Pi or Linux Gallium disk images.

## Reusable workflows

| Workflow | Responsibility |
| --- | --- |
| `full-pipeline.yml` | Change planning and end-to-end orchestration. |
| `build-env.yml` | Builds the caller's manifest-defined package build environment for amd64 and arm64. |
| `bootc-debs-builder.yml` | Reads `packages.yml`, computes dependency waves, and publishes the resulting APT repository. |
| `build-one-deb.yml` | Builds or reuses one content-addressed Debian package. |
| `bootc-build.yml` | Discovers image variants, builds them natively, runs QEMU tests, scans images with Trivy (report only), signs images, and publishes manifest lists. |
| `iso-builder.yml` | Produces online and offline installer ISOs from a Fedora Anaconda environment. |
| `img-builder.yml` | Produces opt-in raw disk images for supported targets. |
| `docs-wiki-sync.yml` | Copies `docs/*.md` into a repository wiki (obsolete for DaemonCores repos whose `docs/` is a wiki symlink). |
| `upstream-check.yml` | Fails when a package tracking an upstream release does not ship that release in the published APT repository (the latest release did not build). |
| `release.yml` | Publishes a GitHub release from Conventional Commits: `channel: edge` makes a pre-release `vX.Y.Z-edge.N` per releasable push, `channel: stable` makes `vX.Y.Z` and only runs when triggered manually. |
| `lint.yml` | Provisions the lint toolchain (checksum-pinned hadolint) and runs the caller's `make admission check docs-check`. |
| `rotate-apt-key.yml` | Rotates the APT signing key (`APT_GPG_KEY`) from CI and restarts the caller's pipeline, which republishes the repository. Nothing is committed: the key pin is derived at build time. Needs `PAT_SECRETS`. |

## Composite actions

| Action | Responsibility |
| --- | --- |
| `build-deb` | Hashes package inputs, reuses the last published artifact when unchanged, runs the package build, and records metadata. |
| `upstream-track` | Resolves the latest upstream release (version and asset SHA-256) of the entries a package lists under `track:`, so it follows upstream with nothing committed per version. |
| `next-version` | Script (`next-version.sh`) that derives the next version from Conventional Commits since the last stable tag. |
| `bootc-debs-start` | Checks out the caller and prepares the package workspace. |
| `bootc-debs-end` | Creates the signed APT repository, publishes its cache, and deploys it to GitHub Pages. |
| `image-test` | Installs a local image onto a virtual disk, boots it with QEMU/KVM, and executes manifest-defined tests over SSH. |
| `sign-image` | Installs cosign (2.x) and signs image references with the repository key, keyless when there is none. |
| `trigger-pipeline` | Dispatches a workflow in another repository and waits for its result. |

## Caller repository contract

A product repository using the full pipeline is expected to provide:

```text
Containerfile*
workflows/
  build-env/env.yml
  bootc-debs-builder/packages.yml
  bootc-debs-builder/<package sources>
  tests.yml
docs -> ../<repo>.wiki (symlink; the wiki is the documentation)
```

Each root-level `Containerfile*` becomes an image variant. `Containerfile` maps to the `latest` family; for example, `Containerfile.nvidia` maps to the `nvidia` family. Each tag has one image per architecture (`<tag>_<arch>`) and a multi-arch manifest list `<tag>`; automatic updates are a run-time choice, not an image variant. A Containerfile carrying the `org.daemoncores.unsigned-kernel-image` label also gets a `<tag>-nosign` twin when the `both` signing mode is active: the same image on the unsigned kernel package, for amd64 and the plain-tag arm64 kernel only. `Containerfile.base` is not an image variant: it is the shared base every other Containerfile builds on (`BASE_IMAGE`, default `localhost/debian-bootc-base`). Each build job builds it once, then the image, then its twin on the same layers, and pushes only the images the run asked for; the base is never pushed.

Package dependencies declared in `packages.yml` are converted into build waves. Independent packages build in parallel; a package is scheduled after every declared dependency.

### Release channels

`release.yml` derives the version from Conventional Commits since the last stable tag (`vX.Y.Z`): a breaking change (`type!:` or a `BREAKING CHANGE:` footer) bumps the major, `feat` the minor, `fix`, `perf` and `revert` the patch. Other types (`docs`, `chore`, `ci`, `refactor`, ...) release nothing. The `edge` channel tags every releasable commit as a pre-release (`vX.Y.Z-edge.N`, `N` being the commit count since the last stable tag). The `stable` channel refuses any trigger other than `workflow_dispatch`: promoting to stable stays a manual decision, so a long pause never promotes anything on its own. The caller owns the triggers, for example `on: push` on `main` for `edge` and `workflow_dispatch` for `stable`.

### Rolling upstream releases

A package that builds an upstream project can follow its latest GitHub release instead of pinning a version and checksum in the repository:

```yaml
- name: bootc
  track:
    BOOTC_VER:                       # env variable that receives the tag
      repo: bootc-dev/bootc
      strip-v: false                 # true drops the leading "v"
      digests:                       # env variable -> release asset name
        BOOTC_VENDOR_SHA256: "bootc-{version}-vendor.tar.zstd"
```

`build-one-deb.yml` resolves `track:` before the build; the resolved values feed the content hash, so a new release rebuilds the package. The asset checksums come from the release API, so downloads stay verified. When the new release does not build, `build-deb` keeps the last-good published deb, opens a broken-build issue and retries on the next run: a bad upstream release never reaches the image. `upstream-check.yml` reports, from a scheduled caller, the packages stuck on an older release.

Every runtime test in `workflows/tests.yml` must include a diagnostic command. The harness rejects incomplete test definitions so a failed boot test always provides actionable state.

## Minimal caller workflow

```yaml
name: Full Pipeline

on:
  push:
    branches: [main]
  workflow_dispatch:

permissions:
  actions: write
  contents: write
  packages: write
  pages: write
  id-token: write
  issues: write

jobs:
  run:
    uses: DaemonCores/DaemonCores-CI/.github/workflows/full-pipeline.yml@main
    secrets: inherit
```

Product repositories normally add scheduled builds, concurrency control, path filters, and manual stage-selection inputs.

## Shared checks and tooling

Everything generic that the repositories used to copy lives in `scripts/` and
runs against any checkout with `--root DIR` (default: the current directory).
The `lint.yml` workflow checks DaemonCores-CI out into `.ci` and exports
`CI_REPO`; locally, `CI_REPO` defaults to the sibling checkout.

| Script | What it enforces |
|--------|------------------|
| `check_docstrings.py` | Every module, class and function (private and test ones too) has a Google docstring with the `Args`/`Returns`/`Yields`/`Raises` sections its signature needs |
| `check_tests_manifest.py` | `workflows/tests.yml`: `name`/`run`/`diag` present, known `arch`/`variant`, unique names, positive timeouts |
| `check_build_context.py` | Containerfile `COPY` sources exist and are not `.dockerignore`d; `packages.yml` names, deps, sources, build/upstream and hashed `lib/*.sh` helpers are coherent |
| `gen_docs.py` | Generates `docs/reference/` from the Python docstrings and shell headers listed in the caller's Makefile (`--check` fails when stale) |
| `check_docs.py` | Documentation sync: every `[src: file: path:LINE]` citation resolves to a non-blank line and every relative Markdown link exists |
| `e2e-local.sh` | Runs the boot tests of the calling repository against a local image with the CI harness |

A caller Makefile only wires them (`$(CI_REPO)/scripts/...`); it carries no
copy. `make check` and `make test` in this repository hold the scripts and
their tests to the same strict rules.

## Credentials and repository settings

The complete pipeline currently uses the following Actions secrets when the corresponding stage or package requires them:

| Secret | Purpose |
| --- | --- |
| `PAT_SECRETS` | Token with `secrets:write`. The only secret a fork has to provide: it lets the `prepare` job generate all the others. Without it nothing is generated and the pipeline degrades (keyless images, unsigned modules). |
| `PAT_PKG` | Optional. Pull and push GHCR images; defaults to `github.token`. |
| `APT_GPG_KEY` | Signs the generated APT repository. Auto-generated. |
| `SB_SIGNING_KEY`, `SB_SIGNING_CERT` | Self-signed Secure Boot key and certificate (base64) for the custom EFI bootloader. Auto-generated. |
| `COSIGN_PRIVATE_KEY`, `COSIGN_PASSWORD`, `COSIGN_PUBLIC_KEY` | The repository's own image signing key. Auto-generated. |
| `MODULE_SIGNING_KEY`, `MODULE_SIGNING_CERT` | Kernel module signing key. Auto-generated when module signing is enabled. The modules are always signed and shared; in `both` mode only the kernel exists twice, as the signed `linux-image-common` and the `-nosign` twin (a manifest entry marked `when: {unsigned-kernel: true}`), published in the same APT suite. |

### Generated secrets and the `prepare` job

Each repository owns its keys: the pipeline stays here, the keys belong to the repository that runs it. The `prepare` job of `full-pipeline.yml` runs `scripts/ensure-secrets.sh`, which creates every missing secret group and never touches an existing one. When all secrets exist the job does nothing. Secrets created during a run are only visible to the next run, so when `prepare` created some it dispatches the caller workflow again (`gh workflow run`, needs `actions: write`) with the same plan and skips the rest of the current run. The loop ends by itself: the second run finds every secret.

The APT repository key is not committed anywhere. The `apt_key` job waits until the published `gpg.key` has the fingerprint of the `APT_GPG_KEY` secret, then passes its sha256 and the repository URL to the image build as `BOOTC_GPG_SHA256` and `BOOTC_APT_URL` build arguments.

The cosign public key is baked into the images at `/etc/pki/containers/` together with a `policy.json` that makes the host require the signature. Without a key the images are signed keyless and no policy is shipped. Regenerating a key invalidates it for hosts that already pinned the old one. The Secure Boot certificate is self-signed: firmware only trusts it once the user has enrolled it (MOK or db).

The caller also needs GitHub Pages configured for Actions deployment and the permissions shown above. Cross-repository base rebuilds require a token accepted by the `trigger-pipeline` action.

## Interface policy

The current callers reference this repository at `@main`. Treat workflow input names, manifest structure, artifact names, and tag formats as shared interfaces: coordinate incompatible changes with every caller in the same change set.

---

<p>
  <strong align="left">Made with ⭐ by the DaemonCores community</strong>
  <a href="https://github.com/DaemonCores/debian-bootc/wiki"><img align="right" src="https://img.shields.io/badge/Wiki-FFFFFF?style=for-the-badge&logoColor=white" alt="Documentation"/></a>
  <a href="https://github.com/orgs/DaemonCores/discussions"><img align="right" src="https://img.shields.io/badge/Community-000000?style=for-the-badge&logoColor=white" alt="Community"/></a>
  <a href="https://github.com/DaemonCores/debian-bootc"><img align="right" src="https://img.shields.io/badge/Base_debian_for_all_project-A81D33?style=for-the-badge&logo=debian&logoColor=white" alt="Debian Bootc"/></a>
</p>
