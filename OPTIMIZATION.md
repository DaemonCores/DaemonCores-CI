# CI/CD optimization plan

Source of every number: run 41129742013 of `debian-bootc` (workflow_dispatch, 21:16 to
22:20 UTC on 2026-10-30, 64 min wall clock). MEASURED = read from job logs/API,
ESTIMATED = derived, UNKNOWN = not measured. Each lever is closed only when a local or CI
measurement replaces its estimate; results go to the "Results" section.

## Baseline (MEASURED)

| Item | Cumulated job time |
|---|---|
| arm64 kernels: `linux-image` 1737 s, `linux-image-16k` 1771 s, 4 device modules 912-1031 s | 7404 s |
| amd64 kernel | 949 s |
| 6 image jobs (base 50-64 s, build 246-371 s, boot test 146-388 s) | 4014 s |
| Non-kernel deb jobs (22 jobs, all "unchanged; reused cached deb(s)") | about 40 s each |

Critical path: arm64 kernel (1771 s) then device modules (about 1000 s) then images (about 850 s).
The signed/unsigned twin (`-nosign`) was not built in this run (no `PAT_SECRETS`,
so no module signing key); with the twins active the cumulated time is about 290 min (ESTIMATED).

## Findings

1. **Conditional rebuild works at deb level.** 22 of 28 deb jobs logged
   `unchanged; reused cached deb(s)`. The 6 that rebuilt are the kernels and the four
   `linux-modules-*`, because commit `0e54237` changed `lib/kernel-signing.sh`, which is in
   their `sources:`. What is wasted: each reused job still starts a container
   (`Initialize containers` 22-29 s) and takes 30-50 s.
2. **No compiler cache anywhere.** No `ccache`/`actions/cache` in the workflows. Every
   `linux-modules-*` job runs `make -j"$(nproc)" modules` from a freshly downloaded source
   (`packages.yml:418-`), about 985 s, although its objects are nearly identical to `linux-image`.
3. **Image layer cache does not cross jobs.** The heavy `RUN apt update && apt install`
   (STEP 13/34) ran for real in every image job: 103 s amd64, 142 s arm64. Within one job the
   second build pass logged 25 `Using cache`. Cause of the registry-cache miss (read in
   `bootc-build.yml` `chash` step): `DEBS_HASH` fingerprinted every deb of the arch, kernels
   included, and is declared before that `RUN` (`Containerfile:71`). Any kernel rebuild changed
   it and re-ran the heavy layer, which does not install a kernel. Fixed in `f3e89d5` (CI) and
   `b87ef23` (Containerfiles): `DEBS_HASH` now excludes `linux-image-*`/`linux-modules-*`, a new
   `KERNEL_DEBS_HASH` sits before the kernel layer. Local proof: a 2-layer build where only the
   second ARG changes reuses layer 1 and rebuilds layer 2; changing the first ARG rebuilds both.
4. **`--cache-to` pushes every layer**: each trivial step (`LABEL`, `ARG`) takes 2-3 s.
   About 80 s of the 285 s `Build` step (ESTIMATED).
5. **Boot test dominates per-image time** on arm64 (367-388 s, TCG), amd64 146 s.
6. The kernel layer is already after the heavy package layer (`Containerfile:192`); moving it
   last saves about 30 s only. The `-ia` variants must come after it (`linux-modules-nvidia`
   is built against that kernel).

## Levers

