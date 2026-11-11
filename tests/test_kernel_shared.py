"""Contract tests of the product-canonical kernel build data.

Owner arbitrations 2026-11-11 (R-869b..R-871b): the kernel build data lives
in the PRODUCT repository (debian-bootc, workflows/bootc-debs-builder/
kernel-src) — the build engine keeps NO kernel package data at all (the
former build-deb/kernel-shared directory is deleted, the CI_KERNEL_SHARED
export is gone from build-deb.sh and content-hash.sh answers "none"). The
debian-bootc lane reads everything from its own checkout
(packages.yml BASE_CONFIG / patches / kconfig paths); the VE and vanilla
lanes carry their own data. Both kernel lanes still consume the helper
primitives through scripts/kernel_helpers.sh, unchanged.
"""

import subprocess
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
PRODUCT = REPO.parent / "debian-bootc"
KERNEL_SRC = (PRODUCT / "workflows" / "bootc-debs-builder" / "kernel-src")
BUILD_DEB_SH = REPO / "build-deb" / "build-deb.sh"
CONTENT_HASH_SH = REPO / "build-deb" / "content-hash.sh"


def test_kernel_src_canonical_files_present() -> None:
    """The canonical set is committed in the product repo: the base config,
    the flavour/cpu fragments, the pages overlays, the kernel and PVE patch
    manifests and the per-profile GPU patch sets."""
    assert (KERNEL_SRC / "config.cfg").is_file()
    for name in (
        "x86-common.cfg",
        "x64v1.cfg",
        "x64v2.cfg",
        "x64v3.cfg",
        "x64v4.cfg",
        "armv8.0.cfg",
        "armv8.2-lse.cfg",
        "armv9-sve2.cfg",
    ):
        assert (KERNEL_SRC / "kconfig" / "cpus" / name).is_file(), name
    for name in ("4k.cfg", "16k.cfg"):
        assert (KERNEL_SRC / "kconfig" / "pages" / name).is_file(), name
    for name in ("kernel.yml", "pve.yml"):
        assert (KERNEL_SRC / "patches" / name).is_file(), name
    for profile in ("ia", "gaming"):
        for name in ("amd.yml", "intel.yml", "nvidia.yml"):
            path = KERNEL_SRC / "patches" / "gpu" / profile / name
            assert path.is_file(), f"{profile}/{name}"


def test_engine_carries_no_kernel_package_data() -> None:
    """The engine's former shared kernel data is GONE: no build-deb/
    kernel-shared directory, no CI_KERNEL_SHARED export in build-deb.sh,
    and shared_data_hash answers none (no engine-side term in any cache
    key any more — the product checkout's checkout hash covers the data)."""
    assert not (REPO / "build-deb" / "kernel-shared").exists()
    text = BUILD_DEB_SH.read_text(encoding="utf-8")
    assert "CI_KERNEL_SHARED" not in text
    r = subprocess.run(
        [
            "bash", "-c",
            f'. "{CONTENT_HASH_SH}"\nBUILD="echo x"\nshared_data_hash'
        ],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "none"


def test_base_config_values() -> None:
    """The base config keeps the surviving arbitration values: the network/
    display built-ins fold, the GPU drivers stay =m, SQUASHFS=m (the ISO
    livefs needs it — R-855b) and NR_CPUS=512 (MAXSMP dropped, R-872b)."""
    cfg = (KERNEL_SRC / "config.cfg").read_text(encoding="utf-8")
    for present in (
        "CONFIG_E1000=y",
        "CONFIG_R8169=y",
        "CONFIG_DRM_VC4=y",
        "CONFIG_DRM_ROCKCHIP=y",
        "CONFIG_NR_CPUS=512",
        "CONFIG_SQUASHFS=m",
        "CONFIG_DRM_I915=m",
        "CONFIG_DRM_AMDGPU=m",
        "CONFIG_FUSE_FS=m",
    ):
        assert present in cfg, present
    for gone in ("CONFIG_MAXSMP=y", ):
        assert gone not in cfg, gone
    # CONFIG_DRM_AMD_DC=y is BACK in the base (the FULL union R-869b kept
    # the distro value; the profile still flips the DC bits it owns).


def test_patch_manifest_schema_and_gates() -> None:
    """The canonical manifests carry the declarative schema and the known
    gates: the bore sibling stays series-excluded (re-armed 2026-11-09, the
    CachyOS dry-run still fails fuzz-0 on vanilla 7.2.15) and the ARM OPTI
    completions stay sha-pinned stage-common entries."""
    doc = yaml.safe_load(
        (KERNEL_SRC / "patches" / "kernel.yml").read_text(encoding="utf-8")
    )
    names = {p["name"]: p for p in doc["patches"]}
    bore = names["bore"]
    assert bore["exclude-series"] == ["7.0", "7.1", "7.2", "7.3"]
    helper = (REPO / "scripts" / "kernel_helpers.sh").read_text()
    assert "exclude-series" in helper
    completion = names["0166-sched-fair-remove-upper-limit"]
    assert completion["stage"] == "common"
    assert completion["license"] == "GPL-2.0-only"
    assert completion["sha256"] == (
        "e6a7bfa9844f5f6bde6ef0a149dd58a7ad7dcfceae45cb08a9a4fd638d6ff0e9"
    )
    # K-PORTed by run 42713342754 (mainline MEMCG_CHARGE_BATCH BUILD_BUG_ON):
    assert "0174-memcg-charge-batch-128" not in names
    # the msm devfreq siblings are K-PORT (run 42715501467):
    assert "msm-adreno-devfreq-ifpc-relax" not in names
    pve = yaml.safe_load(
        (KERNEL_SRC / "patches" / "pve.yml").read_text(encoding="utf-8")
    )
    assert isinstance(pve.get("patches"), list) and pve["patches"]


def test_device_module_fragments_live_in_the_product_repo() -> None:
    """The per-device module fragments are lane data of the debian-bootc
    checkout (the engine keeps nothing); every declared device carries its
    fragment file."""
    embedded = KERNEL_SRC / "modules" / "embeded"
    for device in (
        "rpi3",
        "rpi4",
        "rpi5",
        "rk3588",
        "rk3566",
        "rk3399",
        "h616",
        "imx8m",
        "mt8183",
    ):
        assert (embedded / f"{device}.cfg").is_file(), device
    for profile in ("ia", "gaming"):
        assert (KERNEL_SRC / "modules" / "gpu" / profile).is_dir(), profile


def test_packages_yml_reads_the_product_checkout() -> None:
    """The debian-bootc packages.yml resolves its kernel data from its own
    checkout: the BASE_CONFIG and the patch/kconfig paths point inside
    kernel-src, and the track entry still requires the engine election
    (require-patches: engine — resolved by track.sh from THIS repo)."""
    packages = (PRODUCT / "workflows" / "bootc-debs-builder" / "packages.yml")
    text = packages.read_text(encoding="utf-8")
    assert "kernel-src/config.cfg" in text
    assert "kernel-src/patches/kernel.yml" in text
    assert "CI_KERNEL_SHARED" not in text
    assert "require-patches: engine" in text
    import yaml  # noqa: PLC0415 — local import keeps the module import light

    doc = yaml.safe_load(text)
    offenders = [
        p["name"] for p in doc["packages"]
        if isinstance(p, dict) and "patches" in p
    ]
    assert not offenders, (
        "production entries carry no native patch data (the canonical set "
        f"is kernel-src/patches): {offenders}"
    )
