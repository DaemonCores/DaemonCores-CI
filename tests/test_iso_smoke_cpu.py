"""The arm64 ISO smoke must not emulate ``-cpu max``.

On the runner's QEMU 8.2.2 (Ubuntu 24.04) ``-cpu max`` advertises FEAT_MOPS and
TCG mis-executes the kernel's MOPS memset: ``free_initmem`` poisons memory past
``__init_end`` (including sched_clock's ``cd``), the next ``printk`` jumps to
``0xcccccccccccccccc`` and the guest goes silent after ``PM: genpd: Disabling
unused power domains`` (run 45163009666; ``arm64.nomops`` flips it to pass).
A smoke that reports a kernel stall for an emulator defect proves nothing about
the ISO, so the arm64 CPU model is pinned to a real one.
"""

import re
from pathlib import Path
from typing import Any, cast

import yaml

WORKFLOW = Path(
    __file__
).resolve().parents[1] / '.github' / 'workflows' / 'iso-builder.yml'


def _smoke_script() -> str:
    """Return the run block of the ISO boot smoke step.

    Returns:
        The shell text of ``Boot smoke test the ISO (QEMU)``.
    """
    doc = cast(dict[str, Any], yaml.safe_load(WORKFLOW.read_text()))
    for job in doc['jobs'].values():
        for step in job.get('steps', []):
            if step.get('name') == 'Boot smoke test the ISO (QEMU)':
                return cast(str, step['run'])
    raise AssertionError('smoke step not found')


def test_arm64_smoke_does_not_use_cpu_max() -> None:
    """The aarch64 QEMU command names a concrete CPU model, never ``max``."""
    script = _smoke_script()
    match = re.search(r'qemu-system-aarch64[^\n]*?-cpu\s+(\S+)', script)
    assert match, 'aarch64 QEMU command without an explicit -cpu'
    assert match.group(1) != 'max'
