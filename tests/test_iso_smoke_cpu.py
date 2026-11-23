"""The arm64 ISO smoke must not emulate ``-cpu max``.

On the runner's QEMU 8.2.2 (Ubuntu 24.04) ``-cpu max`` advertises FEAT_MOPS and
TCG mis-executes the kernel's MOPS memset: ``free_initmem`` poisons memory past
``__init_end`` (including sched_clock's ``cd``), the next ``printk`` jumps to
``0xcccccccccccccccc`` and the guest goes silent after ``PM: genpd: Disabling
unused power domains`` (run 45163009666; ``arm64.nomops`` flips it to pass). A
smoke that reports a kernel stall for an emulator defect proves nothing about
the ISO, so the arm64 CPU model is pinned to a real one.
"""

import re
from pathlib import Path

from typed_docs import as_mapping, as_str, iter_steps, load_yaml

WORKFLOW = Path(__file__).resolve(
).parents[1] / '.github' / 'workflows' / 'iso-builder.yml'


def _smoke_script() -> str:
    """Return the run block of the ISO boot smoke step.

    Returns:
        The shell text of ``Boot smoke test the ISO (QEMU)``.

    Raises:
        AssertionError: If the smoke step is not found in the workflow.
    """
    workflow = as_mapping(load_yaml(WORKFLOW.read_text()))
    for step in iter_steps(workflow):
        if step.get('name') == 'Boot smoke test the ISO (QEMU)':
            return as_str(step['run'])
    raise AssertionError('smoke step not found')


def test_arm64_smoke_does_not_use_cpu_max() -> None:
    """The aarch64 QEMU command names a concrete CPU model, never ``max``."""
    script = _smoke_script()
    match = re.search(r'qemu-system-aarch64[^\n]*?-cpu\s+(\S+)', script)
    assert match, 'aarch64 QEMU command without an explicit -cpu'
    assert match.group(1) != 'max'


def test_arm64_smoke_budget_exceeds_slow_tcg_boot() -> None:
    """Arm64 gets a larger login budget than the 900 s of the other arches.

    The live root reaches the login prompt in ~380 s on a fast host and ~2x that
    on the shared runner (DEBUG.md 31): a flat 900 s reports a slow boot as a
    stall. The login marker stays mandatory, only the time grows.
    """
    script = _smoke_script()
    match = re.search(r'arm64\)\s+BUDGET="\$\{SMOKE_BUDGET_S:-(\d+)\}"', script)
    assert match, 'no per-arch smoke budget for arm64'
    assert int(match.group(1)) >= 1800
    assert 'seq "${BUDGET}"' in script
    assert 'seq 900' not in script
