# DEBUG: boot-tests under UEFI Secure Boot

## Symptom

- amd64: with `SECURE_BOOT=1` the boot test stops on a blue `ERROR` dialog
  `Verification failed: (0x1A) Security Violation` right after the shim starts.
  Until now this was read as "OVMF rejects the Microsoft-signed shim".
- arm64: every `*-arm64` job fails with `VM did not become reachable within
  300s`; the serial console shows `Access Denied` on the disk and `Security
  Violation` on the EFI Shell.

## arm64 firmware selection (fixed, commit 39d52b6)

`find /usr/share/AAVMF -name 'AAVMF_CODE*.fd' | head -1` can return the
`.secboot` flavour shipped next to the plain one by Ubuntu's `qemu-efi-aarch64`.
Measured locally with the package's own firmware files and QEMU `virt`:

| firmware                                   | EFI Shell                 |
|--------------------------------------------|---------------------------|
| `AAVMF_CODE.secboot.fd` + `AAVMF_VARS.ms.fd` | `Security Violation`      |
| `AAVMF_CODE.no-secboot.fd` + `AAVMF_VARS.fd` | `Shell>` starts           |

The fix selects a non-enforcing pair, which means arm64 is still booted with
Secure Boot off. Testing signed images needs the Secure Boot path below.

## amd64 Secure Boot (0x1A): cause proven

Local bench (Arch OVMF 202608 `OVMF_CODE.secboot.4m.fd`, `q35,smm=on`, KVM,
varstore built with `virt-fw-vars`: own PK/KEK, Microsoft UEFI CA 2011 in db,
own cert as MOK), ESP = Debian `shimx64.efi.signed` + a grub:

| # | grub on the ESP                                       | result                        |
|---|-------------------------------------------------------|-------------------------------|
| 1 | shim alone                                            | shim runs (looks for grubx64) |
| 2 | the image's grub (signed by our key, MOK enrolled)    | `0x1A Security Violation`     |
| 3 | Debian's signed grub                                  | `grub>` prompt                |
| 4 | our grub re-signed, `.sbat` with `grub,4`             | `0x1A Security Violation`     |
| 5 | our grub re-signed, cert in db instead of MOK         | `0x1A Security Violation`     |
| 6 | our grub re-signed, `.sbat` with `grub,5`, MOK        | `grub>` prompt (boots)        |

- Row 1: the firmware accepts the shim, so the former diagnosis (firmware
  rejects the shim) was wrong; the message comes from the shim verifying the
  next stage.
- Row 3: firmware, shim and bench are sound.
- Rows 2/4/5/6: the shim's built-in SBAT policy
  (`sbat,1,2025051000 / shim,4 / grub,5 / grub.proxmox,2`, read from
  `shimx64.efi.signed`) revokes a grub with no `.sbat` section (the image's grub
  has sections `.text .data mods .reloc` only) or with generation 4.
  Generation 5 plus the existing signature and MOK boots.

## Fix (debian-bootc, `workflows/bootc-debs-builder/packages.yml`)

`grub-mkimage --sbat` with a file built from the Fedora packaging of the same
source (`sbat.csv.in`, generation resolved at build time, not pinned) plus one
component line of our own, for both `grub-efi-amd64-signed` and
`grub-efi-arm64-signed`.

## Validation of the fix (local)

Real `grub-mkimage --sbat` build from the rhboot/grub2 head in a container, with
the exact snippet from `packages.yml`: the image carries a `.sbat` section
(`sbat,1` / `grub,5` / `grub.debian-bootc,1`), re-signed with a test key and
booted on the bench with shim + MOK: `grub>` prompt under Secure Boot.
(The container needed `--http-proxy=false`: it inherited a local proxy
`127.0.0.1:8899` and `git clone` failed; bench-only.)

## arm64 Secure Boot: bench result (local, QEMU `virt` TCG)

Firmware: Ubuntu `qemu-efi-aarch64` 2024.02-2ubuntu0.10, `AAVMF_CODE.secboot.fd`
with a varstore built from the blank `AAVMF_VARS.fd` (own PK/KEK, Microsoft UEFI
CA 2011 in db, bench cert as MOK). ESP: Debian `shimaa64.efi.signed`
(sha256 `e5446d2a...`) + the `grubaa64.efi` of the CI-built
`grub-efi-arm64-signed` package (run 41054040585, carries `.sbat` `grub,5`),
re-signed with a bench key after `sbattach --remove`.

| # | scenario                                        | result                              |
|---|-------------------------------------------------|-------------------------------------|
| 1 | grub signed by bench key, bench cert in MOK     | `GRUB version 2.12`, `grub>` prompt |
| 2 | same grub, a different cert in MOK              | shim `0x1A Security Violation`      |
| 3 | unsigned-by-us grub as BOOTAA64.EFI (no shim)   | firmware `Security Violation`       |

Rows 2 and 3 show the enforcement is real (the firmware rejects unsigned code,
shim rejects a grub whose signer is not in MOK), so row 1 is a genuine Secure
Boot boot. The arm64 chain therefore works with the same recipe as amd64.

Pitfall met on the bench: the grub inside the package is already signed by the
image key; signing it again appends a second signature, so a "wrong MOK" control
still passed. Controls must start from a stripped binary.

## Harness change (this commit)

`image-test.sh`: `SECURE_BOOT=1` is no longer refused on arm64; `build_sb_vars`
picks the blank varstore per architecture; arm64 boots `AAVMF_CODE.secboot.fd`
with that varstore; amd64 gets `smm=on` when Secure Boot is enforced (the
secboot OVMF builds protect the varstore through SMM; it was on in the bench).

## Real run 41062757704 (debian-bootc b358f9f): the image still had the old grub

- The `grub-efi-amd64-signed` deb published on Pages carries `.sbat`
  (`objdump -h`: section `.sbat`), and the APT index serves that exact file
  (`Packages` SHA256 `c8671566...` = the downloaded deb).
- The grub extracted from the pulled image `latest_amd64` has sections
  `.text .data mods .reloc` only: no `.sbat`, so the shim would still revoke it.
- Image job log: `STEP 12/33: RUN apt update && apt install ...` is
  `--> Using cache ...`, 1 minute after the Pages index was republished. The layer
  comes from the registry layer cache (`--cache-from`), keyed on the instruction
  text, which did not change. The package version string did not change either.
- The content hash (`org.daemoncores.content-hash`, built from `hashes.json`) was
  only stamped as a label, so a rebuilt deb never reached the image.
- Local proof that a build arg declared before the `RUN` invalidates the layer
  even when the command does not reference it (`podman build --layers`):
  `DEBS_HASH=a` twice -> `Using cache` on both steps; `DEBS_HASH=b` -> the `RUN`
  re-executes.

Fix: the CI computes a per-architecture fingerprint of `hashes.json` and passes it
as `--build-arg DEBS_HASH`; `Containerfile` and `Containerfile.minimal` declare
`ARG DEBS_HASH=none` before the first layer that installs from the repository.

## Open

- Read the next real runs of the debian-bootc pipeline (grub `.sbat`, shim accepts it).
- Whether the Fedora head used by the package carries the fixes behind SBAT
  generation 5 is inherited from the Fedora packaging, not verified here.
- End-to-end with a real image (install-fs.sh disk, SSH reachable) under
  `SECURE_BOOT=1`, then flip the default to 1 in CI and confirm on a real run.
