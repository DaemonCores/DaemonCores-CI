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
   second build pass logged 25 `Using cache`. Cause of the registry-cache miss: UNKNOWN
   (`ARG DEBS_HASH` is placed right before that `RUN`).
4. **`--cache-to` pushes every layer**: each trivial step (`LABEL`, `ARG`) takes 2-3 s.
   About 80 s of the 285 s `Build` step (ESTIMATED).
5. **Boot test dominates per-image time** on arm64 (367-388 s, TCG), amd64 146 s.
6. The kernel layer is already after the heavy package layer (`Containerfile:192`); moving it
   last saves about 30 s only. The `-ia` variants must come after it (`linux-modules-nvidia`
   is built against that kernel).

## Levers

| # | Lever | Estimated gain (cumulated) | Status |
|---|---|---|---|
| L1 | One kernel build for signed and `-nosign`: `MODULE_SIG_FORCE=n` in the kernel, the signed image ships `/usr/lib/bootc/kargs.d/*.toml` with `module.sig_enforce=1`, the `-nosign` image ships none (the twin then differs by one tiny layer, not by a kernel) | kernel builds only: about 45 min of 290 (amd64 about 950 s + arm64-16k about 1770 s); the 4 twin image jobs stay unless the twin is dropped | planned, see constraint below |
| L2 | Shared `ccache` for all kernel/module builds | modules 985 s to about 250 s each: about 48 min, and about 12 min off the wall clock | to measure locally |
| L3 | Chain the variants of one Containerfile+arch in one job (local layer cache), push once | about 200 s per extra variant: about 27 min with the full plan | planned |
| L4 | Skip unchanged debs in the setup job instead of starting a container per package | 22 jobs x 30-50 s, plus shorter waves | planned |
| L5 | Build without per-layer `--cache-to` inside a chain (push the final layers once) | about 80 s per image job | to measure |
| L6 | Smoke test only for package-only variants (gaming/ia); full boot test per (arch, kernel) | up to 6 min per variant on arm64 | to decide |
| L7 | Conditional rebuild of kernels on upstream version + our sources only (already hashed); stop invalidating the kernels for edits that do not change them | wall clock below 15 min on a no-change push | verify |

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

## Constraints from the owner

- Never disable Secure Boot or weaken a test to reach a time target.
- A fix is pushed to the CI only after being proven locally; the CI result is then read.
- `-nosign` means no module signature enforcement only; shim, grub and vmlinuz stay signed.

## Results

(Filled as each lever is measured/applied.)
