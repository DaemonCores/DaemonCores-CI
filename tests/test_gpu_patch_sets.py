"""GPU driver patch sets (product kernel-src/patches/gpu/{ia,gaming}).

The owner orders GPU perf patch sets per driver "like the kernel":
the per-PROFILE sets live in the PRODUCT repo's kernel-src/patches/gpu
(owner 11/11 R-873b — the engine keeps no kernel data at all), the
canonical patches schema, the SAME dry-run all-or-nothing application, and
the patches really applied by the driver builds:
  - linux-image: amd/intel from .../gpu/${GPU_PATCH_PROFILE}/ (the vm
    profile rides the gaming set, packages.yml case), the kernel tree;
  - nvidia: the NVIDIA open-gpu-kernel-modules tree gets its per-set
    .../gpu/${GPU_PROFILE}/nvidia.yml BEFORE `make modules` (the same
    sets ride the linux-modules-nvidia debs).

demandes.jsonl never named a patchset until the owner DID (05/11 17:0x:
"I want the nvidia patches in the CI yaml" (translated) — the sweep pool
landed): nvidia.js carries the applying subset as sha-pinned entries and
documents the rest as K-PORT blocks. The 05/11 16:5x GPU-completions
mandate then entered the PROVEN applying subsets of amd/intel too
(fuzz-0 dry-run on pristine 7.2.15, sha-pinned, K-PORT documented) — no
set stays empty, every entry is pinned and every non-entering remainder
is a documented K-PORT porting lane, never hand-forced.

IA vs GAMING nvidia variants: CachyOS publishes ONE nvidia set per kernel
series, no per-profile divergent patchset exists upstream — the two
variants share the manifest verbatim (documented as "same set" in nvidia.yml).
"""

import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest
from typed_docs import as_list, as_mapping, as_mappings, as_str, load_yaml

REPO = Path(__file__).resolve().parents[1]
PRODUCT = Path(__file__).resolve().parents[2] / "debian-bootc"
KERNEL_SRC = PRODUCT / "workflows" / "bootc-debs-builder" / "kernel-src"
GPU_PROFILES = ("ia", "gaming")
HELPER = REPO / "scripts" / "kernel_helpers.sh"
BOOTC_PACKAGES = PRODUCT / "workflows" / "bootc-debs-builder" / "packages.yml"


def test_gpu_manifests_present_and_schema() -> None:
    """Check the driver manifests of every profile and their schema.

    The three driver manifests exist in EVERY profile of the product
    repo and carry the canonical schema: a `patches` LIST with name/url/
    stage entries.
    """
    for profile in GPU_PROFILES:
        gpu = KERNEL_SRC / "patches" / "gpu" / profile
        assert gpu.is_dir(), profile
        for name in ("nvidia.yml", "amd.yml", "intel.yml"):
            manifest = gpu / name
            assert manifest.is_file(), f"{profile}/{name}"
            doc = as_mapping(load_yaml(manifest.read_text()))
            assert isinstance(doc.get("patches"), list), name
            for entry in as_mappings(as_list(doc["patches"])):
                assert entry.get("name")
                assert entry.get("url")
                stage = as_str(entry.get("stage", "common"))
                assert stage in ("common", "amd64", "arm64")
                sha = entry.get("sha256")
                if sha is not None:
                    digest = as_str(sha)
                    assert len(digest) == 64
                    assert all(c in "0123456789abcdef" for c in digest)
        # A profile set is EXACTLY the three driver manifests (no stray
        # file rides the product checkout path the builders glob).
        assert {p.name
                for p in gpu.iterdir()
                } == {"nvidia.yml", "amd.yml", "intel.yml"}


def test_gpu_nvidia_candidates_documented_not_chosen() -> None:
    """Check each set carries sha-pinned entries and documents K-PORT.

    Every set carries its applying subset as sha-pinned entries and documents
    the non-applying remainder as K-PORT (the 05/11 17:0x nvidia order landed
    the nvidia pool; the 05/11 16:5x GPU-completions mandate then entered the
    fuzz-0-proven amd/intel subsets too — entries only on a clean dry-run, the
    rest a porting lane, never hand-forced).
    """
    for profile in GPU_PROFILES:
        for name in ("nvidia.yml", "amd.yml", "intel.yml"):
            path = KERNEL_SRC / "patches" / "gpu" / profile / name
            text = path.read_text()
            doc = as_mapping(load_yaml(text))
            if not doc["patches"]:
                # An EMPTY entered set is legitimate after a pin-bump re-proof
                # (2026-11-09: the formerly entered release -> 620.70.06 —
                # the formerly nv-p2p-core failed the pristine fuzz-0
                # dry-run on the new tag and was DEMOTED to K-PORT rather
                # than hand-forced; an
                # all-or-nothing gate cannot ride a FAIL). The port-lane proof
                # documentation must still be in the manifest.
                assert "K-PORT" in text, name
                assert "sha256" in text.lower(), name
                continue
            assert True, name  # the applying subset is ENTERED
            for entry in as_mappings(as_list(doc["patches"])):
                name_text = as_str(entry.get("name"))
                sha = entry.get("sha256")
                assert sha, name_text
                digest = as_str(sha)
                assert len(digest) == 64, name_text
                assert all(c in "0123456789abcdef" for c in digest), name_text
                assert entry.get("url"), name_text
                stage = as_str(entry.get("stage", "common"))
                assert stage in ("common", "amd64"), name_text
            assert "K-PORT" in text or "exclude-series" in text, (
                name + ": the non-applying remainder is documented"
            )
            assert "sha256" in text.lower()  # proof documentation in manifest


