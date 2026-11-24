"""Wiring tests for the plan step of .github/workflows/full-pipeline.yml."""

import re
from pathlib import Path

WORKFLOW = Path(__file__).resolve(
).parent.parent / '.github' / 'workflows' / 'full-pipeline.yml'


def test_plan_owes_publication_of_an_unpublished_channel() -> None:
    """The push plan forces the debs stage when the channel has no repository.

    Without it a run that changes nothing skips the debs stage, the channel
    never gets its public key, and the APT key pin job fails on a 404.
    """
    text = WORKFLOW.read_text(encoding='utf-8')
    plan = text.split('owed_stages.py', 1)[1].split('printf \'env=%s', 1)[0]
    assert 'channel-published.sh' in plan
    after = plan.split('channel-published.sh', 1)[1]
    assert re.match(r'[^;]*; then\s+echo [^\n]+\s+debs=true', after)
