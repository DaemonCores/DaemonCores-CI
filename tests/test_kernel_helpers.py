"""Kernel helper (scripts/kernel_helpers.sh) contract tests.

The kernel build primitives are shared by two kernel modes — DaemonCores-VE
kernel-pve (PVE packaging + XanMod) and debian-bootc linux-image (kernel.org
rolling), both consumed by the bootc-debs builders. The manifest schema is
CANONICAL: declarative patches data (a patches.yml file or the native
PATCHES_JSON entry export), consumed through yq, never a bashified list.

Reference data (owner 11/11 R-870b): the product-canonical manifests live in the
debian-bootc checkout (kernel-src/patches); this test suite reads the pve.yml
and kernel.yml from that checkout as the reference data the helper must resolve.
"""

import json
import os
import subprocess
from pathlib import Path

from typed_docs import (
    as_list,
    as_mapping,
    as_mappings,
    as_str,
    as_str_mapping,
    load_yaml,
)

REPO = Path(__file__).resolve().parents[1]
HELPER = REPO / "scripts" / "kernel_helpers.sh"
PRODUCT = Path(__file__).resolve().parents[2] / "debian-bootc"
KERNEL_SRC = PRODUCT / "workflows" / "bootc-debs-builder" / "kernel-src"
KERNEL_MANIFEST = KERNEL_SRC / "patches" / "kernel.yml"
VE_MANIFEST = KERNEL_SRC / "patches" / "pve.yml"

ENV = {"KERNEL_CDN": "", "PATH": os.environ["PATH"], "HOME": os.environ["HOME"]}


def run_helper(script: str,
               **env_over: str) -> subprocess.CompletedProcess[str]:
    """Run a script in bash after sourcing the kernel helper library.

    Args:
        script: Bash snippet run after sourcing the helper.
        **env_over: Environment variables set on top of the base environment.

    Returns:
        The finished process, with stdout and stderr as text.
    """
    env = dict(ENV, **env_over)
    return subprocess.run(
        ["bash", "-c", f'source "{HELPER}"\n{script}'],
        capture_output=True,
        text=True,
        env=env,
    )


def test_helper_shell_syntax() -> None:
    """The helper script passes a bash syntax check."""
    assert subprocess.run(["bash", "-n", str(HELPER)]).returncode == 0


def _expected_class_count(
    doc: dict[str, object], series: str, classes: tuple[str, ...]
) -> int:
    """Count the entries the helper lists for a series and stage classes.

    An entry excluded on the series (exclude-series) or staged outside the
    classes drops.

    Args:
        doc: Parsed patch manifest.
        series: Kernel series being built.
        classes: Stage classes an entry must belong to.

    Returns:
        The number of entries the helper should list.
    """
    return sum(
        1 for entry in as_mappings(as_list(doc["patches"]))
        if series not in as_list(entry.get("exclude-series") or [])
        and as_str(entry.get("stage", "common")) in classes
    )


def test_patch_list_from_native_manifest() -> None:
    """Resolve the class filter over the reference pve.yml via PATCHES_JSON.

    The product packages.yml carries NO native patch data (owner R-870b: the
    canonical set lives in kernel-src/patches).
    """
    manifest = PRODUCT / "workflows" / "bootc-debs-builder" / "packages.yml"
    assert manifest.is_file()
    packages = as_mapping(load_yaml(manifest.read_text()))
    assert not [
        p for p in as_mappings(as_list(packages["packages"]))
        if p["name"] == "linux-image" and "patches" in p
    ]
    doc = as_str_mapping(load_yaml(VE_MANIFEST.read_text()))
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
    """Write a manifest whose URL carries {seriesdir}/{topic}.

    The fixture creates the resolved dirs for series 7.0 (eol layout) and 7.2.

    Args:
        tmp: Directory the manifest and the series dirs are written under.

    Returns:
        The path of the manifest.
    """
    manifest = tmp / "series.yml"
    _ = manifest.write_text(
        ''.join([
            "patches:\n",
            "  - name: x86-cflags\n",
            f"    url: file://{tmp}/{{seriesdir}}/{{topic}}/",
            "0001-x86-cflags.patch\n",
            "    topic: topic\n",
            "    stage: common\n",
            "    license: GPL-2.0-only\n",
        ])
    )
    for seriesdir in (
        "eol/linux-7.0.y-xanmod",
        "linux-7.2.y-xanmod",
    ):
        sdir = tmp / seriesdir / "topic"
        sdir.mkdir(parents=True)
        _ = (
            sdir / "0001-x86-cflags.patch"
        ).write_text("--- a/README\n+++ b/README\n@@ -1 +1 @@\n-x\n+patched\n")
    return manifest


