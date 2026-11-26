"""Consistency tests for .github/workflows/security-channel.yml."""

import re
from pathlib import Path

from typed_docs import as_list, as_mapping, as_str, as_str_mapping, load_yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / '.github' / 'workflows' / 'security-channel.yml'


def _job(name: str) -> dict[str, object]:
    """Load one job of the workflow.

    Args:
        name: Job id.

    Returns:
        The job definition.
    """
    doc = as_mapping(load_yaml(WORKFLOW.read_text()))
    return as_str_mapping(as_mapping(doc['jobs'])[name])


def test_referenced_scripts_exist() -> None:
    """Every shared script the workflow runs is present in the repository."""
    pattern = re.compile(r'\.ci-tooling/(scripts/[\w./-]+)')
    refs = [str(m[1]) for m in pattern.finditer(WORKFLOW.read_text())]
    assert refs
    assert not [ref for ref in refs if not (ROOT / ref).is_file()]


def test_promotion_waits_for_boot_and_rebuild() -> None:
    """A candidate is promoted only after it was built and booted."""
    needs = {as_str(need) for need in as_list(_job('promote')['needs'])}
    assert {'detect', 'rebuild', 'smoke'} <= needs


def _archs(name: str) -> set[str]:
    """Architectures of a job matrix.

    Args:
        name: Job id.

    Returns:
        The values of the `arch` key of the matrix entries.
    """
    strategy = as_str_mapping(_job(name)['strategy'])
    matrix = as_str_mapping(strategy['matrix'])
    return {
        as_str(as_str_mapping(entry)['arch'])
        for entry in as_list(matrix['include'])
    }


def test_smoke_reads_the_manifest_of_the_product() -> None:
    """The harness gets the caller's manifest, present in the workspace.

    `image-test.sh prepare` exits with "TESTS_FILE is not set" otherwise, and
    the tooling checkout must come after the product one, which cleans the
    workspace root.
    """
    job = _job('smoke')
    assert as_str_mapping(job['env'])['TESTS_FILE'] == 'workflows/tests.yml'
    checkouts = [
        as_str_mapping(step.get('with', {}))
        for step in map(as_str_mapping, as_list(job['steps']))
        if as_str(step.get('uses', '')).startswith('actions/checkout@')
    ]
    assert [with_.get('path') for with_ in checkouts] == [None, '.ci-tooling']
    assert 'repository' not in checkouts[0]


def test_smoke_boots_every_built_arch() -> None:
    """Each architecture the rebuild produces is booted before promotion."""
    assert _archs('smoke') == _archs('rebuild')


def test_promote_can_publish_release() -> None:
    """The changelog release needs the contents write permission."""
    perms = as_str_mapping(_job('promote')['permissions'])
    assert perms['contents'] == 'write'


def test_detection_goes_through_the_adoption_pipeline() -> None:
    """Candidates are adopted by the gates and the care delay, ledger cached."""
    text = WORKFLOW.read_text()
    assert 'security-adopt.sh' in text
    assert 'care_hours' in text
    steps = [as_str_mapping(step) for step in as_list(_job('detect')['steps'])]
    caches = [
        step for step in steps
        if as_str(step.get('uses', '')).startswith('actions/cache@')
    ]
    assert len(caches) == 1
    cache_with = as_str_mapping(caches[0]['with'])
    assert as_str(cache_with['restore-keys']).startswith('security-care-')
    perms = as_str_mapping(_job('detect')['permissions'])
    assert perms['issues'] == 'write'