| # | Lever | Estimated gain (cumulated) | Status |
|---|---|---|---|
| L1 | One kernel build for signed and `-nosign`: `MODULE_SIG_FORCE=n` in the kernel; module-signature enforcement becomes an INSTALL-TIME variable of the bootc-installer (it writes `module.sig_enforce=1` as an install karg, default on), so the `-nosign` twin disappears (no twin kernel, no twin image). Adding the karg at install avoids the bootc rule that forbids removing a shipped karg | kernel builds only: about 45 min of 290 (amd64 about 950 s + arm64-16k about 1770 s) plus the 4 twin image jobs | ORDERED by the owner: to develop. Recorded trade-off: a karg is editable on the GRUB command line, a compiled-in `MODULE_SIG_FORCE` is not; whether lockdown or a GRUB password closes this is UNVERIFIED |
| L2 | Shared `ccache` for all kernel/module builds (enabled for the packages built with `lib/kernel-signing.sh`, i.e. the kernel compilers, `actions/cache`, compiler masquerade via `/usr/lib/ccache`, `CCACHE_BASEDIR=/tmp`) | local: cold 142 s to warm 10-12 s on 16 cores for the project config (vmlinux + modules, fresh source tree, 2126/2126 second-pass hits, 83 MB cache); CI estimate: kernel/module jobs from 912-1771 s to a few minutes when the cache is warm | implemented, awaiting CI run |
| L3 | Chain the variants of one Containerfile+arch in one job | already done: `bootc-build.yml:611-625` builds the image, its `-nosign` twin, `-gaming` and `-ia` in one `Build` step on shared local layers; nothing left to gain | closed (no change) |
| L4 | Skip unchanged debs in the setup job instead of starting a container per package | 22 jobs x 30-50 s, plus shorter waves | ORDERED: to develop |
| L5 | Build without per-layer `--cache-to` inside a chain (push the final layers once) | about 80 s per image job | ORDERED: to develop (measure first) |
| L6 | Smoke test only for package-only variants (gaming/ia); full boot test per (arch, kernel) | up to 6 min per variant on arm64 | ORDERED: to develop |
| L7 | Conditional rebuild of kernels on upstream version + our sources only (already hashed); stop invalidating the kernels for edits that do not change them | wall clock below 15 min on a no-change push | measured: run 41155245822 took 15 min 46 s with every deb reused; ORDERED: to finish |
| L8 | Per-package dependencies for the waves (wave1 modules wait for wave0 kernels, wave2 `bootc` waits for all of wave1) instead of whole-wave barriers | shorter critical path | ORDERED: to develop |
| L9 | GPU: ONE image per generic amd64 variant carries the drivers of EVERY vendor (AMD, Intel, nouveau in-tree `=m`, NVIDIA open modules as the plan deb `linux-modules-nvidia`, built against the same kernel). `-gaming`, `-ia` and any `-nvidia` twin disappear. See `GPU-VARIANTS.md` | removes the GPU twin images and the vendor hardcode (`bootc-build.yml:460`, `Containerfile:262-264`) | ORDERED: to develop; the `-nvidia` variant is covered the same way, and the profile is a set of vendors (multi-GPU), see `GPU-VARIANTS.md` item 5 |
| L10 | Loading is an install-time variable of the bootc-installer (modules-load.d / blacklist, user-space stack activation), frozen after install; detection by `lspci`. Applies to every generic amd64 variant, NVIDIA included. Open points: image size cost (UNMEASURED), EULA scope of the NVIDIA user-space when it ships in the image (UNVERIFIED) | same | ORDERED: to develop |
| L11 | A command to change the GPU profile after install (working name `dc-gpu set gaming\|ia\|auto`, plus `dc-gpu status`): it only rewrites `/etc` configuration of the single all-vendor image (modules-load.d, blacklist, user-space stack activation), which bootc preserves across upgrades. Applied at next boot by default; applied live only where it is safe: loading a module, enabling a compute stack, a GPU no display session uses. Switching the driver of the GPU that drives the running display (for example nouveau to NVIDIA open) needs the display session stopped, so it is reboot-only. The existing LXC glue `dc-gpu-lxc` (VE) is separate | removes the "frozen after install" rule of the current wiki (`GPU-Support.md`) | ORDERED: to design and develop after L9/L10; behaviour of live unbind UNVERIFIED |
| L12 | ONE custom step set in the bootc-installer carrying ALL install-time configuration, so the first-boot setup service (`firstboot-user-setup`, `debian-bootc/workflows/bootc-debs-builder/firstbootpostinstall/usr/local/bin/firstboot-user-setup`) is removed (owner: the goal of bootc-installer is to drop the setup service). Axes: module-signature enforcement (L1), GPU profile (L9/L10), and every step of the service, transposed (table below); further axes to propose: bootloader/embedded profile (U-Boot vs UEFI), MOK enrolment (already patched in `bootc-installer-mok.patch`), auto-update (already `bootc-installer-autoupdate.patch`), SSH policy. Pattern to follow: one patch per step under `workflows/bootc-debs-builder/patches/` (template `.blp` + `defaults/<step>.py` + `builder.py` registration + `processor.py` recipe field) | removes a boot-time interactive service, the `getty@tty1` drop-in and the done-flag seeding in the boot test (`.ci/image-test/install-fs.sh:222-229`) | ORDERED: to design and develop |

