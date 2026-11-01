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

## arm64 timing comparison (job logs only, still no local reproduction)

- `latest-4k-arm64` (uboot): `Startup finished in 6.111s (kernel) + 1min 13.453s (initrd) + 2min 6.438s
  (userspace)`; sshd listening at guest 190.9 s, `port 2222 open`, then `Connection timed out during banner
  exchange`: the SSH step is lost to TCG slowness, the guest itself is healthy.
- `latest-arm64` (secboot, 16 KB kernel): after the same 300 s wall window the serial log ends at guest
  4.56 s. The captured serial of the 4k job only starts at guest 186.6 s, so the 4k boot cannot be compared
  with the secboot one before that point. Whether the secboot guest is silent in the initrd or stalled is
  still UNKNOWN. A local boot of the same disk is required; blocked on the binfmt prerequisite above.

## Run 41151560841 (debian-bootc, b87ef23): results

- Deb jobs: all skipped (conditional rebuild: nothing changed, no container started). 6 image jobs failed.
- Heavy layer (`apt install`, STEP 13/35) ran for real and logged `--> Pushing cache` (latest-amd64 00:44:22 to
  00:45:53): expected, the `DEBS_HASH` formula changed in `f3e89d5`, so this run only seeds the registry
  cache. The next run decides whether STEP 13 prints `Using cache` (NOT yet verified).
- latest-amd64: Secure Boot enforced, `signed kernel enforces module signatures` still fails; cause unchanged
  (no `PAT_SECRETS`, owner action).
- latest-arm64 (secboot, 16k): `port 2222 open` at the end of the 300 s grace period, then `Connection timed out
  during banner exchange`; serial tail still ends at guest 4.47 s (`PM: genpd: Disabling unused power domains`),
  after `Loaded X.509 cert 'Build time autogenerated kernel key'`. Same signature as the previous run; the
  guest stays silent for the rest of the window while QEMU accepts TCP on the forwarded port. Cause still
  UNKNOWN (stall vs serial output not flushed); the 16k + AAVMF vs 4k + U-Boot confound is not separated.
- Single-sleep design of `wait_ssh` (`image-test.sh:368-374`): one attempt after the grace period, so a guest
  that is merely slow under TCG is reported like a hung one. Not changed: needs the local arm64 boot first.
- Blocker unchanged: no `qemu-aarch64` binfmt handler on the host (needs one batched pkexec prompt).

## arm64: local bench separates the kernel, the page size and Secure Boot from the CI hang (MEASURED)

Method (no root, no arm64 execution): the published `daemoncores-kernel-7.2.14` (4k) and
`daemoncores-kernel-7.2.14-16k` debs were extracted with `dpkg-deb -x` from the APT repository
(`file`: "ARM64 boot executable Image, 4K pages" / "16K pages"), a static arm64 busybox from Debian
trixie was packed into a 1 MB initramfs whose `/init` prints `INIT-REACHED <uname -r>`. Same QEMU
flags as the CI (`-M virt -cpu max`, TCG, virtio disk and net).

| Boot path | 4k kernel | 16k kernel |
|---|---|---|
| `-kernel` direct (DT) | init reached, `genpd` 1.77 s, `Freeing unused kernel memory` 2.11 s | init reached, 1.80 s / 2.14 s |
| UEFI `AAVMF_CODE.no-secboot.fd` (ACPI) + GRUB | init reached, 1.74 s / 2.10 s | init reached, 1.77 s / 2.14 s |
| UEFI `AAVMF_CODE.secboot.fd`, SB ENFORCED, shim -> Debian signed GRUB -> the REAL signed kernel, its signing cert (`CN=debian-bootc Secure Boot Signing Key`, extracted from the kernel with `pe-listsigs -x`) enrolled as MOK | init reached, 1.78 s / 2.16 s | init reached, 1.79 s / 2.15 s |

Conclusions (proved by these runs):
- The signed 16k kernel boots under Secure Boot enforced with the CI's firmware on the TCG machine. The
  firmware, shim, signed GRUB, the kernel signature, the 16 KB page size and TCG are NOT the cause of the CI
  silence after `PM: genpd: Disabling unused power domains` (the CI never reaches `Freeing unused kernel
  memory`; every bench boot prints it 0.3 to 0.4 s after the `genpd` line).
- What the bench does not have and the CI has: the installed disk (GPT, btrfs/ostree root), the 25 MB real
  initramfs (`Freeing initrd memory: 25936K` is printed in CI), the real kernel command line. The CI log only
  shows the last 200 serial lines, so the command line is not visible there.
- Next proof needed: boot the real installed image. Blocked on the arm64 binfmt handler (root): the arm64
  image is never pushed (push runs after the boot test), so its layers cannot be fetched either.
- Proposed diagnostic for `image-test.sh` (diag only, no behaviour change): print the first 40 serial lines
  (kernel command line) in `dump_serial`.
- Diagnostic committed (`cb39aff`, diag only): `dump_serial` now prints the kernel command line, the first 40 serial lines and the capture size. Read from run 41155245822 (dispatched, queued at this tick).

## Run 41155245822 (`cb39aff`): first read of the real arm64 command line (MEASURED)

- Wall clock 15 min 46 s (01:25:02 to 01:40:48); every deb reused; the 6 image jobs fail.
- Heavy layer cache confirmed: `latest-amd64` STEP 13/35 logs `Cache pulled from remote
  ghcr.io/daemoncores/debian-bootc-cache-amd64` then `Using cache`.
- `latest-arm64` (16k): the serial capture is 329 lines / 27175 bytes. Kernel command line:
  `BOOT_IMAGE=(hd0,gpt2)/boot/ostree/default-…/vmlinuz-7.2.14-16k root=UUID=… rootflags=subvol=/root rw boot=UUID=…
  console=tty0 console=ttyAMA0 ostree=/ostree/boot.0/default/…`. GRUB 2.12 shows two entries
  (`ostree:0`, `ostree:1`) and boots the first. The kernel is the signed 16k one, ACPI boot, ttyAMA0 last (so
  `/dev/console` is the serial port).
- Open: the capture still stops after `genpd` while the bench reaches init; what the real initramfs does between
  the two is not visible in the 40-line head nor the tail. Hypothesis, NOT proved: the guest keeps running
  silently (root mount on virtio-blk btrfs under TCG) and `wait_ssh` gives up at 300 s.
- `latest-amd64`: SB enforced PASS; `signed kernel enforces module signatures` fails: no `PAT_SECRETS`.
- `minimal-*`: `RAM idle budget exceeded: 320 MiB (amd64), 206 MiB (arm64) over the 15 MiB budget` (RAM gate, other work).

## Run 41156912069: arm64 guest boots but never gets IPv4 (MEASURED, cause of the timeout NOT yet proved)

