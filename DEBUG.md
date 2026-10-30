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

## Confirmed on the real run (debian-bootc 3213bbf, Full Pipeline 41081357672)

- Image `latest_amd64` pulled after the run: `grubx64.efi` has the `.sbat`
  section (`.text .data mods .sbat .reloc`), sha256 `f0d212e9...` = the deb on Pages.
- Bench (OVMF secboot, `q35,smm=on`, KVM, own PK/KEK, MS UEFI CA 2011 in db): the
  image's own shim (sha `e103c5d0...`) + that grub, with the image's
  `sb_signing.crt` (CN=debian-bootc Secure Boot Signing Key) as MOK: shim loads
  grub, `GRUB version 2.12`, `grub>` prompt. No 0x1A.
- Job results of that run: `latest-amd64` success; the 4 other image jobs failed on
  the unrelated RAM gate / arm64 TCG slowness (see Open).

## Second blocker found locally: the kernel is not signed

- `vmlinuz` of the image (`/usr/lib/modules/7.2.14/vmlinuz`) has an empty PE
  certificate table (`off/size 0 0`): nothing in the pipeline signed it (`sbsign`
  was only applied to grub).
- Bench (shim + the image's grub + `grub.cfg` + the kernel on the ESP, bench cert
  and `sb_signing.crt` in MOK):

| # | kernel on the ESP                                 | result                                                        |
|---|---------------------------------------------------|---------------------------------------------------------------|
| 1 | unsigned `vmlinuz`                                | `shim_lock_verifier_write: bad shim signature`, `linux` rc=45 |
| 2 | `vmlinuz` signed by a bench key, cert in MOK      | `linux` rc=0                                                  |

So the end-to-end boot would have failed at the kernel even with the fixed grub.

Status: no fix is applied. A first attempt (a `kernel_sign_efi` step in the
`linux-image` build) was reverted: it ignored the signing modes the build already
has (`MODULE_SIGNING_ACTIVE`, `KERNEL_SIGNING_ENFORCE`, the `-nosign` twins). The
EFI signature of `vmlinuz` has to follow those modes; until it does, a signed
category booted under Secure Boot stops at the kernel (`rc=45` above).

The module signing key (`MODULE_SIGNING_*`) is compiled into the kernel's built-in
keyring: no MOK entry is needed for it. MOK is needed for the keys of forks, which
is what the installer's `mok-stage` enrolls (`sb_signing.crt` plus
`/usr/share/debian-bootc/mok.d/*.{crt,cer,pem,der}`, never a private key).

## Harness change: Secure Boot follows the image category, mok.d enrolled

`image-test.sh` reads `categories:` of the tests manifest. The category is the one
of the running arch whose `suffix` ends the tag (minus `_<arch>`), longest suffix
first; it gives `secure_boot` (0|1) and `reference`. Tests with a `category:` list
run only there; tests without one are global and run on the `reference` category
only. A manifest without `categories` keeps every test and Secure Boot off.
`SECURE_BOOT` in the environment still overrides, for a local run.

When Secure Boot is on, `build_sb_vars` enrolls in the MOK the same set as the
installer: `sb.crt` plus the certificates found in the image's `mok.d` (PEM or
DER, files holding a private key skipped).

Local check (functions sourced, `ssh` stubbed): `latest_amd64` -> `signed`
(SB=1, reference); `latest-nosign_amd64` -> `unsigned-kernel` (SB=0);
`latest-4k_arm64` -> `uboot` (SB=0); an unknown category in a test, a duplicate
`(arch, suffix)` or a `secure_boot` outside 0|1 are refused before the VM boots.

## Open

- Needs root (image pull + install-fs.sh): run `image-test.sh` with `SECURE_BOOT=1 ARCH=amd64` on the real image; blocked while nobody validates the pkexec prompt.
- Whether the Fedora head used by the package carries the fixes behind SBAT
  generation 5 is inherited from the Fedora packaging, not verified here.
- End-to-end with a real image (install-fs.sh disk, SSH reachable) under
  `SECURE_BOOT=1` is now the CI run itself (default 1); result to be read from the
  real run, per architecture.

## A push touching only tests.yml never booted an image

Full Pipeline 41101835515 (debian-bootc `5ded36a`, only `workflows/tests.yml`
changed) concluded `success` with `image=false`: the plan job matches changed
files against `^(Containerfile|src/|assets/|workflows/image-tests/)` and that
directory does not exist in the product repo, so the manifest was never run.
Fix: `full-pipeline.yml` also matches `workflows/tests.yml` (the manifest the
image-test action reads by default).