One-kernel scope (answer given to the owner): signed vs `-nosign` can share one kernel (L1). amd64 vs arm64 cannot (instruction set); 4K vs 16K pages on arm64 cannot (compile-time `CONFIG_ARM64_4K_PAGES`/16K); a per-device trimmed config is a different kernel, so the device `linux-modules-*` packages stay. Minimum: one kernel per (arch, page size).

Target (ESTIMATED): about 290 to 150 min cumulated with L1+L2, wall clock from 64 to about
45 min; below 15 min only for pushes that rebuild nothing heavy (L4+L7).

## L1 constraint found in the bootc docs (MEASURED, source read)

`docs/src/building/bootc-kernel-arguments.7.md` of bootc: "It is currently undefined behavior to
remove kernel arguments locally that are included in the base image via /usr/lib/bootc/kargs.d".
So a user cannot cleanly drop `module.sig_enforce=1` from a signed image; the unsigned behaviour
must come from the image that does not ship the karg (the `-nosign` tag stays, built on the same
kernel package). The kernel config has `CONFIG_SECURITY_LOCKDOWN_LSM=y` only
(`kernel/config:266`), no `LOCK_DOWN_IN_EFI_SECURE_BOOT`, so Secure Boot alone does not enforce
module signatures: the karg is what makes `sig_enforce=Y`. The test
"signed kernel enforces module signatures" (`tests.yml:206-213`) and
"unsigned-kernel variant does not enforce" (`tests.yml:215-221`) stay unchanged.

## L2 local measurement (MEASURED)

Container `debian:trixie`, 16 cores, kernel 7.3.1, `kernel/config` of debian-bootc + `olddefconfig`,
`make vmlinux modules`, source re-extracted between passes, `PATH=/usr/lib/ccache:$PATH`
(no change to any make line), `CCACHE_BASEDIR=/tmp`, `CCACHE_COMPILERCHECK=content`:
cold 142 s, 2105 misses; warm 10 s, 2126 hits on 2126 lookups (100 % direct hits); cache 83 MB.
Not measured: hit rate across two kernel releases, and between the image config and the device
modules configs (the CI step `ccache statistics` prints it for each job).

## Constraints from the owner

- Never disable Secure Boot or weaken a test to reach a time target.
- A fix is pushed to the CI only after being proven locally; the CI result is then read.
- `-nosign` means no module signature enforcement only; shim, grub and vmlinuz stay signed.

## Results

- Heavy layer cache (finding 3): fixed, run 41151560841 (`b87ef23`) seeded the cache
  (`Pushing cache` on STEP 13, new hash formula); `Using cache` on STEP 13 is to be confirmed on the next run.
- ccache (L2): armed in `d3b61bb`. No CI evidence yet: the kernel content hashes are unchanged, so no
  kernel job runs until a kernel input changes. The first kernel rebuild seeds the cache (cold), the
  next one is the measurement (`ccache statistics` step prints the hit rate per job). Open risk:
  `actions/cache` inside the `container:` job needs `tar` in the build-env image (UNVERIFIED).
- Run 41155245822 (`cb39aff`): wall clock 15 min 46 s (01:25:02 to 01:40:48), every deb reused (kernel jobs about 1 min,
  `linux-image (amd64) unchanged; reused cached deb(s)`), the 6 image jobs still fail. This is the first measured
  figure for the 15 min target on a push that rebuilds no deb.

## L12: setup service steps to transpose (MEASURED, source read)

Source: `firstboot-user-setup` (282 lines). Installer steps today (`src/buildisopreinstall/etc/bootc-installer/recipe.json`): welcome, disk, encryption, autoupdate, user. Whether the upstream `user` step already covers some fields below is UNVERIFIED (upstream `user.py` is fetched at package build time, not read yet).

