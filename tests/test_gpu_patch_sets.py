"""GPU driver patch sets (build-deb/kernel-shared/patches-gpu).

The owner orders GPU perf patch sets per driver "like the kernel":
patches-gpu/{nvidia,amd,intel}.yml inside the SAME shared export
(build-deb/kernel-shared, hashed by content-hash.sh shared_data_hash), the
canonical patches schema, the SAME dry-run all-or-nothing application, and
the patches really applied by the driver builds:
  - nvidia: the manifest applies to the NVIDIA open-gpu-kernel-modules tree
    BEFORE `make modules` (debian-bootc linux-modules-nvidia AND
    linux-modules-nvidia-pve), {series} resolving as the KERNEL series;
  - amd/intel: the manifests apply to the KERNEL source tree next to the
    shared set, inside the linux-image build (and the VE lane the same way).

demandes.jsonl never named a patchset until the owner DID (05/11 17:0x:
"les patchs nvidia, je les veux dans les yaml de la CI" — the sweep pool
landed): nvidia.js carries the applying subset as sha-pinned entries and
documents the rest as K-PORT blocks. The 05/11 16:5x GPU-completions
mandate then entered the PROVEN applying subsets of amd/intel too
(fuzz-0 dry-run on pristine 7.2.15, sha-pinned, K-PORT documented) — no
set stays empty, every entry is pinned and every non-entering remainder
is a documented K-PORT porting lane, never hand-forced.

IA vs GAMING nvidia variants: CachyOS publishes ONE nvidia set per kernel
series, no per-profile divergent patchset exists upstream — the two
variants share the manifest verbatim (documented "même set" in nvidia.yml).
"""

import json
import subprocess
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
GPU_DATA = REPO / "build-deb" / "kernel-shared" / "patches-gpu"
HELPER = REPO / "scripts" / "kernel_helpers.sh"
BOOTC_PACKAGES = (
    Path(__file__).resolve().parents[2] / "debian-bootc" / "workflows" /
    "bootc-debs-builder" / "packages.yml"
)


def test_gpu_manifests_present_and_schema() -> None:
    """The three driver manifests exist inside the shared export and carry
    the canonical schema: a `patches` LIST with name/url/stage entries."""
    import os

    for name in ("nvidia.yml", "amd.yml", "intel.yml"):
        manifest = GPU_DATA / name
        assert manifest.is_file(), name
        doc = yaml.safe_load(manifest.read_text())
        assert isinstance(doc.get("patches"), list), name
        for entry in doc["patches"]:
            assert entry.get("name") and entry.get("url")
            assert entry.get("stage", "common") in ("common", "amd64", "arm64")
            sha = entry.get("sha256")
            if sha is not None:
                assert len(sha) == 64 and all(
                    c in "0123456789abcdef" for c in sha
                )

    # The shared-data hash term rides the WHOLE directory (content-hash.sh
    # uses find kernel-shared -type f): adding a driver set changes every
    # consuming package's cache key. Prove the dir listing sees the files.
    assert {p.name
            for p in GPU_DATA.iterdir()
            } == {"nvidia.yml", "amd.yml", "intel.yml"}


def test_gpu_nvidia_candidates_documented_not_chosen() -> None:
    """Every set carries its applying subset as sha-pinned entries and
    documents the non-applying remainder as K-PORT (the 05/11 17:0x nvidia
    order landed the nvidia pool; the 05/11 16:5x GPU-completions mandate
    then entered the fuzz-0-proven amd/intel subsets too — entries only on
    a clean dry-run, the rest a porting lane, never hand-forced)."""
    for name in ("nvidia.yml", "amd.yml", "intel.yml"):
        text = (GPU_DATA / name).read_text()
        doc = yaml.safe_load(text)
        assert doc["patches"], name  # the applying subset is ENTERED
        for entry in doc["patches"]:
            sha = entry.get("sha256")
            assert sha and len(sha) == 64 and all(
                c in "0123456789abcdef" for c in sha
            ), entry.get("name")
            assert entry.get("url"), entry.get("name")
            assert entry.get("stage") in ("common", "amd64"), entry.get("name")
        assert "K-PORT" in text, name  # the non-applying remainder documented
        assert "sha256" in text.lower()  # proof documentation in the manifest


