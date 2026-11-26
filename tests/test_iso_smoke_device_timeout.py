"""The TCG smoke must not give up on the serial console before udev is done.

``serial-getty@ttyAMA0`` waits for ``dev-ttyAMA0.device`` with the default 90 s
device timeout. Under TCG the udev coldplug of the live root takes longer: the
job fails with a dependency error and no login prompt ever shows, whatever the
smoke budget (run 45691559463, reproduced on QEMU 8.2.2).
"""

import re

from test_iso_smoke_cpu import smoke_script


def test_device_timeout_follows_the_smoke_budget() -> None:
    """The guest waits for its devices as long as the smoke waits for login."""
    script = smoke_script()
    kargs = re.search(r'^\s*KARGS="([^"]*)"', script, re.MULTILINE)
    assert kargs, 'smoke kernel arguments not found'
    assert 'systemd.default_device_timeout_sec=${BUDGET}' in kargs[1]
    assert script.index('BUDGET="${SMOKE_BUDGET_S') < kargs.start()