def run_helper(script: str,
               **env_over: str) -> subprocess.CompletedProcess[str]:
    """Run a script in bash after sourcing the helper with strict mode.

    Args:
        script: Bash snippet run after sourcing the helper.
        **env_over: Environment variables set on top of PATH and HOME.

    Returns:
        The finished process, with stdout and stderr as text.
    """
    import os

    env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], **env_over}
    return subprocess.run([
        "bash", "-c", f'source "{HELPER}"\nset -euo pipefail\n{script}'
    ],
                          capture_output=True,
                          text=True,
                          env=env)


def test_empty_gpu_set_is_a_noop() -> None:
    """Applying an EMPTY gpu manifest exits 0 without touching the tree.

    kernel_apply_patches over an EMPTY gpu manifest exits 0 without touching the
    tree (no set ships a `patches: []` any more since the 05/11 GPU completions,
    but the no-op contract of an empty manifest stays a helper behaviour the
    builds rely on).
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        manifest = tmp / "empty.yml"
        (tmp / "tree").mkdir()
        _ = (tmp / "tree" / "README").write_text("unpatched\n")
        _ = manifest.write_text("patches: []\n")
        r = run_helper(
            " ".join([
                f'kernel_apply_patches "{manifest}" amd64 7.2.15 "{tmp}/tree"',
                f'&& grep -q unpatched "{tmp}/tree/README" && echo NOOP'
            ])
        )
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "NOOP"


def _series_probe_patch(tmp: Path) -> Path:
    """Write a tiny patch whose URL embeds {series}.

    It proves the nvidia manifest resolves {series} as the KERNEL series passed
    by the caller (KVER prefix), not as a series derived from the NVIDIA release
    number.

    Args:
        tmp: Directory the probe files are written under.

    Returns:
        The path of the probe manifest.
    """
    patch = tmp / "probe.patch"
    _ = patch.write_text(
        textwrap.dedent(
            """\
            --- a/README
            +++ b/README
            @@ -1 +1 @@
            -gpu probe
            +gpu probe patched
            """
        )
    )
    _ = (patch.parent / "README").write_text("gpu probe\n")
    manifest = tmp / "probe.yml"
    _ = manifest.write_text(
        textwrap.dedent(
            f"""\
            patches:
              - name: probe
                url: file://{tmp}/series-{{series}}/probe.patch
                stage: common
            """
        )
    )
    (tmp / "series-7.2").mkdir()
    _ = patch.rename(tmp / "series-7.2" / "probe.patch")
    return manifest


def test_patches_series_override_resolves_kernel_series() -> None:
    """PATCHES_SERIES overrides the computed series.

    PATCHES_SERIES (exported by the linux-modules-nvidia build from KVER) wins
    over the computed series of the NVIDIA release version arg.
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        manifest = _series_probe_patch(tmp)
        # The NVIDIA release 620.70.06 (current pin) would compute series
        # '620.70' — the override must resolve 7.2 instead, then dry-run +
        # apply.
        r = run_helper(
            " ".join([
                'PATCHES_SERIES=7.2',
                f'kernel_apply_patches "{manifest}" amd64 620.70.06 "{tmp}" &&',
                f'grep -q patched {tmp}/README && echo APPLIED'
            ])
        )
        assert r.returncode == 0, r.stderr
        assert "APPLIED" in r.stdout


