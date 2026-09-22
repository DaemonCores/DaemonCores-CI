# DaemonCores-CI

<p align="center">
  <img src="https://raw.githubusercontent.com/DaemonCores/.github/refs/heads/main/assets/banner.svg" alt="AstralEmu Banner" width="100%"/>
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
| `bootc-build.yml` | Discovers image variants, builds them natively, runs QEMU tests, signs images, and publishes manifest lists. |
| `iso-builder.yml` | Produces online and offline installer ISOs from a Fedora Anaconda environment. |
| `img-builder.yml` | Produces opt-in raw disk images for supported targets. |
| `docs-wiki-sync.yml` | Copies `docs/*.md` into a repository wiki. |

## Composite actions

| Action | Responsibility |
| --- | --- |
| `build-deb` | Hashes package inputs, reuses the last published artifact when unchanged, runs the package build, and records metadata. |
| `bootc-debs-start` | Checks out the caller and prepares the package workspace. |
| `bootc-debs-end` | Creates the signed APT repository, publishes its cache, and deploys it to GitHub Pages. |
| `image-test` | Installs a local image onto a virtual disk, boots it with QEMU/KVM, and executes manifest-defined tests over SSH. |
| `trigger-pipeline` | Dispatches a workflow in another repository and waits for its result. |

## Caller repository contract

A product repository using the full pipeline is expected to provide:

```text
Containerfile*
workflows/
  build-env/env.yml
  bootc-debs-builder/packages.yml
  bootc-debs-builder/<package sources>
  image-tests/tests.yml
docs/*.md
```

Each root-level `Containerfile*` becomes an image variant. `Containerfile` maps to the `latest` family; for example, `Containerfile.minimal` maps to the `minimal` family. A Containerfile carrying the `org.daemoncores.autoupdate-variants` label also produces a lock variant.

Package dependencies declared in `packages.yml` are converted into build waves. Independent packages build in parallel; a package is scheduled after every declared dependency.

Every runtime test in `workflows/image-tests/tests.yml` must include a diagnostic command. The harness rejects incomplete test definitions so a failed boot test always provides actionable state.

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

## Credentials and repository settings

The complete pipeline currently uses the following Actions secrets when the corresponding stage or package requires them:

| Secret | Purpose |
| --- | --- |
| `PAT_PKG` | Pull and push GHCR images. |
| `APT_GPG_KEY` | Sign the generated APT repository. |
| `SB_SIGNING_KEY` | Sign the custom EFI bootloader package. |
| `SB_SIGNING_CERT` | Certificate paired with the EFI signing key. |

The caller also needs GitHub Pages configured for Actions deployment and the permissions shown above. Cross-repository base rebuilds require a token accepted by the `trigger-pipeline` action.

## Interface policy

The current callers reference this repository at `@main`. Treat workflow input names, manifest structure, artifact names, and tag formats as shared interfaces: coordinate incompatible changes with every caller in the same change set.

## License

[LGPL-2.1](LICENSE)

---

<p>
  <strong align="left">Made with ⭐ by the DaemonCores community</strong>
  <a href="https://github.com/DaemonCores/debian-bootc/wiki"><img align="right" src="https://img.shields.io/badge/Wiki-FFFFFF?style=for-the-badge&logoColor=white" alt="Documentation"/></a>
  <a href="https://github.com/orgs/DaemonCores/discussions"><img align="right" src="https://img.shields.io/badge/Community-000000?style=for-the-badge&logoColor=white" alt="Community"/></a>
  <a href="https://github.com/DaemonCores/debian-bootc"><img align="right" src="https://img.shields.io/badge/Base_debian_for_all_project-A81D33?style=for-the-badge&logo=debian&logoColor=white" alt="Debian Bootc"/></a>
</p>
