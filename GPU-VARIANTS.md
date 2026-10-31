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

## Direction

1. One YAML manifest of per-vendor package sets per usage; `-gaming` and `-ia` derive their lists
   from it for all vendors; the vendor-named variables and matrix fields become neutral.
2. EULA consent applies to the packages that carry an EULA (NVIDIA user-space), not to the variant.
3. Brand selection stays at runtime (detection), never in the image.
4. Per-variant build configuration and optimisation: list to be provided by the owner.