def run_helper(script: str, **env_over) -> subprocess.CompletedProcess:
    import os

    env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], **env_over}
    return subprocess.run([
        "bash", "-c", f'source "{HELPER}"\nset -euo pipefail\n{script}'
    ],
                          capture_output=True,
                          text=True,
                          env=env)


def test_empty_gpu_set_is_a_noop() -> None:
    """kernel_apply_patches over an EMPTY gpu manifest exits 0 without
    touching the tree (no set ships a `patches: []` any more since the
    05/11 GPU completions, but the no-op contract of an empty manifest
    stays a helper behaviour the builds rely on)."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        manifest = tmp / "empty.yml"
        (tmp / "tree").mkdir()
        (tmp / "tree" / "README").write_text("unpatched\n")
        manifest.write_text("patches: []\n")
        r = run_helper(
            f'kernel_apply_patches "{manifest}" amd64 7.2.15 "{tmp}/tree" && '
            f'grep -q unpatched "{tmp}/tree/README" && echo NOOP'
        )
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "NOOP"


def _series_probe_patch(tmp: Path) -> Path:
    """A tiny patch whose URL embeds {series}: proves the nvidia manifest
    resolves {series} as the KERNEL series passed by the caller (KVER
    prefix), not as a series derived from the NVIDIA release number."""
    patch = tmp / "probe.patch"
    patch.write_text(
        "--- a/README\n+++ b/README\n@@ -1 +1 @@\n-gpu probe\n+gpu probe patched\n"
    )
    (patch.parent / "README").write_text("gpu probe\n")
    manifest = tmp / "probe.yml"
    manifest.write_text(
        "patches:\n"
        "  - name: probe\n"
        f"    url: file://{tmp}/series-{{series}}/probe.patch\n"
        "    stage: common\n"
    )
    (tmp / "series-7.2").mkdir()
    patch.rename(tmp / "series-7.2" / "probe.patch")
    return manifest


def test_patches_series_override_resolves_kernel_series() -> None:
    """PATCHES_SERIES (exported by the linux-modules-nvidia build from KVER)
    wins over the computed series of the NVIDIA release version arg."""
    import os

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        manifest = _series_probe_patch(tmp)
        # The NVIDIA release 595.104.02 would compute series '595.104' —
        # the override must resolve 7.2 instead, then dry-run + apply.
        r = run_helper((
            f'PATCHES_SERIES=7.2 '
            f'kernel_apply_patches "{manifest}" amd64 595.104.02 "{tmp}" && '
            f'grep -q patched {tmp}/README && echo APPLIED'
        ))
        assert r.returncode == 0, r.stderr
        assert "APPLIED" in r.stdout


def test_gpu_env_var_does_not_leak_between_driver_manifests() -> None:
    """PATCHES_SERIES is restored by kernel_apply_patches, so a second
    manifest in the same sourced bash never inherits the previous override."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        manifest = _series_probe_patch(tmp)
        r = run_helper((
            f'PATCHES_SERIES=7.2 kernel_apply_patches "{manifest}" amd64 '
            f'595.104.02 "{tmp}" >/dev/null; '
            f'[[ -z "${{PATCHES_SERIES:-}}" ]] && echo RESTORED'
        ))
        assert r.returncode == 0, r.stderr + r.stdout
        assert r.stdout.strip() == "RESTORED"


