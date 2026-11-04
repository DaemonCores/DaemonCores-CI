"""Contract tests of the shared kernel build data (build-deb/kernel-shared).

Owner arbitration 2026-11-03 12:30: the canonical patches manifest and the
shared kconfig fragments live HERE, in the build engine; build-deb.sh
exports the directory as CI_KERNEL_SHARED (like the helper scripts) and both
kernel lanes (debian-bootc linux-image, DaemonCores-VE kernel-pve) consume
the set from that checkout — no copy in a consumer repo and no fetch between
production repositories. The shared term is hashed by content-hash.sh for
the builds that consume it.
"""

import subprocess
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SHARED_DATA = REPO / "build-deb" / "kernel-shared"
BUILD_DEB_SH = REPO / "build-deb" / "build-deb.sh"
CONTENT_HASH_SH = REPO / "build-deb" / "content-hash.sh"


def _write_temp_runner():
    """A runner sources build-deb.sh with a real BASH_SOURCE (sourcing it from
    /dev/stdin would resolve its own dirname to /dev), then calls run_build
    like the engine does and prints both exported shared directories. Returns
    the CompletedProcess; the runner must live next to build-deb.sh because
    build-deb.sh resolves its own helpers with `dirname ${BASH_SOURCE[0]}`."""
    runner = BUILD_DEB_SH.read_text(encoding="utf-8").replace(
        'main "$@"',
        'run_build >/dev/null 2>&1 || true\n'
        'printf "%s|%s" "${CI_KERNEL_SHARED}" "${CI_SCRIPTS}"',
    )
    tmp = Path(tempfile.NamedTemporaryFile(
        "w", suffix=".sh", delete=False,
        dir=str(BUILD_DEB_SH.parent)).name)
    tmp.write_text(runner, encoding="utf-8")
    try:
        r = subprocess.run(["bash", str(tmp)], capture_output=True,
                           text=True, cwd=str(REPO))
    finally:
        tmp.unlink()
    return r


def test_shared_data_files_present() -> None:
    """The canonical set is committed in the engine: one manifest + 7 kconfig
    fragments (x86 base, three flavours, three GPU sets)."""
    assert (SHARED_DATA / "patches-shared.yml").is_file()
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
        assert (SHARED_DATA / "kconfig-shared" / name).is_file()
    for name in ("amd.cfg", "intel.cfg", "nvidia.cfg"):
        assert (SHARED_DATA / "kconfig-shared" / "gpu" / name).is_file()


def test_arch_overlays_folded_into_the_base_config() -> None:
    """The bare arch overlays are gone; their content lives in kernel/config.

    The fold (owner 2026-11-03): modules/arm and modules/x86_64 do not exist
    anymore, the base config carries the '# arm64 built-in' and
    '# amd64 built-in' sections, and the former =m drivers are =y (a
    modules-only build can never add a built-in to an existing kernel).

    KERNEL FINAL v2 (owner 2026-11-03): the per-device fragments AND the
    arch-overlay dir are gone from the engine — the device module lists are
    debian-bootc lane data (workflows/bootc-debs-builder/kernel/modules),
    built uniquely on that lane; the engine keeps only the common set
    (patches-shared.yml, config, pages, kconfig-shared).
    """
    for gone in ('arm', 'x86_64'):
        assert not (SHARED_DATA / 'modules' / gone).exists(), gone
    assert not (SHARED_DATA / 'modules').exists()
    cfg = (SHARED_DATA / 'config').read_text(encoding='utf-8')
    for section in ('# arm64 built-in', '# amd64 built-in'):
        assert section in cfg, section
    for promoted in (
        'CONFIG_E1000=y',
        'CONFIG_R8169=y',
        'CONFIG_DRM_VC4=y',
        'CONFIG_DRM_ROCKCHIP=y',
        'CONFIG_NR_CPUS=512',
        '# CONFIG_HWPOISON_INJECT is not set',
    ):
        assert promoted in cfg, promoted


    # The device fragments are NOT engine data anymore: they moved back to the
    # debian-bootc lane (KERNEL FINAL v2). The page-size overlays stay here.
    assert not (SHARED_DATA / 'modules').exists()
    for pages in ('4k', '16k'):
        assert (SHARED_DATA / 'pages' / pages).is_file(), pages
    bootc_modules = (
        REPO.parent / 'debian-bootc' / 'workflows' / 'bootc-debs-builder' /
        'kernel' / 'modules'
    )
    if bootc_modules.is_dir():
        for device in (
            'rpi3', 'rpi4', 'rpi5', 'rk3588', 'rk3566',
            'rk3399', 'h616', 'imx8m', 'mt8183',
        ):
            assert (bootc_modules / device).is_file(), device


