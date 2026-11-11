"""Kernel helper (scripts/kernel_helpers.sh) contract tests.

The kernel build primitives are shared by two kernel modes — DaemonCores-VE
kernel-pve (PVE packaging + XanMod) and debian-bootc linux-image (kernel.org
rolling), both consumed by the bootc-debs builders. The manifest schema is
CANONICAL: declarative patches data (a patches.yml file or the native
PATCHES_JSON entry export), consumed through yq, never a bashified list.

Reference data (owner 11/11 R-870b): the product-canonical manifests live in
the debian-bootc checkout (kernel-src/patches); this test suite reads the
pve.yml and kernel.yml from that checkout as the reference data the helper
must resolve.
"""

import os
import subprocess
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HELPER = REPO / "scripts" / "kernel_helpers.sh"
PRODUCT = Path(__file__).resolve().parents[2] / "debian-bootc"
KERNEL_SRC = PRODUCT / "workflows" / "bootc-debs-builder" / "kernel-src"
KERNEL_MANIFEST = KERNEL_SRC / "patches" / "kernel.yml"
VE_MANIFEST = KERNEL_SRC / "patches" / "pve.yml"

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


def _expected_class_count(doc, series: str, classes) -> int:
    """How many entries the helper lists for the given series and stage
    classes: an entry excluded on the series (exclude-series) or staged
    outside the classes drops."""
    return sum(
        1 for p in doc["patches"] if series not in
        (p.get("exclude-series") or []) and p.get("stage", "common") in classes
    )


def test_patch_list_from_native_manifest():
    """The product packages.yml carries NO native patch data (owner R-870b:
    the canonical set lives in kernel-src/patches); the helper resolves the
    class filter over the reference pve.yml via PATCHES_JSON."""
    import json

    import yaml

    manifest = PRODUCT / "workflows" / "bootc-debs-builder" / "packages.yml"
    assert manifest.is_file()
    assert not [
        p for p in yaml.safe_load(manifest.read_text())["packages"]
        if p["name"] == "linux-image" and "patches" in p
    ]
    doc = yaml.safe_load(VE_MANIFEST.read_text())
    patches = json.dumps(doc["patches"])
    for series in ("7.0.14", "6.12.107", "6.14.11"):
        r = run_helper(
            f'kernel_patch_list - amd64 {series}',
            PATCHES_JSON=patches,
        )
        assert r.returncode == 0, r.stderr
        expected = _expected_class_count(doc, series, ("common", "amd64"))
        assert len(r.stdout.splitlines()) == expected, series


def _write_series_fixture(tmp: Path) -> Path:
    """A manifest whose URL carries {seriesdir}/{topic}; the fixture creates
    the resolved dirs for series 7.0 (eol layout) and 7.2."""
    manifest = tmp / "series.yml"
    manifest.write_text(
        "patches:\n"
        "  - name: x86-cflags\n"
        f"    url: file://{tmp}/{{seriesdir}}/{{topic}}/0001-x86-cflags.patch\n"
        "    topic: topic\n"
        "    stage: common\n"
        "    license: GPL-2.0-only\n"
    )
    for series, seriesdir in (
        ("7.0", "eol/linux-7.0.y-xanmod"),
        ("7.2", "linux-7.2.y-xanmod"),
    ):
        sdir = tmp / seriesdir / "topic"
        sdir.mkdir(parents=True)
        (
            sdir / "0001-x86-cflags.patch"
        ).write_text("--- a/README\n+++ b/README\n@@ -1 +1 @@\n-x\n+patched\n")
    return manifest


def test_series_templates_resolve(tmp_path: Path):
    """The helper resolves {seriesdir} (7.0 → eol/ layout) and {topic}, and
    passes the resolved absolute URLs to the patch application.

    Args:
        tmp_path: Scratch directory hosting the fixture dirs.
    """
    manifest = _write_series_fixture(tmp_path)
    r = run_helper(f'kernel_patch_list "{manifest}" amd64 7.0.14')
    assert r.returncode == 0, r.stderr
    assert "eol/linux-7.0.y-xanmod/topic/0001-x86-cflags.patch" in r.stdout
    line = r.stdout.splitlines()[0]
    assert line.startswith("x86-cflags file://")
    assert "{seriesdir}" not in line and "{topic}" not in line


def test_patch_list_from_native_patches_json():
    """PATCHES_JSON (the native export) yields the same lines as the file."""
    import json

    import yaml

    doc = yaml.safe_load(VE_MANIFEST.read_text())
    patches = json.dumps(doc["patches"])
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
    # ARM OPTI (owner 2026-11-09): the arm flavours carry -mcpu=<core>,
    # the level representative the matrix declares; armv8.0 stays bare
    # (it spans Cortex-A53 AND A72 — pinning one CPU mis-tunes the other).
    r = run_helper('kernel_flavour_kcflags armv8.0')
    assert r.stdout.strip() == ""
    r = run_helper('kernel_flavour_kcflags armv8.2-lse')
    assert r.stdout.strip() == "-mcpu=cortex-a76"
    r = run_helper('kernel_flavour_kcflags armv9-sve2')
    assert r.stdout.strip() == "-mcpu=neoverse-v2"


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