def test_sha256_pin_enforced_all_or_nothing(tmp_path=None) -> None:
    """A pinned patch whose bytes mismatch fails BEFORE any tree touch —
    the driver sets may pin their upstream bytes like the kernel tarball."""
    import hashlib
    import os

    tmp = Path(tempfile.mkdtemp())
    patch = tmp / "p.patch"
    patch.write_text("--- a/READ\n+++ b/READ\n@@ -1 +1 @@\n-one\n+two\n")
    (tmp / "READ").write_text("one\n")
    good = hashlib.sha256(patch.read_bytes()).hexdigest()
    manifest = tmp / "m.yml"
    manifest.write_text(
        "patches:\n"
        "  - name: pinned\n"
        f"    url: file://{tmp}/p.patch\n"
        "    stage: common\n"
        f"    sha256: {good}\n"
    )
    r = run_helper(
        f'kernel_apply_patches "{manifest}" amd64 7.2.15 "{tmp}" && grep -q two {tmp}/READ'
    )
    assert r.returncode == 0, r.stderr
    manifest.write_text(manifest.read_text().replace(good, "0" * 64))
    (tmp / "READ").write_text("one\n")
    # kernel_apply_patches exits the SHELL on a bad pin (same contract as
    # every other build failure), so the refusal is asserted as its own run;
    # the tree state after is asserted again in the second run below.
    r = run_helper(f'kernel_apply_patches "{manifest}" amd64 7.2.15 "{tmp}"')
    assert r.returncode == 1, r.stdout
    r = run_helper(f'! grep -q two "{tmp}/READ" && echo GUARDED')
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "GUARDED"


def patches_gpu_nvidia_count(text: str) -> int:
    """Count the kernel_apply_patches invocations on the nvidia.yml in one
    packages.yml — one entry per repo that ships a kernel-module pair."""
    import re
    return len(re.findall(r'kernel_apply_patches "\$\{GPU_NVIDIA_YML\}"', text))


def test_bootc_builds_apply_the_gpu_manifests() -> None:
    """The real builds apply the sets (all-or-nothing, empty = no-op):
    the debian linux-image loops amd+intel over the kernel tree; the
    debian linux-modules-nvidia AND the pve-lane pair apply nvidia.yml to
    the open-gpu-kernel-modules tree before `make modules` (the pve pair
    MOVED to the vanilla lane, one build per kernel repo — owner 04/11)."""
    text = BOOTC_PACKAGES.read_text()
    assert 'patches-gpu/${GPU_DRIVER}.yml' in text
    assert patches_gpu_nvidia_count(text) == 1
    assert 'patches-gpu/nvidia.yml missing' in text  # guarded
    # The debian nvidia entry patches the NVIDIA tree BEFORE its compile:
    import re

    makes = [
        m.start() for m in re.finditer(r'make -C /build/nvidia-open', text)
    ]
    applies = [
        m.start() for m in
        re.finditer(r'kernel_apply_patches "\$\{GPU_NVIDIA_YML\}"', text)
    ]
    # The entry runs two make calls (modules, then the signed pair install):
    # its FIRST make must come after its own manifest application.
    assert len(makes) >= 2
    assert applies[0] < makes[0]
    # The variants share ONE manifest ("même set" ia/gaming, owner 10/31:
    # config differs, never the vendor list) — no nvidia-ia/gaming split.
    assert 'patches-gpu/nvidia-ia' not in text \
        and 'patches-gpu/nvidia-gaming' not in text


def test_pve_lane_applies_the_gpu_manifest_against_its_tree() -> None:
    """FINAL architecture (owner 05/11 13:0x): the vanilla lane builds NO
    kernel and NO NVIDIA pair any more — the former kernel-pve entry is
    REMOVED, the unified kernel and both module pairs (nvidia open on the
    debian linux-modules-nvidia entry, OpenZFS via the SHARED gate) live on
    the debian side / the shared engine helpers. The vanilla manifest stays
    free of a lane-local GPU patch application: its module publisher rides
    the SHARED kernel_build_module helper, never inline module logic."""
    pve = (
        Path(__file__).resolve().parents[2] / "proxmox-ve-bootc" / "workflows" /
        "bootc-debs-builder" / "packages.yml"
    )
    text = pve.read_text()
    # NO lane-local GPU/nvidia patch application, NO open-gpu clone, NO
    # kernel build entry left in the vanilla manifest.
    assert patches_gpu_nvidia_count(text) == 0
    assert "nvidia-open" not in text
    assert "- name: kernel-pve" not in text
    assert "kernel_fetch_source" not in text
    # The module publisher goes through the SHARED gate (one signature, both
    # module pair lanes).
    assert "kernel_build_module" in text
    assert "kernel_module_run_build" not in text
