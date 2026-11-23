"""boot_part_mb.py - size of the raw Android boot partition, in MiB.

Usage:
    python3 boot_part_mb.py BYTES [BYTES ...]

``build-img.sh`` writes one ``boot.img`` (kernel + initramfs + DTB, packed by
mkbootimg) into a raw partition made before the image is laid, so the size
must be derived from what goes into it. Each argument is the size in bytes of
one packed file; the result is their total plus a margin for the mkbootimg
header, page padding and the DTB, rounded up to whole MiB and printed on
stdout. A fixed size broke the gallium build once the kernel and initramfs
reached 109 MB ("dd: error writing '/dev/loop0p1': No space left on device").
"""

import sys

MIB = 1024 * 1024
# Margin over the payload: 10 % plus a flat 8 MiB (header, 2 KiB page
# padding per section, the board DTB).
MARGIN_PERCENT = 10
MARGIN_FLAT = 8 * MIB


def boot_part_mb(sizes: list[int]) -> int:
    """Compute the boot partition size.

    Args:
        sizes: Size in bytes of every file packed in the boot image.

    Returns:
        The partition size in whole MiB.

    Raises:
        ValueError: When no size is given or one is not positive.
    """
    if not sizes or any(s <= 0 for s in sizes):
        raise ValueError(f'need positive file sizes, got {sizes}')
    total = sum(sizes)
    needed = total + total * MARGIN_PERCENT // 100 + MARGIN_FLAT
    return -(-needed // MIB)


def main(argv: list[str] | None = None) -> int:
    """Print the partition size for the sizes given as arguments.

    Args:
        argv: Arguments, ``sys.argv[1:]`` when omitted.

    Returns:
        Process exit code: 0, or 2 on a bad argument.
    """
    args = sys.argv[1:] if argv is None else argv
    try:
        print(boot_part_mb([int(a) for a in args]))
    except ValueError as err:
        print(err, file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
