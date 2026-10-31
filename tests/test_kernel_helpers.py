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
VE_MANIFEST = Path(
    __file__
).resolve().parents[2] / "DaemonCores-VE" / "workflows" / "bootc-debs-builder" / "kernel-pve" / "patches.yml"
BOOTC_MANIFEST = Path(
    __file__
).resolve().parents[2] / "debian-bootc" / "workflows" / "bootc-debs-builder" / "kernel" / "patches.yml"

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


def test_patch_list_from_file_bootc():
    """The debian-bootc kernel/patches.yml file: class filtering works."""
    r = run_helper(
        f'kernel_patch_list "{BOOTC_MANIFEST}" amd64 6.12.107 kernel',
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip(), "kernel-class patches expected in the common stage"


def test_patch_list_from_file_ve():
    """The VE kernel-pve manifest resolves {seriesdir} (7.0.x → eol/)."""
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
    r = run_helper("kernel_patch_list - amd64 7.0.14 | wc -l", PATCHES_JSON=patches)
    assert r.returncode == 0, r.stderr
    # One line per patch entry: the count belongs to the VE manifest (edited
    # by its own lane), the test pins the helper's semantics, not VE history.
    import json as _json

    assert int(r.stdout.strip()) == len(_json.loads(patches))


def test_patch_list_json_matches_file():
    """PATCHES_JSON (the native export) yields the same lines as the file."""
    import json

    import yaml

    if not VE_MANIFEST.exists():
        return
    patches = json.dumps(yaml.safe_load(VE_MANIFEST.read_text())["patches"])
    from_file = run_helper(f'kernel_patch_list "{VE_MANIFEST}" amd64 7.0.14')
    from_json = run_helper("kernel_patch_list - amd64 7.0.14", PATCHES_JSON=patches)
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
    return tmp_path / "cdn", tarball, hashlib.sha256(tarball.read_bytes()).hexdigest()


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