- `latest-4k-arm64`: the guest is alive (QEMU monitor: status running, X00 changes between samples), not hung.
  `Startup finished in 6.363s (kernel) + 1min 18.663s (initrd) + 2min 16.153s (userspace) = 3min 41.180s`
  (hosted arm64 runner has no /dev/kvm, so the guest runs under TCG).
- `networking.service: Failed with result 'timeout'` at 199.85 s; `timeout.conf` sets `TimeoutStartSec=30`.
- `/etc/network/interfaces` at 201 s contains `iface enp0s1 inet dhcp`, so the `dhclient -4` probe (8 s limit) DID
  get an answer; `ip addr` at 201 s shows only link-local/SLAAC IPv6 on `enp0s1`, no IPv4 and an empty `ip route`.
  The harness forwards host :2222 to the guest, which needs the guest IPv4; ssh then dies with
  `Connection timed out during banner exchange` although sshd listens from 01:57:45.
- Separate defect, proven: `tty_log` in `ifupdown2-autoconf` writes to a hardcoded `/dev/ttyS0`, which is not the
  console on arm64 (`tee: /dev/ttyS0: Input/output error`, serial 162.7 s and 163.1 s), so the probe diagnostics are lost.
- Hypothesis, NOT proved: under TCG the 30 s unit budget (ExecStartPre probes + `ifup` dhclient) is exhausted before
  the lease is applied. Diagnostic committed (`e8414f6`): the boot diag now dumps `journalctl -u networking` and the
  unit timestamps. No fix before that output is read.

## Secure Boot local e2e: state at 2026-10-31 (tick 1)

- Run 41158439518 (before the lib/patches refactor) was cancelled by the following push; its result is not
  readable. Run 41159448486 (Full Pipeline, debian-bootc `a015666`) dispatched: it validates the overlay-based
  rebuild of fisherman/bootc-installer/bootupd and the kernel helpers now hosted here.
- arm64 SB bench (above, MEASURED): signed 4k and 16k kernels boot to init with AAVMF_CODE.secboot SB enforced,
  shim -> signed GRUB -> kernel, signing cert as MOK. Not yet done: boot of the REAL installed arm64 image.
- Blockers for the real-image boot, both root: `image-test.sh` installs with `sudo podman run --privileged`
  (loop device + bootc install), and arm64 execution needs a `qemu-aarch64` binfmt handler
  (`/proc/sys/fs/binfmt_misc` has only DOSWin). amd64 has /dev/kvm and a local `latest_amd64` image (6bb0625),
  so the amd64 SB e2e only needs the root podman step.
- Next step: read run 41159448486 (arm64 AAVMF CODE/VARS lines, networking journal). No root popup issued while
  the owner is away.

## Run 41159448486 (debian-bootc a015666, after the lib/patches refactor): results (MEASURED)

- All deb jobs passed: the overlay-based rebuilds (bootupd, fisherman, bootc-installer) and the kernel helpers now
  hosted here work in CI. The kernels were rebuilt once (HELPERS_VER 5 to 6 invalidated their hash).
- 6 image jobs failed:
  - `minimal-amd64` / `minimal-4k-arm64`: `RAM idle budget exceeded: 316 MiB / 203 MiB over the 15 MiB budget`
    (RAM gate, other work). `minimal-arm64`: `VM did not become reachable within 300s`.
  - `latest-amd64`: `PASS booted with UEFI Secure Boot enforced`; `signed kernel enforces module signatures`
    fails with `sig_enforce` = `N`. Proven cause: the kernel job input is `signing_active: no` (linux-image job log,
    line 46) because `Prepare signing secrets` logged `PAT_SECRETS is not set: missing signing secrets cannot be
    generated (images are signed keyless, modules stay unsigned)`; `MODULE_SIGNING_KEY`/`MODULE_SIGNING_CERT` are empty.
    The run started before the owner stored the PAT. `gh secret list` shows only APT_GPG_KEY, SB_SIGNING_CERT,
    SB_SIGNING_KEY on debian-bootc (the org-level list is not readable with this token: HTTP 403).
  - `latest-arm64` (secboot, 16k) and `minimal-arm64`: `AAVMF CODE: AAVMF_CODE.secboot.fd`, `AAVMF VARS: .../OVMF_VARS.fd`,
    then `VM did not become reachable within 300s`. `latest-4k-arm64` (no SB): same timeout, `networking.service`
    `Failed with result 'timeout'` at 192.5 s and `tee: /dev/ttyS0: Input/output error` again.
- Owner statement (2026-10-31): arm64 is NOT missing IPv4 (supersedes the "no IPv4" reading above, which came from
  one `ip addr` sample at 201 s). The cause of the 300 s timeout is open.
- Run 41166078848 dispatched to see whether `PAT_SECRETS` is now seen (expect `Prepare signing secrets` to generate
  MODULE_SIGNING_KEY/CERT and the kernel to be rebuilt signed).

## Run 41168339038 (debian-bootc e10bd0f): signing now active (MEASURED)

- `Prepare signing secrets` generated MODULE_SIGNING_* and COSIGN_* from `PAT_SECRETS` (secrets created 03:50 UTC).
- `publish` (amd64) failed first on run 41166113688: reprepro `md5 expected e34cda96..., got 155a3d43...` for
  `linux-headers-7.2.14_7.2.14-1_amd64.deb`; the signed and -nosign kernels produce different headers debs under the same
  package name. Fixed in debian-bootc `e10bd0f` (-nosign headers get their own name, Conflicts/Replaces the signed
  one); validated on a mock deb locally, then publish passed in run 41168339038.
- `latest-amd64` there: `PASS signed kernel enforces module signatures` (sig_enforce=Y). Two test-logic defects then
  surfaced, both previously masked by unsigned modules:
  1. `signature enforcement follows the installed kernel` printed `unsigned kernel, sig_enforce=Y`:
     `dpkg-query -W 'daemoncores-kernel-*'` lists `daemoncores-kernel-7.2.14-nosign` with an empty version (known only
     through Conflicts). Reproduced in a debian:trixie container: `un zzfoo-nosign` is listed for a package that only
     appears in a Conflicts field. Fix: match `${db:Status-Abbrev}` `^ii `.
  2. `a signed module loads on either kernel`: `insmod ... ecdh_generic.ko: Unknown symbol in module`. insmod does not
     resolve dependencies and the fallback picked a module that has some; the signature was already accepted (module
     signature check precedes symbol resolution). Fix: pick a module whose `modinfo -F depends` is empty.
  Neither change relaxes an assertion: the enforcement and load checks are unchanged.
- arm64 (`latest-arm64` secboot 16k, `latest-4k-arm64`, `minimal-arm64`): still `VM did not become reachable within
  300s`; cause open (owner: not an IPv4 problem). `minimal-*` amd64/4k: RAM gate.

## 2026-10-31 run 41173677216 (6b7f11c): latest-amd64 second image-test invocation

