"""Kernel helper (scripts/kernel_helpers.sh) contract tests.

The kernel build primitives are shared by two kernel modes — DaemonCores-VE
kernel-pve (PVE packaging + XanMod) and debian-bootc linux-image (kernel.org
rolling), both consumed by the bootc-debs builders. The manifest schema is
CANONICAL: declarative patches data (a patches.yml file or the native
PATCHES_JSON entry export), consumed through yq, never a bashified list.
"""

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HELPER = REPO / "scripts" / "kernel_helpers.sh"
# Reference mode (patch parity, owner arbitration 2026-11-03 12:30): the
# canonical shared manifest lives in THIS repository, inside the build-deb
# action; the engine exports it as CI_KERNEL_SHARED and both lanes consume
# it from that checkout (no copy in any consumer repo).
SHARED_DATA = Path(__file__).resolve().parents[
    1
] / "build-deb" / "kernel-shared"
VE_MANIFEST = SHARED_DATA / "patches-shared.yml"

ENV = {"KERNEL_CDN": "", "PATH": os.environ["PATH"], "HOME": os.environ["HOME"]}


def run_helper(script: str, **env_over) -> subprocess.CompletedProcess:
    env = dict(ENV, **env_over)
    return subprocess.run(
        ["bash", "-c", f'source "{HELPER}"\n{script}'],
        capture_output=True,
        text=True,
        env=env,
    )


def test_helper_shell_syntax():
    assert subprocess.run(["bash", "-n", str(HELPER)]).returncode == 0


def test_patch_list_from_native_manifest():
    """The engine manifest drives the class filtering; the bootc packages.yml
    carries NO native patch data (owner arbitration 2026-11-03: production
    packages.yaml list no `patches:` — the canonical set lives in
    build-deb/kernel-shared/patches-shared.yml, exported as CI_KERNEL_SHARED)."""
    import json

    import yaml

    manifest = (
        Path(__file__).resolve().parents[2] / "debian-bootc" / "workflows" /
        "bootc-debs-builder" / "packages.yml"
    )
    if not manifest.is_file():
        return
    assert not [
        p for p in yaml.safe_load(manifest.read_text())["packages"]
        if p["name"] == "linux-image" and "patches" in p
    ]
    patches = json.dumps(
        yaml.safe_load(VE_MANIFEST.read_text())["patches"])
    r = run_helper(
        'kernel_patch_list - amd64 6.12.107 kernel', PATCHES_JSON=patches
    )
    assert r.returncode == 0, r.stderr
    # The amd64 kernel class of the shared set is EMPTY (the module carrier
    # rides on the arm64 tree only).
    assert r.stdout.strip() == ""
    r = run_helper(
        'kernel_patch_list - arm64 6.12.107 module', PATCHES_JSON=patches
    )
    assert r.returncode == 0, r.stderr
    # arm64 module class: the RTL8733BU carrier when the manifest carries it
    # (owner arbitration 2026-11-03 13:05 moved it lane-side).
    carrier = any(p.get("name") == "rocknix-btrtl-rtl8733bu"
                  for p in yaml.safe_load(VE_MANIFEST.read_text())["patches"])
    if carrier:
        assert r.stdout.strip().startswith("rocknix-btrtl-rtl8733bu ")
    else:
        assert r.stdout.strip() == ""


def test_patch_list_from_file_ve():
    """The VE kernel manifest resolves {seriesdir} (7.0.x → eol/)."""
    if not VE_MANIFEST.exists():
        return
    r = run_helper(f'kernel_patch_list "{VE_MANIFEST}" amd64 7.0.14')
    assert r.returncode == 0, r.stderr
    assert "eol/linux-7.0.y-xanmod" in r.stdout
    line = r.stdout.splitlines()[0]
    assert line.startswith("0001-x86-cflags https://gitlab.com")
    assert "{seriesdir}" not in line and "{topic}" not in line


