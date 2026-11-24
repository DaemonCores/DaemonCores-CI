"""Unit tests for ``scripts/boot_part_mb.py`` (gallium boot partition size)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

import boot_part_mb

# Sizes of the gallium arm64 image published on 2026-11-22
# (usr/lib/modules/7.2.15/{vmlinuz,initramfs.img}).
VMLINUZ = 61_690_712
INITRAMFS = 47_270_398
OLD_FIXED_MB = 64


def test_the_old_fixed_partition_could_not_hold_the_real_boot_image() -> None:
    """The bug: 64M against a 109 MB kernel + initramfs (dd ENOSPC)."""
    assert (VMLINUZ + INITRAMFS) > OLD_FIXED_MB * 1024 * 1024


def test_computed_partition_holds_the_real_boot_image() -> None:
    """The computed size covers the payload with room for the header."""
    mb = boot_part_mb.boot_part_mb([VMLINUZ, INITRAMFS])
    assert mb * 1024 * 1024 >= VMLINUZ + INITRAMFS + 8 * 1024 * 1024


def test_size_is_whole_mib_rounded_up() -> None:
    """One byte over a MiB boundary takes the next MiB."""
    assert boot_part_mb.boot_part_mb([1]) == 9
    assert boot_part_mb.boot_part_mb([1024 * 1024]) == 10


@pytest.mark.parametrize('sizes', [[], [0], [-5, 10]])
def test_rejects_missing_or_non_positive_sizes(sizes: list[int]) -> None:
    """No size, or a bogus one, must fail rather than yield a tiny partition.

    Args:
        sizes: Payload sizes that must be rejected.
    """
    with pytest.raises(ValueError, match='need positive file sizes'):
        _ = boot_part_mb.boot_part_mb(sizes)


def test_cli_prints_the_size_and_fails_on_garbage(
    capsys: pytest.CaptureFixture[str]
) -> None:
    """The script contract: MiB on stdout, exit 2 on a bad argument.

    Args:
        capsys: Output capture.
    """
    assert boot_part_mb.main([str(VMLINUZ), str(INITRAMFS)]) == 0
    assert capsys.readouterr().out.strip() == str(
        boot_part_mb.boot_part_mb([VMLINUZ, INITRAMFS])
    )
    assert boot_part_mb.main(['abc']) == 2