| Service step | Mechanism in the service | Installer transposition |
|---|---|---|
| System language | `dpkg-reconfigure locales` | locale step, written to the target `/etc/locale.gen` + `/etc/default/locale` |
| Keyboard layout | `dpkg-reconfigure keyboard-configuration`, `setupcon` | keyboard step, written to `/etc/default/keyboard` |
| Hostname (RFC 1123 check, max 63) | `hostnamectl set-hostname` | hostname step, same validation, written to `/etc/hostname` |
| Username (`^[a-z_][a-z0-9_-]{0,31}$`, must not exist) | `useradd --create-home --shell /bin/bash` | user step (same validation) |
| Full name (optional) | `useradd --comment` | user step field |
| User password (min 8, confirmed) | `chpasswd` | user step |
| Root password (required, min 8, `chage -d` today) | `chpasswd`, `chage` | root-password step |
| Sudo for the user | `usermod -aG sudo` | user step switch |
| Standard groups `adm systemd-journal cdrom audio video plugdev netdev` (those that exist) | `usermod -aG` | applied by the installer for the created user |
| SSH root login | `/etc/ssh/sshd_config.d/01-permitrootlogin.conf` | SSH policy step (default no) |
| Skip if a human account exists (uid 1000-65533, login shell, home under `/home`) | `_existing_user` | not needed: the installer creates the user once |
| Done flag `/var/lib/firstboot-user-setup.done` | touch | disappears with the service |

Once every row is covered and tested, `firstboot-user-setup` leaves `Containerfile:122`, `packages.yml:28-55` and its boot-test seeding. The `packages.yml` line shift must respect the `docs-check` citations.

## L4 + L8 design: plan job and per-package waiting (DESIGNED, not yet coded)

Source: architect-opus report, files read in `build-deb/build-deb.sh`, `build-one-deb.yml`,
`bootc-debs-builder.yml`, `bootc-debs-end/action.yml`. Status of each step is tracked here.

- **Plan job, not a `setup` step.** `upstream_id` resolves through apt of the build env for the native
  arch, and `setup` runs on `ubuntu-latest` amd64. A new `plan` job (`needs: setup`) runs on the arch
  runner inside `container: env_image`: one container start (about 25 s) instead of 22.
- **No duplicated logic.** Move into shared scripts used by both the plan and the build path:
  `build-deb/content-hash.sh` (hash functions, `write_meta`, `restore_cached`),
  `scripts/pkg-def.sh` (package definition read), `scripts/dep-debs.sh` (dependency closure and
  `DEPS_HASH`). The hash formula does not change, so the published cache stays valid.
- **Dependency propagation.** A package with any transitive dependency in the build set is built
  without hashing; otherwise `DEPS_HASH` is computed from the restored cached dependency debs.
- **Skipped packages** are restored into `/debs` with `.meta`, uploaded as ONE artifact
  `debs-_reused-<arch>` (underscore cannot occur in a Debian package name); publish is unchanged.
- **Per-package waiting (L8).** One dynamic `build` matrix (`{package, wait_for}` list from the plan)
  replaces `wave0..wave3`; `build-one-deb.yml` gets a `wait_for` input, a polling first step on
  `status-<dep>-<arch>` artifacts and an `always()` last step uploading its own status.
- **Gates.** `publish` requires `plan.result == success` and `plan_ok`, otherwise an APT repo without
  the reused packages would be deployed.
- **Open measurements.** Time of the single job moving all cached debs (kernels included): UNMEASURED,
  decides the 15 min target. Artifact API budget for polling: ASSUMPTION (1000 req/h), not verified.

Steps: 1 `content-hash.sh` (hash parity test old vs new against published `hashes.json`),
2 `pkg-def.sh` + `dep-debs.sh` (diff of `GITHUB_OUTPUT` old vs new), 3 `deb-plan/` action,
4 `wait_for` in `build-one-deb.yml`, 5 `bootc-debs-builder.yml`, 6 `full-pipeline.yml` gates, 7 docs.

