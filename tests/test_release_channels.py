"""Tests of the git-flow release channels of the shared release workflow.

A workflow cannot be run outside GitHub, so the stable guard is checked on the
text of the publish step.
"""

import re
from pathlib import Path

RELEASE = Path(__file__
               ).resolve().parents[1] / '.github' / 'workflows' / 'release.yml'


def stable_guard() -> str:
    """Return the shell ``case`` that guards the stable channel.

    Returns:
        The text between the opening and closing of the event guard.
    """
    text = RELEASE.read_text(encoding='utf-8')
    found = re.search(
        r'stable\)\n(.*?)tag="\$\{PREFIX\}\$\{NEXT\}"', text, re.S
    )
    assert found, 'stable branch of the publish step not found'
    return found.group(1)


def test_stable_accepts_a_green_pipeline_run() -> None:
    """A stable release follows a green pipeline run of the stable branch."""
    assert 'workflow_run' in stable_guard()


def test_stable_accepts_a_manual_trigger() -> None:
    """The manual trigger stays available for a stable release."""
    assert 'workflow_dispatch' in stable_guard()


def test_stable_refuses_a_push_or_a_timer() -> None:
    """A push or a timer never releases stable: any other event is refused."""
    assert re.search(r'\*\)\n\s*echo "::error::', stable_guard())
