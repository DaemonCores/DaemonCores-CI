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

## Open

- Push of the debian-bootc fix done; read the result of the next real run
  (grub built with `.sbat`, shim accepts it).
- Whether the Fedora head used by the package carries the fixes behind SBAT
  generation 5 is inherited from the Fedora packaging, not verified here.
- Re-enable `SECURE_BOOT=1` in CI for amd64, then arm64 (shim + AAVMF varstore),
  and confirm on a real run.