- Symptom: first image-test invocation (signed image) logs all PASS, then `cat: /home/runner/work/_temp/qemu.pid: Permission denied`;
  the second invocation (`latest-nosign_amd64`) dies with `qemu-system-x86_64: cannot create PID file: Cannot lock pid file: Resource temporarily unavailable`
  (job log, error exit code 1). The two logic failures of the previous run (unsigned-kernel detection, module with dependencies) are gone: `PASS signature enforcement follows the installed kernel`, `PASS a signed module loads on either kernel`.
- Cause (PROVEN from the log + `image-test.sh:313,735`): QEMU is started with `sudo ... -pidfile ${WORK}/qemu.pid`, so the pidfile is root-owned;
  `cleanup()` ran `sudo kill "$(cat pidfile)"` with an unprivileged `cat`, which failed, `|| true` swallowed it, the VM survived and held the pidfile lock.
- Fix: `cleanup()` reads the pidfile with sudo, kills, and waits for the process to exit. To validate in the next CI run (amd64 job: both invocations must complete).

## 2026-10-31 arm64 "VM did not become reachable within 300s" (run 41173677216, latest-4k-arm64)

- Observed (job log): the guest DOES boot under TCG: `Startup finished in 5.890s (kernel) + 1min 15.022s (initrd) + 2min 33.194s (userspace) = 3min 54.108s`,
  `sshd[627]: Server listening on 0.0.0.0 port 22` at guest 221 s, `Reached target multi-user.target`. The runner then logs `port 2222 open` and the single SSH
  attempt fails with `Connection timed out during banner exchange` (ConnectTimeout=3 from `SSH_OPTS`, `image-test.sh:74`).
  `networking.service` also ends `Failed with result 'timeout'` (about 40 s, DHCPv4 had bound 10.0.2.15, DHCPv6 solicits unanswered): a separate defect, not what blocked SSH.
- Cause (PROVEN for the failing step, the log shows the exact message): `wait_ssh` slept the whole grace period then made ONE probe with a 3 s banner budget,
  on a TCG guest still busy with `console-setup`. amd64 (KVM) never hit it. Secure Boot is not involved (no `Security Violation`, init reached).
- Fix: `wait_ssh` polls until the deadline (probe `ConnectTimeout=30`, fail-fast checks kept); after a reboot it requires a boot id different from the previous boot so the
  guest being shut down is not mistaken for the new one. To validate in CI on the arm64 jobs.
- Still open: `networking.service` timeout and the hardcoded `/dev/ttyS0` in `ifupdown2-autoconf` (arm64 console is ttyAMA0).

## 2026-10-31 run 41174777202 (fc585a9): arm64 still unreachable after SSH polling

- Result: `latest-amd64` SUCCESS (both image-test invocations complete: qemu pidfile fix proven). arm64 `latest`, `latest-4k`, `minimal` still
  `VM did not become reachable within 300s`, now with polling: `Connection established` to 127.0.0.1:2222 then `Connection timed out during banner exchange`
  although the guest reports `Startup finished in 3min 33.673s` (guest clock) and sshd listening. The previous explanation (single early probe) was INCOMPLETE:
  polling did not change the outcome. The guest CPU is parked (same PC over three samples, IRQs masked in idle), so it is not busy.
- Hypothesis, NOT proven: `networking.service` ends `Failed with result 'timeout'` and ifupdown2 deconfigures the interface (hostfwd to 10.0.2.15:22 then gets no answer).
  Evidence missing: the guest's `ip addr`/`ip route` printed at boot by `ci-ssh-debug.service` was not shown by `dump_serial` (only filtered lines).
- Step: `dump_serial` now prints the guest's own `ci-dbg: ip addr` .. `/etc/network/interfaces` block. Read it in the next arm64 run before touching anything.

## 2026-10-31 run 41176049265: arm64 guest has NO IPv4 address (PROVEN) and why is being tested