def test_gpu_fold_undone_drivers_are_modules() -> None:
    """GPU FINAL (owner 2026-11-03): the folded =y GPU drivers are back to
    =m in the base config, and CONFIG_DRM_AMD_DC left the base — the
    display-core build is a PROFILE choice of the four GPU module sets
    (gpu-ia, gpu-gaming and their NVIDIA variants), not a base value.

    The device/arch folds are untouched: the arm64 SoC built-ins, the
    network/USB built-ins and the disables section keep their values.
    """
    cfg = (SHARED_DATA / 'config').read_text(encoding='utf-8')
    for modular in (
        'CONFIG_DRM_I915=m',
        'CONFIG_DRM_XE=m',
        'CONFIG_DRM_AMDGPU=m',
        'CONFIG_DRM_AMDGPU_SI=m',
        'CONFIG_DRM_AMDGPU_CIK=m',
        'CONFIG_DRM_RADEON=m',
        'CONFIG_DRM_NOUVEAU=m',
        'CONFIG_DRM_GMA500=m',
        'CONFIG_DRM_MGAG200=m',
        'CONFIG_DRM_AST=m',
        'CONFIG_DRM_UDL=m',
        'CONFIG_DRM_VBOXVIDEO=m',
    ):
        assert modular in cfg, modular
        assert modular.replace('=m', '=y') not in cfg, modular
    for folded in (
        'CONFIG_DRM_I915=y',
        'CONFIG_DRM_AMDGPU=y',
        'CONFIG_DRM_AMD_DC=y',
    ):
        assert folded not in cfg, folded
    # The arm64 device fold of the same file keeps its built-ins.
    for kept in ('CONFIG_DRM_VC4=y', 'CONFIG_DRM_ROCKCHIP=y'):
        assert kept in cfg, kept


def test_no_local_copy_in_consumer_repos() -> None:
    """The sibling consumer repos carry NO copy of the shared data: their
    former locations are gone (the engine checkout is the single source)."""
    for repo, stale in (
        (
            REPO.parent / "debian-bootc",
            "workflows/bootc-debs-builder/kernel/patches-shared.yml",
        ),
        (
            REPO.parent / "debian-bootc",
            "workflows/bootc-debs-builder/kernel/kconfig-shared/x86-common.cfg",
        ),
    ):
        if repo.is_dir():
            assert not (repo / stale).exists()


def test_engine_exports_ci_kernel_shared() -> None:
    """build-deb.sh exports CI_KERNEL_SHARED next to CI_SCRIPTS in
    run_build, so every build shell sees the shared directory."""
    text = BUILD_DEB_SH.read_text(encoding="utf-8")
    assert 'export CI_KERNEL_SHARED' in text
    assert "kernel-shared" in text


def test_bore_excluded_on_all_current_series() -> None:
    """The bore sibling is series-gated: CachyOS carries no 7.0 dir, and the
    2026-11-04 upstream bore (renamed 0001-bore-cachy.patch) no longer applies
    to any vanilla tree (fair.c LOG->NONE drift; dry-run FAIL on 7.2.15 and
    7.3) — the entry is DORMANT, excluded on every active series; the helper
    filter honours it. Re-arm by removing the excludes when upstream matches
    vanilla again."""
    import yaml  # noqa: PLC0415 — local import keeps the module import light

    doc = yaml.safe_load((SHARED_DATA / "patches-shared.yml").read_text())
    bore = next(p for p in doc["patches"] if p["name"] == "bore")
    assert bore["exclude-series"] == ["7.0", "7.1", "7.2", "7.3"]
    helper = (REPO / "scripts" / "kernel_helpers.sh").read_text()
    assert "exclude-series" in helper


def test_engine_exports_resolve() -> None:
    """The export resolves to the committed directory in a plain bash.

    The wrapper file lives in a temp dir so that `bash <wrapper>` gives
    build-deb.sh a real ${BASH_SOURCE[0]} (sourcing it from /dev/stdin
    would resolve its own `dirname` to /dev and break . content-hash.sh).
    """
    r = _write_temp_runner()
    shared, scripts = r.stdout.strip().split("|")
    assert r.returncode == 0, r.stderr
    assert Path(shared).resolve() == SHARED_DATA.resolve()
    assert Path(scripts).resolve() == (REPO / "scripts").resolve()


def test_shared_term_feeds_the_hash() -> None:
    """content_hash appends the shared term only for builds that consume
    CI_KERNEL_SHARED; a non-consumer build keeps the published formula."""
    script = (
        f'. "{CONTENT_HASH_SH}"\n'
        'PACKAGE=x ARCH=amd64 SOURCES= UPSTREAM= EXTRA_ENV=\n'
        'BUILD="echo \\${CI_KERNEL_SHARED}"\n'
        'shared=$(shared_data_hash)\n'
        '[[ "$shared" != none ]] || fail=1\n'
        'BUILD="echo plain-build"\n'
        'plain=$(shared_data_hash)\n'
        'printf "%s|%s" "${shared:-}" "$plain"\n'
    )
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    shared, plain = r.stdout.split("|")
    assert len(shared) == 64
    assert plain == "none"