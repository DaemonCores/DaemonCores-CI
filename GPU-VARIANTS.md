# GPU usage twins: every vendor in both

Owner order: `-gaming` and `-ia` carry the drivers of EVERY vendor; what differs between
them is the build configuration and optimisation, never the vendor list.

## Current state (read from the code)

| Where | Finding |
|---|---|
| `bootc-build.yml:461` | `GPU_GAMING_PKGS` default already spans AMD, NVIDIA and Intel firmware plus mesa-vulkan |
| `bootc-build.yml:460` | `GPU_IA_PKGS` default (`vars.NVIDIA_IA_PKGS`) is NVIDIA only: vendor hardcoded |
| `bootc-build.yml:455` | matrix field `nvidia` / env `NVIDIA` names the vendor in the variant plumbing |
| `Containerfile:262-264` (debian-bootc) | `-ia` installs `linux-modules-nvidia` only |
| `packages.yml:557` (debian-bootc) | the only out-of-tree GPU module deb is `linux-modules-nvidia` |

## Debian trixie package facts (MEASURED: `apt-cache policy` in `debian:trixie`, main contrib non-free non-free-firmware)

| Vendor | Compute / display packages present |
|---|---|
| AMD | `rocm-opencl-icd` 5.7.1, `libamdhip64-5`, `rocminfo`, `rocm-smi`, `firmware-amd-graphics` |
| NVIDIA | `nvidia-driver` 550.163.01, `nvidia-opencl-icd`, `nvidia-smi`, `nvidia-cuda-toolkit` 12.4, `firmware-nvidia-graphics` |
| Intel | `libze1` (Level Zero loader), `intel-media-va-driver-non-free`, `firmware-intel-graphics`, `mesa-opencl-icd` (Mesa rusticl); **`intel-opencl-icd` and `libze-intel-gpu1` (compute-runtime) are NOT in trixie** |
| Common | `mesa-vulkan-drivers`, `mesa-va-drivers`, `libvulkan1`, `vulkan-tools`, `clinfo` |

Gap to close for Intel compute: the compute-runtime has to come from a plan deb built in CI
(like `linux-modules-nvidia`) or from an upstream repository; not decided.

## Direction (owner clarification)

1. ONE image per generic amd64 variant carries every vendor; no `-gaming`/`-ia`/`-nvidia` twins.
   In-tree drivers (AMD, Intel, nouveau) are `=m`; NVIDIA open modules are the plan deb `linux-modules-nvidia`
   built against the same kernel.
2. What gets loaded is an install-time variable of the bootc-installer (modules-load.d, blacklist, user-space
   activation), frozen after install. Brand detection stays at runtime (`lspci`).
3. Open: image size cost (UNMEASURED); EULA scope of the NVIDIA user-space when it ships inside the image
   (UNVERIFIED, owner decision).
4. Per-variant build configuration and optimisation: list to be provided by the owner.
5. The NVIDIA variant follows the same operation as `-gaming` and `-ia`: one all-vendor image, profile chosen at install
   (owner: "la variante nvidia avec le même fonctionnement"). NVIDIA does not exclude the other drivers: multi-GPU
   machines (AMD/Intel iGPU plus NVIDIA dGPU, owner's own machine) load several drivers at once. Consequences for the
   design: the profile is a SET of vendors plus a usage (`gaming`, `ia`, `nvidia`), never a single exclusive choice;
   no blacklist of a vendor's driver unless that vendor's hardware is absent or the user removed it; user-space stacks
   (Vulkan/OpenCL ICDs, CUDA, ROCm, level-zero) are installed side by side and selected per GPU or per application
   (ICD loader, `DRI_PRIME`, `CUDA_VISIBLE_DEVICES`); `dc-gpu status` lists each detected GPU with its driver. Behaviour of
   the NVIDIA proprietary and nouveau modules on the same host is UNVERIFIED: nouveau must not bind the NVIDIA GPU when
   the NVIDIA module is chosen for it, while the other GPUs keep their in-tree driver (per-device binding, not a global
   `blacklist`), to be tested on real multi-GPU hardware.


## Implementation: `gpu-userspace` builds (debian-bootc)

Two Mesa builds, `gaming` and `ia`, compiled in CI by the `gpu-userspace` class of
`workflows/bootc-debs-builder/packages.yml`, both inside the same image. The vendor list never appears in
the logic: Mesa is configured with `-Dgallium-drivers=auto -Dvulkan-drivers=auto`, so every driver the
build host can compile (AMD, Intel, nouveau/NVK, software) is in both profiles. They differ only in build
configuration:

| Profile | Optimisation | Notes |
|---|---|---|
| `gaming` | `-O3`, LTO, all video codecs, VA-API and VDPAU, Vulkan overlay/screenshot layers | no rusticl |
| `ia` | `-O2`, shared LLVM, rusticl OpenCL | no VA-API/VDPAU |

The `-O3`/LTO versus `-O2` split is a design choice of this lane, not ported from VE (VE only defines the
microarchitecture levels).

CPU levels reuse the mapping of `scripts/kernel_helpers.sh` (`kernel_flavour_kcflags`: x64v2/x64v3 give
`-march=x86-64-v2|v3`, x64v1 and arm64 give no flag), driven by the `variant-env` mechanism of the VE
`kernel-pve` entry (one matrix entry per profile and level). The `x64v2`/`x64v3` debs contain only the
libraries, placed in `glibc-hwcaps/x86-64-v2|v3` under the profile prefix, so `ld.so` selects the best
level the CPU supports. This is userspace only; there is no boot-time selection and no kernel choice (one
kernel per image).

Layout: `/usr/lib/dc-gpu/<profile>` and `/usr/share/dc-gpu/<profile>`. `dc-gpu set gaming|ia|auto`
(and `dc-gpu-apply.service` at boot) writes `/etc/ld.so.conf.d/dc-gpu.conf` and
`/etc/environment.d/50-dc-gpu.conf`, merging the profile's ICD, EGL, VA-API and OpenCL entries with the
host's, so vendor stacks installed from Debian remain visible. ICD files carry bare sonames so that
`ld.so` (and hwcaps) resolves them.

Limits:

- NVIDIA proprietary user-space is binary only and cannot be recompiled: it stays the Debian packages.
- ROCm, Level Zero and Intel media remain Debian packages; no source build exists yet.
- UNVERIFIED until the first CI run: the Mesa meson options on the pinned Mesa version, `apt-get build-dep mesa`
  in the build container, `dpkg-shlibdeps` results, and whether libraries opened by absolute path (the
  megadriver under `lib/dri`) benefit from the hwcaps levels.
- Image size cost and any speed gain are UNMEASURED (gains ESTIMATED only).