def test_series_templates_resolve(tmp_path: Path) -> None:
    """Resolve {seriesdir} (7.0 → eol/ layout) and {topic} in patch URLs.

    The helper passes the resolved absolute URLs to the patch application.

    Args:
        tmp_path: Scratch directory hosting the fixture dirs.
    """
    manifest = _write_series_fixture(tmp_path)
    r = run_helper(f'kernel_patch_list "{manifest}" amd64 7.0.14')
    assert r.returncode == 0, r.stderr
    assert "eol/linux-7.0.y-xanmod/topic/0001-x86-cflags.patch" in r.stdout
    line = r.stdout.splitlines()[0]
    assert line.startswith("x86-cflags file://")
    assert "{seriesdir}" not in line
    assert "{topic}" not in line


def test_patch_list_from_native_patches_json() -> None:
    """PATCHES_JSON (the native export) yields the same lines as the file."""
    doc = as_str_mapping(load_yaml(VE_MANIFEST.read_text()))
    patches = json.dumps(doc["patches"])
    from_file = run_helper(f'kernel_patch_list "{VE_MANIFEST}" amd64 7.0.14')
    from_json = run_helper(
        "kernel_patch_list - amd64 7.0.14", PATCHES_JSON=patches
    )
    assert from_file.returncode == 0
    assert from_json.returncode == 0
    assert from_file.stdout == from_json.stdout
    assert from_json.stdout.strip()


def test_flavour_kcflags() -> None:
    """Each kernel flavour maps to its expected compiler flags."""
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


def _make_cdn(tmp_path: Path, version: str) -> tuple[Path, Path, str]:
    """Build a file:// kernel.org mirror holding a tiny linux tarball.

    Args:
        tmp_path: Pytest temporary directory.
        version: Kernel version the tarball is built for.

    Returns:
        The mirror root, the tarball path, and the sha256 hex digest of the
        tarball.
    """
    import hashlib
    import shutil
    import tarfile

    cdn = tmp_path / "cdn" / f"v{version.split('.')[0]}.x"
    cdn.mkdir(parents=True)
    src = tmp_path / f"linux-{version}"
    src.mkdir()
    _ = (src / "Makefile").write_text("x\n")
    tarball = cdn / f"linux-{version}.tar.xz"
    with tarfile.open(tarball, "w:xz") as t:
        t.add(src, arcname=f"linux-{version}")
    shutil.rmtree(src)
    return tmp_path / "cdn", tarball, hashlib.sha256(tarball.read_bytes()
                                                     ).hexdigest()


def _fetch(cdn: Path, version: str, want: str,
           cache: Path) -> subprocess.CompletedProcess[str]:
    """Run kernel_fetch_source against the mirror with the given cache.

    Args:
        cdn: Root of the file:// mirror.
        version: Kernel version to fetch.
        want: Expected sha256 hex digest of the tarball.
        cache: Tarball cache directory.

    Returns:
        The finished process, with stdout and stderr as text.
    """
    return run_helper(
        f'kernel_fetch_source {version} {want} && echo "SRC=$SRCDIR"',
        KERNEL_CDN=f"file://{cdn}",
        KERNEL_TARBALL_CACHE=str(cache),
    )


def test_tarball_cache_miss_then_hit(tmp_path: Path) -> None:
    """A second fetch is served from the cache without the network.

    Args:
        tmp_path: Pytest temporary directory.
    """
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


def test_tarball_cache_corrupted_is_redownloaded(tmp_path: Path) -> None:
    """A corrupted cached tarball is downloaded again.

    Args:
        tmp_path: Pytest temporary directory.
    """
    version = "9.97.2"
    cdn, tarball, want = _make_cdn(tmp_path, version)
    cache = tmp_path / "cache"
    cache.mkdir()
    _ = (cache / tarball.name).write_text("corrupted")
    r = _fetch(cdn, version, want, cache)
    assert r.returncode == 0, r.stderr
    assert "downloading again" in r.stderr


def test_tarball_checksum_mismatch_fails(tmp_path: Path) -> None:
    """A checksum mismatch fails and leaves nothing in the cache.

    Args:
        tmp_path: Pytest temporary directory.
    """
    version = "9.97.3"
    cdn, tarball, _ = _make_cdn(tmp_path, version)
    cache = tmp_path / "cache"
    r = _fetch(cdn, version, "0" * 64, cache)
    assert r.returncode == 1
    assert "expected" in r.stderr
    assert not (cache / tarball.name).exists()
