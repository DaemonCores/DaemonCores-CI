"""Packaging contract of the zfs-module deb and the shared ABI slot.

The 14/11 apt-history proof: install zfs-module-pve-x64v3 chains the VM profile
kernel in (and removes the IA stack), install gpu-ia-nvidia-x64v3 reverses it —
because the zfs deb DEPENDED on a concrete profile package while the profile
kernels are mutually Conflicting. The fix lives in the packaging: the zfs-module
deb Depends on the VIRTUAL ABI slot (linux-image-<release>), which BOTH profile
builds Provide, so apt resolves the dependency on the kernel actually installed
and the profile Conflicts stays an intentional shared slot (one
/lib/modules/<release>/ dir, one profile at a time).
"""

import sys
from pathlib import Path

import pytest
from typed_docs import as_list, as_mapping, as_str, find_mapping, load_yaml

REPO = Path(__file__).resolve().parents[1]
PVE_PACKAGES = (
    Path(__file__).resolve().parents[2] / "proxmox-ve-bootc" / "workflows" /
    "bootc-debs-builder" / "packages.yml"
)
DEB_PACKAGES = (
    Path(__file__).resolve().parents[2] / "debian-bootc" / "workflows" /
    "bootc-debs-builder" / "packages.yml"
)
SIBLINGS_YML = REPO / "upstream-track" / "patch-siblings.yml"


@pytest.mark.skipif(
    not PVE_PACKAGES.exists(), reason="proxmox-ve-bootc checkout missing"
)
def test_zfs_module_depends_on_the_abi_virtual() -> None:
    """The zfs-module-pve deb Depends on the virtual linux-image-<KVER>.

    A hard profile Depends CHAINS one profile kernel (mutually Conflicting with
    the sibling) inside the apt transaction. MODVERSIONS is off and both profile
    builds of one release string share the modules dir, so any profile satisfies
    the slot.
    """
    packages = as_list(
        as_mapping(load_yaml(PVE_PACKAGES.read_text()))["packages"]
    )
    entry = find_mapping(packages, "name", "zfs-module-pve")
    build = as_str(entry["build"])
    assert 'ZDEP="linux-image-${KVER}"' in build
    # The Depends chain must carry NO profile package at all.
    zdep_line = "\n".join(
        line for line in build.splitlines()
        if "ZDEP=" in line or '"${ZDEP}"' in line
    )
    assert "daemoncores-kernel-${HDR_PROFILE}" not in zdep_line
    assert "daemoncores-kernel-" not in zdep_line
    # The shared-slot design stays documented in the entry comment.
    assert "SHARED SLOT" in build


@pytest.mark.skipif(
    not DEB_PACKAGES.exists(), reason="debian-bootc checkout missing"
)
def test_both_profiles_provide_the_virtual_slot() -> None:
    """Every profile kernel build Provides the virtual linux-image-<release>.

    The repack printf emits it (= the built version), and the entry expands BOTH
    profiles per flavour, so the zfs module deb satisfies against whichever
    profile the host has installed.
    """
    text = DEB_PACKAGES.read_text()
    assert "'Provides: %s, linux-image-%s (= %s)" in text
    packages = as_list(as_mapping(load_yaml(text))["packages"])
    entry = find_mapping(packages, "name", "linux-image-profile")
    keys = {as_str(variant) for variant in as_mapping(entry["variants"])}
    assert {key.split("-")[0] for key in keys} >= {"ia", "vm"}
    # Two distinct profiles of a flavour (x64v3) both Provide the release.
    assert any(k.startswith("ia-x64v3") for k in keys)
    assert any(k.startswith("vm-x64v3") for k in keys)
    assert "one kernel of one release" in text


@pytest.mark.skipif(
    not SIBLINGS_YML.exists(), reason="patch-siblings.yml missing"
)
def test_bore_xanmod_alternate_sibling_registered() -> None:
    """The BORE entry carries an ALTERNATE sibling source.

    Xanmod linux-patches with the per-series topic file path targeting the same
    `bore` entry: a candidate the election re-judges, never a local patch edit.
    """
    doc = as_mapping(load_yaml(SIBLINGS_YML.read_text()))
    alt = find_mapping(as_list(doc["siblings"]), "name", "bore-xanmod")
    assert alt["entry"] == "bore"
    assert as_str(alt["repo"]).endswith("xanmod/linux-patches")
    assert "linux-{series}.y-xanmod/xanmod/" in as_str(alt["file"])
    assert {as_str(s) for s in as_list(alt["series"])} == {"7.3", "7.2"}
    # The documented K-PORT verdict of the 2026-11-14 probe stays in the
    # entry comment (xanmod ships no bore file at all).
    text = SIBLINGS_YML.read_text()
    assert "K-PORT" in text.split("bore-xanmod")[1].split("- name: msm")[0]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__]))