## Run 41191163442 timing breakdown (MEASURED)
- Wall clock 24 min (10:03:17-10:27:27). The deb phase ended about 10:06: the kernel jobs took 47-85 s each (`unchanged; reused cached deb(s)`), so conditional rebuild at deb level worked on this run. The run is dominated by the image jobs (about 18 min, `latest-arm64` 10:09:45-10:27:26).
- `latest-arm64` job: Build 115 s; `Boot-test the image` 585 s; `Boot-test the unsigned-kernel twin` 280 s; scan+push+sign 37 s.
- Inside the 585 s boot test: install about 90 s, boot 110 s, one test (`kernel modules are all signed or none is`) 118 s, reboot 145 s.
- L13 (done): that test forked `modinfo` per module (1667 modules). Replaced by one `grep -L` over the plain `.ko` files (local: 0.13 s vs forks; same verdict on the real 16k kernel modules: 1667 signed, 0 unsigned; mixed set fails). Saves about 2 min per boot test, twice per arm64 image (twin).
- Next measured candidates: the unsigned twin repeats the whole boot test (L1/L6), install about 90 s, and calibrated timeouts do not shorten a passing run.

## L14: skip unchanged images (implemented, awaiting CI evidence)
- Before: `chash` computed `identical` but only used it to open a "Suspicious pipeline" issue; all 6 image jobs rebuilt, boot-tested (about 15 min on arm64), pushed and signed on every run. The 24 min wall clock of run 41191163442 is this: the deb phase was over at about 10:06.
- Now: `skip=true` when the content hash equals the published image's `org.daemoncores.content-hash` label (except schedule / force_no_cache). Every step from `Build base` to `Sign image` is guarded. The hash also includes the boot-test manifest. The manifest job reuses the already published per-arch tags.
- First run after the change rebuilds everything once (hash formula changed); the next no-change run is the measurement.
- Images that fail their boot test are never pushed, so they never become "identical" and run again.

## L17: where the deb phase time goes when nothing changed (MEASURED, run 41199518844)
- Conditional rebuild at deb level WORKS: `linux-image (arm64) unchanged; reused cached deb(s)`, `cache[linux-image-arm64]: hash=05759f76... prior=05759f76...` (same hash as the published one). The reuse step itself takes about 1 s.
- The deb phase still took 7 min (12:05:27-12:12:41) because each of the ~28 per-package jobs pays its own setup: container pull about 25 s, `Enable ccache` (apt install) about 33 s, ccache restore (871 MB) about 8 s, checkout, manifest-expand, pkg-def. Kernel job `linux-image` arm64: 115 s, of which about 1 s was the actual decision.
- Billing is the sum over jobs: about 36 job-minutes spent to reuse unchanged debs.
- Fix (designed, steps 6-8 of the single-job plan): one `build` job per arch running `build-deb/build-all.sh` over `build-deb/plan.py` order; hash decision first, ccache install/restore only when a package really builds; kernel tarball cached (`KERNEL_TARBALL_CACHE`, sha256 verified on every use; done, commit b3fb61d). Expected: a no-change run in about 2 min per arch (ESTIMATED).

## L18: Containerfile layer order for cache reuse (implemented in debian-bootc, awaiting CI evidence)
- Rule: put the layers that differ between tags and twins last. Kernel and device modules (they differ per `-nosign`, `-16k`, `-4k`) are now the last heavy layer; the GPU gaming layer, the grub cmdline / service masking, and `ostree-layout` all come before it, so every twin of an image shares them.
- `-ia` cannot precede the kernel: `linux-modules-nvidia` depends on the exact kernel package. Device modules also pin the kernel (`Depends: daemoncores-kernel-<kver> (= <ver>)`), so modules and kernel stay in one transaction unless `dpkg --force-depends` is used (not done).
- `install-signature-policy` is last: `IMAGE_REF` differs per tag, so it must not sit above the kernel layer.
- Removed `rm` lines that duplicated work done elsewhere: the apt lists are wiped by `99-apt-clean` after every apt run, and `ostree-layout` already empties `/tmp`, `/var/tmp`, `/run` and removes `policy-rc.d`.
- Not proven yet: kernel postinst (dracut) running after `ostree-layout`; the boot test and `bootc container lint` of the next run decide.

## Single-job-per-arch plan status
Done locally, not pushed: step 2 `sign-image/sign.sh` (04d9d08), step 6 kernel tarball cache (b3fb61d). Open: 1 image-test split, 3 `build-arch.sh`, 4 `Containerfile.iso` ARG, 5 workflow wiring, 7 `plan.py`/`build-all.sh`, 8 `bootc-debs-builder.yml`, 9 docs.
