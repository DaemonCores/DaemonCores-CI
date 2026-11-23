"""classify_boot_log.py - state of a live ISO boot from its serial console log.

Usage:
    python3 classify_boot_log.py [--stage] LOG

Prints one word on stdout, the state of the boot seen in LOG (a missing file
counts as an empty log):

``emergency``  the initramfs dropped to a dracut emergency shell,
``bootloop``   the kernel banner appeared more than once: the guest rebooted,
``up``         the login prompt of the live root is on the console,
``booting``    nothing conclusive yet (the caller decides on a timeout).

With ``--stage`` it prints where a stalled boot stopped instead, which is what
tells a kernel-side stall (the initramfs never handed over to ``/init``) from
a userspace stall (the initramfs started but never reached the live root):

``kernel``     the kernel never ran ``/init``: no ``Freeing unused kernel
               memory``/``Run /init as init process`` in the log,
``userspace``  the kernel handed over to ``/init``.

The ISO smoke test of ``iso-builder.yml`` polls this while QEMU runs. The
checks are ordered by gravity: a failure marker wins over a login prompt seen
earlier in the same log, and only the login prompt of the *live root* proves
success. The initrd targets also print ``Reached target``, which is why they
are not a marker: a green smoke once published an ISO that looped between the
kernel and the initramfs without ever reaching the live root.
"""

import re
import sys
from pathlib import Path

EMERGENCY = re.compile(
    r'emergency mode|dracut-emergency|Failed to start initrd-switch-root'
)
KERNEL_BANNER = 'Linux version '
LOGIN = re.compile(
    r'^[^ \n]+ login:|Debian GNU/Linux .*\(ttyS0\)', re.MULTILINE
)
HANDOVER = ('Freeing unused kernel memory', 'Run /init as init process')


def classify(log: str) -> str:
    """Classify the boot seen in a serial console log.

    Args:
        log: The serial console text captured so far.

    Returns:
        ``emergency``, ``bootloop``, ``up`` or ``booting``.
    """
    if EMERGENCY.search(log):
        return 'emergency'
    if log.count(KERNEL_BANNER) > 1:
        return 'bootloop'
    if LOGIN.search(log):
        return 'up'
    return 'booting'


def stage(log: str) -> str:
    """Say which side a stalled boot stopped on.

    Args:
        log: The serial console text captured so far.

    Returns:
        ``userspace`` once the kernel ran ``/init`` (or a marker of userspace
        was seen), ``kernel`` while the kernel has not handed over yet.
    """
    if EMERGENCY.search(log) or LOGIN.search(log):
        return 'userspace'
    if any(marker in log for marker in HANDOVER):
        return 'userspace'
    return 'kernel'


def main(argv: list[str] | None = None) -> int:
    """Print the state, or the stage, of the boot logged in the file given.

    Args:
        argv: Arguments, ``sys.argv[1:]`` when omitted.

    Returns:
        Process exit code: 0, or 2 on a bad command line.
    """
    args = sys.argv[1:] if argv is None else argv
    want_stage = bool(args) and args[0] == '--stage'
    if want_stage:
        args = args[1:]
    if len(args) != 1:
        print('usage: classify_boot_log.py [--stage] LOG', file=sys.stderr)
        return 2
    path = Path(args[0])
    log = path.read_text(errors='replace') if path.exists() else ''
    print(stage(log) if want_stage else classify(log))
    return 0


if __name__ == '__main__':
    sys.exit(main())