## linux-image amd64 failed in run 41103751203 at version resolution (open, cause not proven)

- Job log (18:06:00): `patch not available for 7.3.1: .../CachyOS/kernel-patches/master/7.3/sched/0001-bore.patch`
  (404), the selector falls back to 7.2.14, then
  `no sha256 for linux-7.2.14.tar.xz in https://cdn.kernel.org/pub/linux/kernel/v7.x/sha256sums.asc`
  and `KERNEL_VER` stays unset (`track.sh` `resolve_kernel`).
- Local replay at 18:23 of the same lookup (`curl` of that URL + the `awk '$2 == n'` of
  `resolve_kernel`) returns `bb1b871e...` for `linux-7.2.14.tar.xz`: the parsing is sound.
- 7.2.14 was released at 12:32 GMT (`last-modified` of the tarball), 5.5 h before the job, so this is
  not a release-propagation window.
- Not proven: why the runner's `sha256sums.asc` lacked the line (stale CDN edge copy is a hypothesis
  only). No change made.
- Same run, arm64 `linux-image` job, same second (18:06:00): the identical lookup resolved
  `upstream linux-image: KERNEL_VER=7.2.14 KERNEL_SHA256=bb1b871e...`. So the amd64 miss was a transient
  of that one fetch (the response lacked the line), not a deterministic parsing or data error.
  `curl --retry` does not cover it: the HTTP request succeeded, the content was short.

## Real run 41103751203 (debian-bootc, workflow_dispatch): categories read

- `latest-amd64`, `minimal-amd64`, `latest-arm64`, `minimal-arm64`: `Category: signed (… reference=1, secure_boot=1)`.
  arm64 logs `AAVMF CODE: /usr/share/AAVMF/AAVMF_CODE.secboot.fd` and `AAVMF VARS: …/OVMF_VARS.fd` (enforcing pair).
- All four: shim and grub load under Secure Boot (no 0x1A), then
  `error: kern/efi/sb.c:shim_lock_verifier_write:192:bad shim signature.` x3 and
  `VM did not become reachable within 60s` (amd64) / `300s` (arm64). This is the unsigned `vmlinuz` measured
  locally (`rc=45`), now seen on the real images for both architectures. Secure Boot stayed enforced.
- `latest-4k-arm64`, `minimal-4k-arm64`: no boot test; the image build stops at
  `apt install`: `linux-modules-rpi3:arm64=7.2.13-1+r1 … Depends daemoncores-kernel-7.2.13 (= 7.2.13-1)
  but none of the choices are installable`. Separate from Secure Boot; not analysed yet.
- amd64 `linux-image` had failed at resolution (section above), so the amd64 images used the last published kernel deb.

## 4k arm64 images: `linux-modules-rpi3/rpi4` pinned to a kernel that left the index (cause proven)

- Symptom (run 41103751203, `latest-4k-arm64`/`minimal-4k-arm64`): `apt install` fails,
  `linux-modules-rpi3 7.2.13-1+r1 Depends daemoncores-kernel-7.2.13 (= 7.2.13-1)`, no candidate.
  Pages `Packages` (arm64): rpi3/rpi4 modules are 7.2.13 and depend on `daemoncores-kernel-7.2.13`, while the
  index only carries `daemoncores-kernel-7.2.14`.
- `hashes.json`: `linux-modules-rpi3-arm64` hash `209ab93a...` -> deb `linux-modules-rpi3_7.2.13-1+r1`.
- That hash is the one of `KERNEL_VER=7.2.14`: recomputed locally, the env digest `e49e8580...` of the job log is
  `sort` of `HELPERS_VER=5 DEVICE=rpi3 KERNEL_VER=7.2.14 MODULE_SIGNING_ACTIVE=no` (7.2.13 and variants give
  other digests).
- Runs 41054040585 and 41062757704 tracked `KERNEL_VER=7.2.14` but the `linux-image` arm64 build had not
  produced 7.2.14 (last-good kept); their rpi3 job installed the dep from
  `/tmp/prior/linux-image-common_7.2.13-1_arm64.deb` and built `...7.2.13-1+r1` under the 7.2.14 hash.
- Run 41103751203: `linux-image` arm64 built 7.2.14, but rpi3's hash did not move (the dep's version is not an
  input of the hash): `cache[linux-modules-rpi3-arm64]: hash=209ab93a... prior=209ab93a...`,
  `unchanged; reused cached deb(s)`. rpi5 (hash differs) was rebuilt on 7.2.14.