def test_patch_list_from_native_patches_json():
    """The native entry key: compact JSON export of the same patches set."""
    import json

    import yaml

    if not VE_MANIFEST.exists():
        return
    patches = json.dumps(yaml.safe_load(VE_MANIFEST.read_text())["patches"])
    r = run_helper(
        "kernel_patch_list - amd64 7.0.14 | wc -l", PATCHES_JSON=patches
    )
    assert r.returncode == 0, r.stderr
    # One line per patch entry the amd64 series actually carries: an entry
    # whose exclude-series lists the resolved series (7.0 here) is dropped by
    # the helper (e.g. bore, which CachyOS does not publish for 7.0), and an
    # entry staged arm64 does not ride a common/amd64 filter (e.g. the
    # RTL8733BU module carrier).
    import json as _json

    entries = _json.loads(patches)
    expected = sum(
        1 for p in entries
        if "7.0" not in (p.get("exclude-series") or [])
        and p.get("stage", "common") in ("common", "amd64")
    )
    assert int(r.stdout.strip()) == expected


def test_patch_list_json_matches_file():
    """PATCHES_JSON (the native export) yields the same lines as the file."""
    import json

    import yaml

    if not VE_MANIFEST.exists():
        return
    patches = json.dumps(yaml.safe_load(VE_MANIFEST.read_text())["patches"])
    from_file = run_helper(f'kernel_patch_list "{VE_MANIFEST}" amd64 7.0.14')
    from_json = run_helper(
        "kernel_patch_list - amd64 7.0.14", PATCHES_JSON=patches
    )
    assert from_file.returncode == 0 and from_json.returncode == 0
    assert from_file.stdout == from_json.stdout
    assert from_json.stdout.strip()


def test_flavour_kcflags():
    r = run_helper('kernel_flavour_kcflags x64v3')
    assert r.stdout.strip() == "-march=x86-64-v3"
    r = run_helper('kernel_flavour_kcflags x64v1')
    assert r.stdout.strip() == ""


def _make_cdn(tmp_path, version):
    """A file:// kernel.org mirror holding a tiny linux-<version> tarball."""
    import hashlib
    import shutil
    import tarfile

    cdn = tmp_path / "cdn" / f"v{version.split('.')[0]}.x"
    cdn.mkdir(parents=True)
    src = tmp_path / f"linux-{version}"
    src.mkdir()
    (src / "Makefile").write_text("x\n")
    tarball = cdn / f"linux-{version}.tar.xz"
    with tarfile.open(tarball, "w:xz") as t:
        t.add(src, arcname=f"linux-{version}")
    shutil.rmtree(src)
    return tmp_path / "cdn", tarball, hashlib.sha256(tarball.read_bytes()
                                                     ).hexdigest()


def _fetch(cdn, version, want, cache):
    return run_helper(
        f'kernel_fetch_source {version} {want} && echo "SRC=$SRCDIR"',
        KERNEL_CDN=f"file://{cdn}",
        KERNEL_TARBALL_CACHE=str(cache),
    )


def test_tarball_cache_miss_then_hit(tmp_path):
    version = "9.97.1"
    cdn, tarball, want = _make_cdn(tmp_path, version)
    cache = tmp_path / "cache"
    r = _fetch(cdn, version, want, cache)
    assert r.returncode == 0, r.stderr
    assert (cache / tarball.name).is_file()
    tarball.unlink()  # a second use must not need the network
    r = _fetch(cdn, version, want, cache)
    assert r.returncode == 0, r.stderr
    assert f"SRC=/tmp/linux-{version}" in r.stdout


def test_tarball_cache_corrupted_is_redownloaded(tmp_path):
    version = "9.97.2"
    cdn, tarball, want = _make_cdn(tmp_path, version)
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / tarball.name).write_text("corrupted")
    r = _fetch(cdn, version, want, cache)
    assert r.returncode == 0, r.stderr
    assert "downloading again" in r.stderr


def test_tarball_checksum_mismatch_fails(tmp_path):
    version = "9.97.3"
    cdn, tarball, _ = _make_cdn(tmp_path, version)
    cache = tmp_path / "cache"
    r = _fetch(cdn, version, "0" * 64, cache)
    assert r.returncode == 1
    assert "expected" in r.stderr
    assert not (cache / tarball.name).exists()
