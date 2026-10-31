"""Tests of the boot phase when one tag has several manifest categories.

The image-test harness is a stub: ``categories`` prints the categories the
tag is tested under, ``test`` logs the category it was asked to boot and fails
for the one named in ``FAIL_CATEGORY``. What is covered is the orchestration
of ``scripts/image-ci/build-arch.sh``: one boot per category, one failing boot
failing the tag, the failing category named in the detail.
"""

from pathlib import Path

import pytest
from test_image_ci import Sandbox, _script, entry, needs_jq

IMAGE_TEST = r"""#!/bin/bash
echo "image-test $* CATEGORY=${CATEGORY:-}" >>"${STUB_LOG}"
case "$1" in
  categories) printf '%s\n' ${CATEGORIES:-} ;;
  test)
    mkdir -p "${IMAGE_TEST_STATE}/logs"
    echo "boot of ${CATEGORY:-default}" >"${IMAGE_TEST_STATE}/logs/serial.${CATEGORY:-default}.log"
    [ -z "${FAIL_CATEGORY:-}" ] || [ "${CATEGORY:-}" != "${FAIL_CATEGORY}" ]
    ;;
esac
"""


@pytest.fixture
def sb(tmp_path: Path) -> Sandbox:
    """Provide a sandbox whose harness stub knows categories.

    Args:
        tmp_path: Scratch directory.

    Returns:
        The sandbox.
    """
    box = Sandbox(tmp_path)
    _script(box.image_test, IMAGE_TEST)
    return box


def boots(sb: Sandbox) -> list[str]:
    """Return the categories the harness was asked to boot, in order.

    Args:
        sb: The sandbox.

    Returns:
        One category per ``test`` call.
    """
    return [ln.rsplit('CATEGORY=', 1)[1] for ln in sb.calls('image-test test')]


@needs_jq
def test_one_boot_per_matching_category(sb: Sandbox) -> None:
    """A tag with two categories is installed and booted twice."""
    done = sb.run([entry('latest')], CATEGORIES='plain sigenforce')
    assert done.returncode == 0, done.stdout + done.stderr
    assert boots(sb) == ['plain', 'sigenforce']
    assert sb.states() == {'latest': 'ok'}


@needs_jq
def test_one_failing_category_fails_the_tag(sb: Sandbox) -> None:
    """Every category still boots; the tag fails and names the category."""
    done = sb.run([entry('latest')],
                  CATEGORIES='plain sigenforce',
                  FAIL_CATEGORY='plain')
    assert done.returncode == 1
    assert boots(sb) == ['plain', 'sigenforce']
    assert sb.states() == {'latest': 'test-failed'}
    assert 'plain: exit 1' in sb.row('latest')[8]


@needs_jq
@pytest.mark.parametrize('categories', ['', 'plain'])
def test_zero_or_one_category_keeps_the_single_boot(
    sb: Sandbox, categories: str
) -> None:
    """The harness picks the category itself: no CATEGORY is forced."""
    done = sb.run([entry('latest')], CATEGORIES=categories)
    assert done.returncode == 0, done.stdout + done.stderr
    assert boots(sb) == ['']