- Cause: the content hash of a package never sees the dependency debs it is actually built against.
- Fix (`build-one-deb.yml`): the digest of the installed dependency debs is hashed as `DEPS_HASH`
  (step `depdebs`, composed in `hashenv`). Local replay of both scripts: a dep deb with other content gives
  another `DEPS_HASH`; no deps -> empty `extra-env` as before. One-time effect: every package with `deps`
  rebuilds once on the next run. Real check: next full run must rebuild rpi3/rpi4 on 7.2.14 and the 4k images
  must install.
- Real check, run 41118096830: `cache[linux-modules-rpi3-arm64]: hash=da642668... prior=209ab93a...` and
  rpi4 `hash=80e0281a... prior=46905193...` -> both `built`; the published arm64 index now lists
  `linux-modules-rpi3` and `-rpi4` at `7.2.14-1+r1`. Image jobs started afterwards (results below once read).

## Real run 41118096830: all six image jobs built; per-category boot results

- 4k arm64 images now build (the dependency hash fix holds). Category `uboot` (SB off by design):
  `AAVMF CODE: /usr/share/AAVMF/AAVMF_CODE.fd`, `AAVMF VARS: /usr/share/AAVMF/AAVMF_VARS.fd`.
- `latest-4k-arm64`: the guest boots (`Startup finished in 6.167s (kernel) + 1min 14.665s (initrd) + 2min
  15.255s (userspace) = 3min 36.088s`, TCG), port 2222 opens, then ssh stops at `Connection timed out during
  banner exchange` and the run ends at `VM did not become reachable within 300s`. Slowness of software
  emulation against a 300 s window; not Secure Boot. Not fixed, cause of the sshd delay not analysed.
- `minimal-4k-arm64`: `RAM idle budget exceeded: 204 MiB at idle over the 15 MiB budget` (known gate, the
  breakdown work is the uncommitted `ram_gate` block, not mine).
- `signed` (`latest`/`minimal` x amd64/arm64): unchanged, shim + grub pass under SB and the boot stops at
  `bad shim signature` (unsigned `vmlinuz`), arm64 on `AAVMF_CODE.secboot.fd`.

## Kernel EFI signature: arm64 bench and decision

- Local arm64 bench (SB enforced, shim + grub `g1.efi` + MOK `t.crt`, kernel loaded by grub `linux`):
  `Image.gz` as published -> `rc=45 bad shim signature`; raw `Image` (zcat of the published vmlinuz, already a
  PE `MZ`, `CONFIG_EFI_STUB=y`) unsigned -> `rc=45`; raw `Image` signed with `sbsign` -> `rc=0`, `grub>` prompt.
  A gzip stream cannot carry an Authenticode signature, so arm64 must ship the raw PE `Image`, signed.
  Cost: the arm64 kernel grows from about 15 MB to about 45 MB.
- Decision (debian-bootc `0e54237`): `linux-image` always signs the EFI kernel (`kernel_sign_efi`), for every
  kernel variant. `-nosign` only means no module signature enforcement (`MODULE_SIG_FORCE` off); shim, grub and
  vmlinuz stay signed for everyone. The `unsigned-kernel` categories now boot with Secure Boot enforced.
- Real check pending: full run, then read `linux-image` signing, boot tests `signed`/`unsigned-kernel` on
  amd64+arm64 (no more `bad shim signature`), U-Boot 4k arm64 with the raw `Image` (untested risk).

## Design check: one image with both 4k and 16k arm64 kernels (step 1.1, upstream source read)

- Decision from the owner: the U-Boot/embedded criterion must be an `-embedded` image variant, not the `-4k` tag
  suffix (page size is a kernel detail). Today `tests.yml` category `uboot` is selected by suffix `-4k` and the
  `-nosign` twin is skipped for the `-4k` image; no Containerfile carries `LABEL org.daemoncores.boot-profile`.
- Question: can one image hold both kernels and pick at boot? Upstream source (shallow clones of bootc-dev/bootc
  and ostreedev/ostree, read 2026-10-30):
  - `ostree/src/libostree/ostree-repo-os.c:67` `ostree_commit_metadata_for_bootable`: "Multiple kernels found in
    /usr/lib/modules" (commit-time error).
  - `bootc/crates/ostree-ext/src/bootabletree.rs:41` and `:86`: "Found multiple subdirectories in
    /usr/lib/modules"; `bootc/crates/lib/src/lints.rs:492` lints the same condition.
  - `ostree-sysroot-deploy.c` `get_kernel_from_tree` / `install_deployment_kernel` (l.1420, l.1834) yield one
    kernel layout per deployment, hence one BLS entry per deployment.