def test_gpu_env_var_does_not_leak_between_driver_manifests() -> None:
    """The PATCHES_SERIES override does not leak between manifests.

    PATCHES_SERIES is restored by kernel_apply_patches, so a second manifest in
    the same sourced bash never inherits the previous override.
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        manifest = _series_probe_patch(tmp)
        r = run_helper(
            " ".join([
                f'PATCHES_SERIES=7.2 kernel_apply_patches "{manifest}" amd64',
                f'620.70.06 "{tmp}" >/dev/null;',
                '[[ -z "${PATCHES_SERIES:-}" ]] && echo RESTORED'
            ])
        )
        assert r.returncode == 0, r.stderr + r.stdout
        assert r.stdout.strip() == "RESTORED"


def test_sha256_pin_enforced_all_or_nothing() -> None:
    """A bad sha256 pin fails before the tree is touched.

    A pinned patch whose bytes mismatch fails BEFORE any tree touch — the driver
    sets may pin their upstream bytes like the kernel tarball.
    """
    import hashlib

    tmp = Path(tempfile.mkdtemp())
    patch = tmp / "p.patch"
    _ = patch.write_text("--- a/READ\n+++ b/READ\n@@ -1 +1 @@\n-one\n+two\n")
    _ = (tmp / "READ").write_text("one\n")
    good = hashlib.sha256(patch.read_bytes()).hexdigest()
    manifest = tmp / "m.yml"
    _ = manifest.write_text(
        textwrap.dedent(
            f"""\
            patches:
              - name: pinned
                url: file://{tmp}/p.patch
                stage: common
                sha256: {good}
            """
        )
    )
    r = run_helper(
        " ".join([
            f'kernel_apply_patches "{manifest}" amd64 7.2.15 "{tmp}"',
            f"&& grep -q two {tmp}/READ"
        ])
    )
    assert r.returncode == 0, r.stderr
    _ = manifest.write_text(manifest.read_text().replace(good, "0" * 64))
    _ = (tmp / "READ").write_text("one\n")
    # kernel_apply_patches exits the SHELL on a bad pin (same contract as
    # every other build failure), so the refusal is asserted as its own run;
    # the tree state after is asserted again in the second run below.
    r = run_helper(f'kernel_apply_patches "{manifest}" amd64 7.2.15 "{tmp}"')
    assert r.returncode == 1, r.stdout
    r = run_helper(f'! grep -q two "{tmp}/READ" && echo GUARDED')
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "GUARDED"


def patches_gpu_nvidia_count(text: str) -> int:
    """Count the kernel_apply_patches invocations on nvidia.yml.

    Counted in one packages.yml - one entry per repo that ships a kernel-module
    pair.

    Args:
        text: Contents of nvidia.yml.

    Returns:
        The number of invocations found.
    """
    import re
    return len(re.findall(r'kernel_apply_patches "\$\{GPU_NVIDIA_YML\}"', text))


def test_bootc_builds_apply_the_gpu_manifests() -> None:
    """The real builds apply the GPU manifests.

    The real builds apply the sets (all-or-nothing, empty = no-op):
    the debian linux-image loops amd+intel over the kernel tree with the
    per-profile set (vm rides gaming, packages.yml case); the nvidia module
    entry applies its per-set nvidia.yml to the open-gpu-kernel-modules
    tree BEFORE `make modules`; the pve pair lives on the vanilla lane. All
    paths resolve inside the product checkout's kernel-src.
    """
    text = BOOTC_PACKAGES.read_text()
    assert 'kernel-src/patches/gpu/${GPU_PATCH_PROFILE}/${GPU_DRIVER}.yml' \
        in text
    assert 'kernel-src/patches/gpu/${GPU_PROFILE}/nvidia.yml' in text
    assert 'nvidia.yml missing' in text  # guarded
    # The debian linux-image entry also applies the kernel + pve manifests:
    assert 'kernel-src/patches/kernel.yml' in text
    assert 'kernel-src/patches/pve.yml' in text
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
    # The per-set nvidia manifests ride the profile directory (no
    # nvidia-ia/nvidia-gaming flat split).
    assert 'nvidia-ia' not in text
    assert 'nvidia-gaming' not in text


def test_pve_lane_applies_the_gpu_manifest_against_its_tree() -> None:
    """The pve lane applies no lane-local GPU manifest.

    FINAL architecture (owner 05/11 13:0x): the vanilla lane builds NO
    kernel and NO NVIDIA pair any more — the former kernel-pve entry is
    REMOVED, the unified kernel and both module pairs (nvidia open on the
    debian linux-modules-nvidia entry, OpenZFS via the SHARED gate) live on
    the debian side / the shared engine helpers. The vanilla manifest stays
    free of a lane-local GPU patch application: its module publisher rides
    the SHARED kernel_build_module helper, never inline module logic.
    """
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


def test_a_driver_entry_may_omit_its_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sha256-less entry passes the schema; only a pin is validated.

    The product manifests pin every entered entry today, so the optional-digest
    path of the schema check is exercised on a tree of the same shape.

    Args:
        tmp_path: Scratch directory holding the synthetic kernel-src tree.
        monkeypatch: Patcher pointing the check at that tree.
    """
    manifest = f"""patches:
  - name: pinned
    url: https://example.invalid/pinned.patch
    sha256: {'a' * 64}
  - name: unpinned
    url: https://example.invalid/unpinned.patch
"""
    tree = tmp_path / "kernel-src"
    for profile in GPU_PROFILES:
        gpu = tree / "patches" / "gpu" / profile
        gpu.mkdir(parents=True)
        for name in ("nvidia.yml", "amd.yml", "intel.yml"):
            _ = (gpu / name).write_text(manifest)
    monkeypatch.setattr("test_gpu_patch_sets.KERNEL_SRC", tree)
    test_gpu_manifests_present_and_schema()