- PROVEN (job 124465002463 `latest-4k-arm64`, guest block printed by the new `dump_serial` group): at guest 195 s `enp0s1` has only `inet6 fec0::...` (SLAAC) and
  `fe80::...`; there is NO `inet` address and `ip route` is empty. So the hostfwd to the guest's 10.0.2.15:22 can never answer, which is exactly the banner-exchange timeout.
  (Not a missing DHCPv4 offer: the same boot's journal shows `DHCPOFFER of 10.0.2.15 from 10.0.2.2` / `bound to 10.0.2.15` for the autoconf PROBE.)
- The probe (`ifupdown2-autoconf`) runs dhclient with a no-op hook, so it never configures the address. The address only comes from `ifup` (`inet dhcp`), started
  at 05:36:13 ("networking: Configuring network interfaces") and killed at 05:36:41 by `start operation timed out` (`timeout.conf`: `TimeoutStartSec=30`, counted from 05:36:11).
  The journal has no ifup/dhclient line in that window.
- Open: is ifup merely slow under TCG (ifupdown2 is Python, `Consumed 12.857s CPU time`) or blocked (left-over probe `dhclient[550]` still running in the unit's control group)?
- Experiment (temporary, `install-fs.sh`, marked): CI-only drop-in `TimeoutStartSec=300`. If networking then succeeds and SSH answers: slowness proven; the product fix is a
  timeout that does not depend on CPU speed (embedded boards are slow too), not a test-only drop-in. If it still fails: blocked ifup, look at the probe dhclient. Remove the drop-in after the run.

## arm64 run 41177213666 (TimeoutStartSec=300 experiment) - analysis

- latest-4k (uboot category, SB off): SSH answered at the edge of the 300 s window (wait started 06:44:17, `is-system-running: running` at 06:49:38). The networking timeout was therefore a real factor, and the TCG boot is ~300 s.
- latest (16k, signed, SB on): still unreachable at 300 s, serial ends at 4.65 s of kernel time (quiet systemd, no console output after) - consistent with "still booting", not with a firmware rejection (no Security Violation, `integrity: secureboot mode enabled`, db certs loaded).
- Separate defect: test `u-boot-tools present` failed on 4k because no Containerfile sets the embedded profile (generic = GRUB, `libubootenv-tool` only). The test could never pass on a generic arm64 build; changed (debian-bootc cb7c122) to accept the stack of the profile.
- Next: TCG boot-timeout multiplier 5 -> 12 and print the measured time-to-SSH, to learn the real boot time of 16k instead of guessing.

## Run 41178510567 (TCG ceiling 720 s) - results

- latest-4k: `SSH reachable after 298s` (MEASURED). My new test failed: `grub-efi-arm64-signed` is `hi` (held on purpose via APT_HOLD_PKG), not `ii`; the check now accepts `^[ih]i`.
- latest-arm64 / minimal-arm64 (16k, SB enforced): still `VM did not become reachable within 720s`, so slowness is NOT the cause (720 s vs ~300 s for 4k). QEMU monitor: VM running, CPU executing in EL1h with DAIF masked (PSTATE=...3c5), PC alternating, X01=0x96000005 (an ESR value, data abort at EL1). Serial stops at `PM: genpd: Disabling unused power domains` (kernel 4.65 s), `Freeing unused kernel memory` never printed. The local bench with a tiny initramfs prints it 0.3 s after genpd, so the stall depends on what the real image adds (25 MB initramfs, cmdline, disk).
- Next: dump the full register file and the code around PC from the monitor (image-test `guest_state`), match the instruction bytes against the 16k kernel Image to find the KASLR slide, resolve LR/PC with System.map.

## Run 41180405930 - 16k stall data (MEASURED) and instrumentation fix

- latest-4k now passes (SSH after 331 s, reboot test included). latest-arm64 / minimal-arm64 (16k): unreachable at 720 s again, same as at 300 s: not a speed problem.
- Monitor snapshot of the stalled 16k guest: PC alternates ffffe06e1397ef54 / ef6c (a 6-instruction loop), PSTATE=004003c5 (EL1h, DAIF all masked), X30=ffffe06e1397eac4, SP=fff00000fe988da0. The KASLR slide is unknown, so symbols cannot be resolved yet.
- The code dump requested in the previous run printed nothing: the monitor rejects `$pc-48` (checked locally with qemu: `x /8xw $pc` and `x /4xg $sp` work). guest_state now dumps `x /32xw $pc` and `x /16xg $sp`; the instruction words are searched in the kernel Image to get the slide.

## Run 41181984579 - instrumentation still empty

- The `x /32xw $pc` dump printed nothing again when sent in the same hmp.py invocation as `info registers` (whose reply is large). Run alone, the same two `x` commands print the memory locally (qemu, `ffffce423ff409b0: 0x14000002 …`). They now go in their own invocation.
- Re-measured 16k stall: PC alternates ffffd56efa57ef54 / ef6c, X30=ffffd56efa57eac4, SP=fff00000fe988da0 (same low bits as the previous run, new KASLR base).

## Run 41183530336 - 16k stall, second sampling (MEASURED + one inference)

- Serial identical: stops at `[4.726790] PM: genpd: Disabling unused power domains`, single boot, no panic/oops text.
- Registers this time: PC=ffffe2aab1e10a00 on all 3 samples, SP=X29=ffffc0008002be00, X30=ffffe2aab21292e8, PSTATE=004003c5 (EL1h, DAIF masked). A 2 MB-aligned base fits PC - 0x10a00 = ffffe2aab1e00000; with that base PC = `vectors+0x200` and LR = `memblock_find_in_range_node+0x1c` in the System.map of the 16k kernel found in the scratchpad (same version 7.2.14-16k; the CI build itself is not verified identical).
- Inference (NOT proven): `vectors+0x200` is the EL1h synchronous entry; SP=ffffc0008002be00 is the link-time (unrelocated) address of init_stack, so the vector entry faults on its own stack push and loops. This would be an exception taken with an SP that was not relocated.
- The memory dump is still empty in CI. The monitor reply is now printed unfiltered (plus gva2gpa for PC and SP) to see whether the address is unreadable.

## Run 41185182187 - 16k third sampling and a 4k reboot timeout

- This CI QEMU monitor answers `unknown register` for `$pc`/`$sp` (the local QEMU accepts them), which is why every memory dump was empty. The dump now takes the numbers from `info registers`; the same extraction was run locally against a booting 16k kernel (`gpa:` and `ffff…: 0x…` lines printed).
- 16k registers this time: PC=ffffe2fb8ac10a00 (again the EL1h synchronous vector entry, vectors+0x200 if the base is 2 MB aligned), SP=ffe00001fd30ff10 (not a linear-map address), X20=X01=0x96000005 (ESR: data abort, translation fault level 1), X30=0b9fe2fb8ac112b0 (high bits set: a pointer-authentication signature). So SP differs between samples (earlier ffffc0008002be00), the stall is an exception loop on an unusable stack.
- 4k: `SSH reachable after 340s`, then `VM did not come back after reboot within 300s`: reboot_timeout was still multiplied by 5. Both timeouts now use TCG_TIMEOUT_SCALE (default 12, overridable).

## arm64 16k stall: reproduced locally, cause = `-cpu max` under QEMU 8.2.2 TCG
- MEASURED: ubuntu:24.04 container (qemu-system-arm 8.2.2, same as the CI runner), AAVMF secboot, CI-identical 16k kernel (7.2.14-16k, built 2026-10-31 04:26:53), 26 MiB initramfs.
- `-cpu max` (also `max,pauth=off`, `sve=off`, `sme=off`, `lpa2=off`, and all four combined): stalls after `PM: genpd: Disabling unused power domains`, no `Freeing unused kernel memory`.
- `-cpu neoverse-n1`, `neoverse-n2`, `cortex-a76`: boot to INIT-REACHED (`Freeing unused kernel memory` at 1.69 s). `cortex-a72`: stalls early after 10 lines (not analysed; likely no 16k granule).
- QEMU 11.1.2 with `-cpu max` boots, so it is a QEMU 8.2.x TCG bug; the exact feature inside `max` is NOT isolated.
- Fix: arm64 TCG now defaults to `neoverse-n1` (TCG_CPU_ARM64 override). Secure Boot stays enforced. To validate: next Full Pipeline run, jobs latest-arm64 / minimal-arm64.

## Timeout scale back to 5 (MEASURED)
- With `-cpu neoverse-n1` the arm64 4k guest answers SSH after 105 s and after 115 s on reboot (run 41191163442, job latest-4k-arm64), against 298-340 s with `-cpu max`. The x12 scale only compensated for the slow CPU model; it is reverted to the original x5.
- Open proposal: replace the ceiling by a stall watchdog on the serial console (fail when the console is silent for N s) with SSH expected within seconds of boot end. Needs a boot-complete marker on the serial, not yet verified.

## TODO (owner-approved): boot timeout calibrated on previous runs
- Timeouts must never trip on a merely slow emulated boot, and a fixed multiplier is not a measure. Keep the manifest value as the first-run default, then use the last measured `SSH reachable after Ns` (per arch / page size) times a margin, persisted between runs (CI cache or artifact).
- Context: arm64 runners have no /dev/kvm, so TCG is the only mode there (run 41191163442 log: "/dev/kvm unavailable on arm64"). Measured with neoverse-n1: 4k boot 105 s, reboot 115 s.
- Related proposal: serial-console stall watchdog, SSH expected within 5 s of boot end (marker not yet verified).

## Run 41191163442 result (MEASURED, `-cpu neoverse-n1`, d56d067)
- `latest-arm64` (16k, AAVMF_CODE.secboot.fd, SB enforced): success; SSH after 110 s (boot) and 145 s / 111 s / 93 s on the following boots/reboots. `latest-4k-arm64` and `latest-amd64`: success. The 16k stall is closed.
- `minimal-arm64`, `minimal-4k-arm64`, `minimal-amd64`: boot OK (SSH after 80 s, 81 s, 18 s) then fail on the RAM gate only: 508 / 208 / 348 MiB used over the 15 MiB budget. Not a boot or Secure Boot failure; the gate is separate work.
- Wall clock of the run: 10:03:17 to 10:27:27 (24 min), with the kernels rebuilt.

## Experiment removed
- The `networking.service` TimeoutStartSec=300 drop-in in install-fs.sh is removed. In run 41191163442 no networking.service failure appears in the latest-arm64 log (grep for failed/timed out: no match); the 30 s product timeout is re-exposed to the next run, which will show whether it still trips under neoverse-n1.

## Run 41199518844 (debian-bootc 3f03653): Secure Boot state, tick 2 (MEASURED)
- Secure Boot is enforced and passing on both architectures, no firmware rejection anywhere:
  - `latest-arm64` (job 124534011106): `AAVMF CODE: /usr/share/AAVMF/AAVMF_CODE.secboot.fd`, `AAVMF VARS: .../OVMF_VARS.fd`
    (virt-fw-vars sets `SecureBootEnable: True`), `SSH reachable after 127s`, then PASS on the bootc/ostree/mount tests.
  - `latest-amd64` (job 124534011158): `PASS booted with UEFI Secure Boot enforced`, `PASS Secure Boot signing cert shipped`, SSH after 19 s.
- The only arm64 failure left in that run is not Secure Boot: `no failed systemd units` finds `networking.service` failed
  (`start operation timed out`, `CPU: 25.513s`, 30 s from `timeout.conf`). The journal shows `SyntaxWarning: invalid escape sequence`
  from `/usr/share/ifupdown2/ifupdown/utils.py:243` at boot, i.e. Python recompiles ifupdown2 on every boot.
- Image check (MEASURED, published `latest_arm64`, files copied without running it): 63 `.py` and 63 `.pyc` under `/usr/share/ifupdown2`;
  the pyc header mtime (1598623279) equals the source mtime, so the pycs are valid IN THE IMAGE LAYER.
- Hypothesis (NOT proven on the guest): ostree stores no file mtime, so the deployed tree has a different mtime and every timestamp-based
  pyc is stale (ostreedev/ostree#1469). Evidence needed before any fix: print `stat -c %Y` of `utils.py` and its pyc in the diag of the failing test.
- Candidate fix once proven: hash-based pycs (`python3 -m compileall --invalidation-mode unchecked-hash`) in the ifupdown2 overlay build.

## Tick 3: instrumentation for the networking timeout (not pushed)
- No local reproduction is possible without root (installing the image needs `podman --privileged`; no new pkexec while the owner is away), and the deployed mtimes only exist on the installed guest.
- debian-bootc `workflows/tests.yml` diag of `no failed systemd units` now prints the source mtime and the pyc header mtime of `ifupdown/utils.py` on the guest (diag only, the assertion is unchanged). The edit is in the debian-bootc working tree, NOT committed: the pre-commit `make docs-check` fails on citations in `docs/testing-quality.md` (packages.yml:1072/1122) caused by another agent's uncommitted packages.yml edits, not by this hunk. Commit once that work lands (no `--no-verify`); the push also waits for the other pending work, since a partial push breaks the pipelines.
- Expected reading in the next run: equal values = hypothesis false (look elsewhere); different values = hypothesis proven, then switch the ifupdown2 overlay to hash-based pycs.

## build-all.sh: `GITHUB_TOKEN: unbound variable` (run 41218539531)

- Symptom: both `Build all packages` jobs fail in 67 ms, `build-all.sh: line 241: GITHUB_TOKEN: unbound variable`.
- Proof: the step env lists `SECRETVAL_GITHUB_TOKEN` but no `GITHUB_TOKEN`; the workflow maps the secret only under that name (`bootc-debs-builder.yml:82`). The unit test exported `GITHUB_TOKEN` itself, which masked the gap.
- Fix: the script derives `GITHUB_TOKEN` from `SECRETVAL_GITHUB_TOKEN`; the test now supplies only the workflow's variable and drops `GITHUB_TOKEN`.

## Tick 4: waiting for the full run (state)
- Secure Boot needs no further work: enforced and passing on both architectures in run 41199518844 (see above). Nothing was disabled or weakened.
- Run 41219400047 (debian-bootc 1b73370, CI f1b04f5+token fix) is the first one exercising the single-job-per-arch pipeline. Next reading: arm64 `.pyc` mtime diag line (proves or refutes the stale-pyc hypothesis), AAVMF CODE/VARS lines, one job per arch.

## build-all.sh: `bootupd` fails with `/build/rust/env: No such file` (run 41219400047)

- Symptom: `bootupd (amd64)` fails right after the vendor tarball check, `bash: line 24: /build/rust/env: No such file or directory`.
- Proof: the Rust toolchain is baked into the build image at `CARGO_HOME=RUSTUP_HOME=/build/rust` (`debian-bootc/workflows/env.yml:19-20,115`); `build-all.sh` ran `rm -rf "${BUILD_DIR}"/*` after each built package, which deleted it before `bootupd`.
- Fix: the entries of `BUILD_DIR` are recorded at start and only entries added since are removed; `tests/test_build_all.py::test_cleanup_keeps_what_the_build_image_ships` covers it.

## Conditional rebuild lost: `prior=<none>` for every package (run 41219400047)

- Symptom: `cache[...]: prior=<none>` for libcomposefs/ostree/fisherman/linux-image although the computed hashes equal those of run 41199518844 (where they were reported unchanged and reused).
- Proof: hash parity holds; the published `hashes.json` had been overwritten by run 41218539531, whose build jobs all failed. `bootc-debs-end/action.yml` rebuilds `hashes.json` and the APT repo from `debs/.meta/*.json` of the current artifact only, and the `publish` job runs with `always() && !cancelled()` (`bootc-debs-builder.yml`), so an empty artifact published an empty cache and repo.
- Fix: new input `carry_over` (set when `needs.build.result != 'success'`) runs `bootc-debs-end/carry-over.sh` before the repo is created: every package of the published `hashes.json` without metadata in the artifact is downloaded back from `debs-cache/` and recorded as reused. A fully successful build keeps the artifact authoritative, so removed packages still disappear. Covered by `tests/test_carry_over.py`.
- Status: not yet confirmed on a real run; confirm with `prior=` values after the next failed-then-rerun sequence.

## Run 41227506511: `gpu-userspace-*` build-dep and `linux-modules-nvidia` (amd64)

- Mesa symptom: `builddeps:mesa : Depends: directx-headers-dev (>= 1.619.1) but 1.614.1-1 is to be installed` for all six variants.
- Proof: the diag lists four mesa source records (25.0.7-2+deb13u1 from trixie; 25.2.6, 26.1.2, 26.1.6 from trixie-backports, enabled by `preinstall/etc/apt/sources.list.d/debian.sources:3`). `apt-get build-dep mesa` resolves the newest source record (26.1.6), whose dependency needs the backports-only directx-headers 1.619.1, while the built version is the binary candidate 25.0.7. Reproduced locally in the build-env image with that sources file: `apt-get build-dep -s mesa` fails with the same message, `apt-get build-dep -s mesa=25.0.7-2+deb13u1` succeeds. The earlier local success came from a sources file without backports.
- Fix: `packages.yml` pins build-dep to the version it builds (`mesa=${DEB_MESA#*:}`).
- `linux-modules-nvidia` symptom: `find: '/tmp/prior': No such file or directory`, job failed right after installing the kernel common deb. Proof: `scripts/kernel_prepare.sh` searched only `/tmp/prior`, the directory the former one-job-per-package pipeline filled; `build-all.sh` keeps the debs in `DEBS_DIR` (`/debs`, where apt just installed `linux-image-common` from). With `pipefail`, the missing directory aborts the step.
- Fix: `kernel_prepare` searches `DEBS_DIR` then `/tmp/prior` (existing directories only); `build-all.sh` exports `DEBS_DIR`. Covered by `tests/test_kernel_prepare.py`.
- Also in this run: `bootupd` built (cd6e1a6 fix confirmed); `linux-image`, `fisherman`, `libcomposefs`, `ostree`, ... printed `prior=<hash>` equal to the computed hash and were reused (conditional rebuild working with an intact cache). `linux-image-nosign` was rebuilt (prior hash differs from the computed one: its inputs changed, not yet explained). The `bootc` package took about 10 min (18:10:17 to 18:20:10).

## Single-job deb phase reverted; waves restored (owner decision)
- Measured (run 41229165698, cancelled at 48 min): one sequential job per arch serialised the slow independent builds: each `gpu-userspace-*` Mesa variant took about 18.5 min (18:30:43 to 18:49:21, then to 19:07:40), `linux-image-16k` on arm64 39 min. The owner asked to chain only what shares a cache and parallelise the rest, not to put everything in one job.
- Action: `2b71c21` is reverted for the deb workflow (`bootc-debs-builder.yml` waves, `build-one-deb.yml`, `full-pipeline.yml`, README, `bootc-debs-end/action.yml` comment). `carry_over` is kept and now derives from the wave results. `build-deb/build-all.sh`, `plan.py` and the image-side single-job work are untouched.
- Next: declared chains (`chain:` key in `packages.yml`) built on top of the waves, so that packages sharing the kernel cache run in one job after their wave dependencies.

## Tick 5: run 41233619804 in progress (waves restored)
- Secure Boot: nothing to do, still enforced and green on both architectures (run 41199518844). Nothing disabled or weakened.
- Wave 0 timings read from the jobs API (MEASURED): `linux-image` arm64 52 s (reused), `bootupd` 1 min 39 s, `linux-image-nosign` amd64 4 min 17 s, `linux-image-16k-nosign` arm64 6 min 57 s, `gpu-userspace-*` Mesa variants 11 to 15 min each in parallel (gaming-generic arm64 12 min 47 s, gaming-x64v3 15 min 22 s); `gaming-x64v1`, `gaming-x64v2` and `linux-image-16k` arm64 were still running after 20 min.
- Still pending the end of the run: arm64 `.pyc` mtime diag line and the AAVMF CODE/VARS lines.

## Tick 6: Secure Boot unchanged, run 41233619804 still in progress
- Secure Boot: no action, state of tick 2 stands (enforced and green on amd64 and arm64, run 41199518844); nothing disabled or weakened. The AAVMF CODE/VARS lines of run 41233619804 are not available yet (image jobs not started).
- New failure in the run (MEASURED, wave1 `linux-modules-nvidia` amd64, log 19:44:54Z): `ERROR: a file touched by the patches changed upstream`, sha256 changed for `include/linux/sched.h`, `kernel/exit.c`, `kernel/fork.c`, `kernel/sched/core.c`, `kernel/sched/fair.c`, `kernel/sched/sched.h`, then `no prior deb to fall back to`. Cause not yet proven (which patch set and which kernel version produced the drift); no fix written.

## Run 41233619804 result (MEASURED, failed after 57 min)
- Secure Boot still green: arm64 `AAVMF CODE: /usr/share/AAVMF/AAVMF_CODE.secboot.fd`, `AAVMF VARS: .../OVMF_VARS.fd`; amd64 `SecureBootEnable: True`, SSH after 18 s, all bootc/ostree/mount tests PASS.
- amd64 image test fails `no failed systemd units`: the only failed unit is `dc-gpu-apply.service` (`failed failed`, job 124642365724 log 20:13:00Z). `networking.service` is fine there (`Finished`, DHCPACK).
- arm64 fails the RAM idle gate (510 MiB over 15 MiB), the other party's work, not Secure Boot.
- `linux-modules-nvidia` wave1 failure: see Tick 6.
- Cause of the `dc-gpu-apply` failure NOT proven: the diag listed the unit but printed no log. `dc-gpu apply` run in a trixie container exits 0 both with and without an installed `gpu-userspace` prefix, so the script alone does not fail; the guest context differs (sysinit ordering, read-only paths). No fix written.
- Instrumentation (debian-bootc `workflows/tests.yml`, both failed-units diags): status and journal of every failed unit. Local, not pushed; the next run will show the journal.

## Tick 7: conditional rebuild confirmed on a second run (MEASURED, run 41238278585, no package change)
- Wave 0 of both architectures completed within about 90 s (setup 20:18:47Z, last wave0 job 20:20:28Z). Every `gpu-userspace-*` variant took 30 to 45 s (11 to 15 min in run 41233619804), `linux-image-nosign` amd64 58 s (4 min 17 s), `linux-image-16k-nosign` 48 s (6 min 57 s): the packages were reused, not rebuilt. The `80ebca9` fix holds with an intact cache.
- Not yet read: the per-package `unchanged; reused` log lines, wave 1 (`linux-modules-nvidia`, rebuilt because it failed last time with no prior deb), image jobs and the `dc-gpu-apply` journal.

## Tick 8: final result of run 41238278585 (MEASURED)
- amd64 image test: `no failed systemd units` is PASS (log 20:41:05Z) and after rollback reboot PASS (20:41:38Z). The `dc-gpu-apply.service` failure of run 41233619804 did not reproduce; cause stays unproven, no corrective change made.
- amd64 `iso` image build fails at STEP 15/21 (`apt install ... bootc-installer`, log 20:42:41Z): `dpkg-deb: ... member 'data.tar': internal gzip read error: invalid distance code`.
- Proof the published `.deb` is corrupt: downloaded `pool/main/b/bootc-installer/bootc-installer_3.0.0+git0290d4f+bootc8_all.deb` from the apt repo, sha256 `4e7947b6...ae58a` and size 42111900 equal the `Packages` index entry (so the transfer is not the cause); `ar x` then `gzip -t data.tar.gz` -> `invalid compressed data--format violated`. The file was already corrupt when it was indexed.
- `nvidia` image build fails (20:43:02Z): `Unable to locate package linux-modules-nvidia` (consequence of the wave1 patch-drift failure, Tick 6) and `nvidia-persistenced` has no installation candidate (not yet investigated).
- Where the corruption is introduced is NOT yet proven. Candidates: the `tar -czf /tmp/data.tar.gz` / `ar qc` step in `workflows/bootc-debs-builder/packages.yml` (bootc-installer build), the artifact upload/download between jobs, the pool publish. Next: find the job that built it and compare its artifact against the published file.
- MEASURED: the artifact `debs-bootc-installer-amd64` of the same run (sha256 `c2b62518...c6a`) passes `gzip -t` on its `data.tar.gz`; it differs from the corrupt pool file (`4e7947b6...`). So the build step of this run is not corrupting; the corrupt file was produced or stored by an earlier run/publish. Origin still unproven; no corrective change made.

## Tick 9: cause of the corrupt published bootc-installer deb PROVEN, fixed
- MEASURED: the artifacts `debs-bootc-installer-amd64` (42111900 B) and `debs-bootc-installer-arm64` (42110188 B) of run 41238278585 hold the same file name (`Architecture: all`) with different bytes. `bootc-debs-end` downloads `debs-*` with `merge-multiple: true`; the log shows all downloads starting in parallel to the same `debs/` path (publish job 124648086564, 20:25:00Z).
- Proof: comparing the published file with both artifacts in 64 KiB chunks: 608 chunks equal the amd64 build only, 22 the arm64 build only, 13 neither (boundaries). The published file is a mix of two builds, hence `invalid distance code`.
- Fix (`bootc-debs-end/merge-artifacts.sh`, `action.yml`): each artifact is downloaded into its own directory, merged deterministically (first artifact by name wins, every dropped duplicate printed) and every deb must unpack (`dpkg-deb --contents`) or the job fails before anything is published.
- Validated locally on the real artifacts: merge keeps the intact amd64 copy (sha256 `c2b62518...`); the real corrupt pool file placed in an input directory is refused (`does not unpack`, rc=1). Tests: `tests/test_merge_artifacts.py` (2 passed) and `tests/test_carry_over.py`.
- Not yet validated in CI: the next full run must show the `duplicate bootc-installer_...` line in the end job and a sound `iso` image build.
- Secure Boot: no change this tick; enforced and green in run 41238278585 (nothing disabled or weakened).

## Tick 10: run 41241640416 (MEASURED): the unpack gate caught a reused corrupt deb
- Jobs `publish` (amd64/arm64) and `Publish .deb packages (APT repo)` failed at `ERROR: debs/bootc-installer_..._all.deb does not unpack` (logs 21:07:44Z-21:08:19Z). The new merge worked: `duplicate bootc-installer_...: kept the copy of an earlier artifact, dropped debs-bootc-installer-arm64`.
- Cause of the kept copy being corrupt, PROVEN: the artifact `debs-bootc-installer-amd64` of this run has `.meta` `"skipped": true` and sha256 `4e7947b6...` = the corrupt pool file of run 41238278585. The conditional rebuild restored the published deb (`restore_cached`, `build-deb/content-hash.sh`) without checking it, so the corrupt file would be republished forever.
- Fix: `restore_cached` now runs `dpkg-deb --contents` on every restored deb; a deb that does not unpack is deleted and counts as a cache miss, so the package is rebuilt (the last-good fallback fails the job honestly if the published copy is corrupt). Tests: `tests/test_restore_cached.py` (sound deb reused, corrupt deb is a miss), real corrupt pool file -> rc=1 with the warning.
- Not yet validated in CI: next full run must show `cached bootc-installer_... does not unpack; discarding it`, then a fresh build of `bootc-installer`, a sound publish and a sound `iso` image.

## Tick 11: run 41243363879 and the nvidia module failure (PROVEN)
- MEASURED: the unpack gate works (`bootc-installer` rebuilt on both architectures, publish green). Deb phase 9 min 40 s with a warm cache; see OPTIMIZATION.md.
- `linux-modules-nvidia` amd64 (job 124662190894) fails with `ERROR: a file touched by the patches changed upstream` (`kernel_helpers.sh:223`). The log lists six files whose sha256 differ from `patches.lock.json`. The guard runs before any patch is applied, so this is not a patch that fails to apply.
- Cause PROVEN: the lock was generated on an older kernel than the one the build resolves (7.2.14). Local run of the refresh logic on 7.2.14 produced the same 13 keys, six hashes changed, and those six equal the "new" hashes printed in the failing log (sched.h `6b67ef1d`, exit.c `a3a81f3e`, fork.c `c521c5b3`, sched/core.c `98fc0556`, sched/fair.c `22b28eeb`, sched/sched.h `e10a11a2`).
- Why the automatic refresh never fixed it (each PROVEN by a local or CI log): (1) it sourced `lib/kernel-source.sh`, which no longer exists; (2) it used the bare latest stable kernel 7.3.1, whose CachyOS BORE patch URL returns 404; (3) wrong argument order for `kernel_apply_patches`; (4) `kernel_fetch_source` changes directory so relative paths broke; (5) it deleted the lock, and the guard only runs when a lock file exists, so the result was `{}`.
- Fix `4be7dbf` (DaemonCores-CI): sources `scripts/kernel_helpers.sh`, resolves the version with `upstream-track` (`require-patches`) like the kernel build, absolute paths, seeds `{}`, fails on an empty lock. Validated locally, then in run 41246396958: the branch with the new lock was pushed, identical to the local result. The PR creation was refused by the repository setting (`GitHub Actions is not permitted to create or approve pull requests`); the lock was applied to `main` as `b471d05`.
- Not yet validated: run 41246555169 must show `linux-modules-nvidia` green and the `nvidia` image building. `nvidia-persistenced` without candidate (seen in the `nvidia` image build) is unexplained.
- Secure Boot: untouched this tick, enforced in every image job.


## Tick 12: run 41246555169 and the nvidia module build (PROVEN)
- MEASURED: the patch guard no longer blocks (lock `b471d05`). Run 41246555169 still failed: `linux-modules-nvidia` amd64 (job 124674294961) stops at `make modules` with `/tmp/linux-7.2.14/include/config/auto.conf: No such file or directory` (log line 1085). `Build images` amd64/arm64 failed too (not yet analysed; arm64 idle-RAM check is the earlier unrelated finding).
- Cause PROVEN locally on the pristine 7.2.14 tarball: `make defconfig`, `rm -rf include/config`, then an external module build (`make M=<dir> modules`) prints the identical `include/config/auto.conf: No such file` error. After `make olddefconfig` the auto.conf error is gone; after `make modules_prepare` the dummy module builds to `d.ko`.
- Why only nvidia: the device entries run `make olddefconfig` before building in-tree modules (packages.yml, linux-modules-arm-device); the nvidia entry went from `kernel_prepare` (which only copies the seeded `.config`) straight to an out-of-tree `make -C kernel-open`, so the tree was never configured.
- Fix (debian-bootc packages.yml, linux-modules-nvidia): `make olddefconfig` + `make modules_prepare` after `kernel_signing_setup`. Reproduced and validated on the dummy module only; the full nvidia build is validated by the next CI run.
- Open: patch guard design (owner asked for a commit-free dynamic variant; awaiting his decision), `Build images` failures, `nvidia-persistenced` without candidate.

## Tick 13: Secure Boot check, nothing to fix (MEASURED)
- Run 41249914654, job 124677197190 (`ia2.log` lines 3132-3133): `set variable SecureBootEnable: True`, and the signed image booted to the test stage under enforced Secure Boot; the only failing test was `no failed systemd units` (`dc-gpu-apply`, `ldconfig: Renaming of /etc/ld.so.cache~ ... No such file or directory`).
- No local Secure Boot reproduction is needed: no boot failure exists to reproduce. Nothing disabled or weakened.
- Open: the `dc-gpu-apply` cause is not proven. The failed-units diag now prints `ld.so.cache` state, every `ldconfig` run of the boot and the unit ordering; run 41250424265 should show them. A local concurrent-`ldconfig` loop reproduces the same message, which proves the mechanism is possible, not that it is the cause.
- Fixes pushed since the last run: nvidia `olddefconfig` + `modules_prepare` (`dbd3dfe`), nvidia `Module.symvers` in the kernel output tree (`660399e`).

## Tick 14: runs 41250424265 and 41253579167, nvidia module and image (PROVEN)
- `linux-modules-nvidia` run 41250424265 (job 124683083634): `No rule to make target nvidia/nv-kernel.o_binary`. Cause PROVEN: the repository-root `Makefile` (lines 36-58) builds `src/nvidia` and links `kernel-open/nvidia/nv-kernel.o_binary` before descending; the entry called `make -C kernel-open` directly. Reproduced locally; fix `b20e6d4` builds and installs from the root.
- `nvidia` image (job 124685016618): `nvidia-persistenced` has no installation candidate. Cause PROVEN from `deb.debian.org/debian/dists/trixie/{main,contrib,non-free}/binary-amd64/Packages.xz`: the package exists only in `contrib` (550.163.01-1); the images' `debian.sources` listed `main non-free-firmware non-free`. Fix `3b6bf9a`: `contrib` added.
- Run 41253579167 (after both fixes) fails the module build later (job 124692453677): `nvidia/nv-nano-timer.c: os-interface.h: No such file`. Reproduced locally (kernel 7.2.14 tree in the scratchpad, NVIDIA tag 550.163.01). Cause PROVEN: the driver Kbuild sets include paths through `EXTRA_CFLAGS` (`kernel-open/Kbuild:85-89`) and `/usr/bin/grep -rn EXTRA_CFLAGS scripts/` on the 7.2.14 tree has no hit: the kernel no longer reads it. Renaming it to `ccflags-y` locally only moves the failure to hundreds of API errors (`radix_tree_replace_slot`, `dma_buf_export` conftests).
- Tag 615.78.08 (newest upstream tag, commit date 2026-10-07) built on the same tree fails with two removed kernel APIs only: `del_timer_sync` (16 uses, `common/inc/nv-timer.h:58`) and `hrtimer_init`. So no upstream tag builds on kernel 7.2.14 unmodified, and the Debian user space is 550.163.01 (module and user space versions must match).
- OPEN, owner decision needed (module version vs user space vs kernel); see Handoff-Decisions.
- arm64 images: `ram_gate` default 15 MiB (`image-test/lib.sh:27`) vs 508/510 MiB measured (MemTotal-MemAvailable); amd64 runs with the gate disabled (`RAM idle gate disabled`). Not touched (owner's gate).
- amd64 image `no failed systemd units` PASSED in run 41250424265 (it failed in 41249914654): `dc-gpu-apply` failure is intermittent, cause still unproven.

## Tick 15: VE startup_failure and the kernel patch guard (PROVEN)
- VE `Full Pipeline` (runs 41250321169, 41267485270) ended `startup_failure` with the same permissions as the working debian-bootc caller. Cause PROVEN by comparison: VE `pipeline.yml` passed `gpu_driver_images`, which `full-pipeline.yml` (inputs, lines 14-69) no longer declares (`gpu_driver` since `2bb3b58`). Fix VE `86c81d4`; validation: run 41267556092 left the startup stage (in_progress).
- P-603: lock and refresh workflows removed, `patch --fuzz=0`. PROVEN locally that the default fuzz hides a moved context: `patch --dry-run` prints "Hunk #1 succeeded at 1 with fuzz 2", `--fuzz=0` prints "FAILED". Test `test_moved_context_is_refused_not_fuzzed` added (debian-bootc `bc33fd0`), pytest 5 passed. Not yet validated by a full run.
- debian-bootc `Lint` failed on `tests/unit/test_locale_keyboard.py` (pydoclint DOC101/DOC103 on three tests, commit `3998654`); docstrings completed (`8045ef1`, local pydoclint clean), pushed after the running pipeline ends so it is not cancelled.

## Tick 16: run 41267411283 and 41270183067 (PROVEN)
- `dc-gpu-apply` cause PROVEN (run 41267411283, job 124737780329, journal): `dc-gpu-apply` starts at 03:36:53.379864 and `ldconfig.service` at 03:36:53.385806; `dc-gpu` fails with `ldconfig: Renaming of /etc/ld.so.cache~ to /etc/ld.so.cache failed: No such file or directory` while `ldconfig.service` finishes with success. Two concurrent ldconfig runs race on the shared temporary file. Fix: `After=ldconfig.service` in `dc-gpu-apply.service` (debian-bootc `26ead49`). Run 41270183067 amd64 (job 124746566367): no `image test failed [tests]` line and no `dc-gpu-apply` failure; the only error left is the RAM gate.
- rpi3/rpi4/rpi5/rk3588 modules: patch `rocknix-btrtl-rtl8733bu` refused at fuzz 0 (job 124736677681: `ERROR: patch rocknix-btrtl-rtl8733bu does not apply to 7.2.14`). Fix: per-patch `fuzz:` (`74d4262` here, `26ead49` there). Run 41270183067: all four jobs passed.
- `linux-modules-nvidia`: with the full `Module.symvers` the module compiles (job 124744357953, no modpost error); packaging then failed: `dpkg-deb: 'Depends' field, reference to 'daemoncores-kernel-7.2.14': version contains 'V' instead of ')'`. Cause: the nvidia sed did not substitute `{{ VER }}` in the Depends line. Reproduced the sed locally: output `Depends: daemoncores-kernel-7.2.14 (= 7.2.14)` after the fix. Fixed in `982a16e`.
- Images: both fail only on the RAM gate (amd64 339 MiB, arm64 502 MiB, budget 15). Breakdown amd64: processes ~100 MiB RSS, slabs ~30 MiB; the rest of MemTotal-MemAvailable is not process memory. NOT weakened, owner decision on the gate pending.