- Conclusion (MEASURED by source reading, not yet by a build): a two-kernel image is rejected at three layers.
  Supporting it means patching ostree (C: commit check, deploy, BLS generation) AND bootc (Rust) AND bootupd
  status/rollback logic: not a simple patch. This repo currently carries no bootc/ostree patch (only
  `bootupd-dpkg.patch` and the installer patches); both are built unpatched.
- Second blocker (ESTIMATED, not tested): no standard boot-time page-size detection. A 16k kernel on a core without
  the 16 KB granule (Pi 3/4) does not boot and falls back to nothing; selection would need a per-board map.
- Next: implement the `-embedded` variant (test category + twin logic keyed on it), keep one kernel per image.

## Full run 41129742013 (debian-bootc, after `0e54237`): results

- Progress (MEASURED): no `bad shim signature` in any of the 6 image jobs; latest-amd64 boots with Secure Boot
  enforced and runs the suite (43 PASS, 4 SKIP, 1 FAIL). `linux-image` jobs are green with `SB_SIGNING_KEY/CERT`
  exposed. arm64 `signed` logs `AAVMF_CODE.secboot.fd`; `uboot` logs `AAVMF_CODE.fd` (SB off, by design).
- latest-amd64: `signed kernel enforces module signatures` fails, `sig_enforce=N`, package
  `daemoncores-kernel-7.2.14 7.2.14-1`.
  - Proof: job "Prepare signing secrets" prints `module signing mode=both active=no unsigned_kernel=no; missing:
    cosign modules` then `PAT_SECRETS is not set: missing signing secrets cannot be generated`.
    `gh secret list` of the repo shows only `APT_GPG_KEY`, `SB_SIGNING_CERT`, `SB_SIGNING_KEY`: neither
    `PAT_SECRETS` nor `MODULE_SIGNING_*`/`COSIGN_*`. `ensure-secrets.sh` can only generate them with a token
    holding secrets:write, and `full-pipeline.yml` (`HAS_MODULES`) then keeps `signing_active=no`, which makes
    `kernel-signing.sh` build with `MODULE_SIG_FORCE` off. The test is legitimate; it cannot pass until the
    repository has `PAT_SECRETS` (then the next run creates the keys and the run after uses them).
  - Not caused by `0e54237`. No twin `-nosign` image is built in this state (`unsigned_kernel=no`).
- latest-arm64, minimal-arm64 (secboot): `VM did not become reachable within 300s`; serial log stops at about 4.6
  s guest, no panic, no shim error. Cause UNKNOWN (TCG slowness vs real hang): to reproduce on the local arm64
  bench before any fix.
- latest-4k-arm64 (uboot): the raw `Image` boots under U-Boot (userspace ready at 206 s guest) but the 300 s
  window expires (TCG slowness, known). minimal-4k-arm64 and minimal-amd64: RAM idle gate (205 / 330 MiB over 15).

## arm64 secboot "VM did not become reachable" (latest-arm64, minimal-arm64): reproduction status

- Observed (job logs of run 41129742013): serial output ends at `[    4.560027] PM: genpd: Disabling unused
  power domains`, nothing for the remaining 300 s window, no panic and no shim error. The `-4k` U-Boot job
  (SB off) prints systemd up to `Startup finished` at 206 s guest. Two differences between the cases (Secure
  Boot firmware and 16 KB vs 4 KB kernel) are confounded: the cause is NOT established.
- Local reproduction blocked: building and installing the arm64 image needs arm64 binaries to run on this
  amd64 host and `/proc/sys/fs/binfmt_misc/` has no `qemu-aarch64` handler (`qemu-aarch64` exists, not
  registered). Registering it needs root (one batched pkexec prompt). No arm64 disk image of this run is
  available (the image is built inside the job; artifacts hold only the `.deb` files).
- Next step once a root prompt can be answered: register the handler, `podman build --platform linux/arm64`
  the current Containerfile against the published APT repository, install to a disk as `image-test.sh` does,
  boot it with the bench (`AAVMF_CODE.secboot.fd` + enrolled varstore) and, to separate the two variables,
  also boot the same disk with `AAVMF_CODE.fd` (SB off, NOT a test change, a diagnostic only).
- No fix written: cause unproven.
