"""Structure of the single-job deb builder workflow."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / '.github' / 'workflows'


def _builder() -> dict:
    """Return the parsed bootc-debs-builder workflow."""
    return yaml.safe_load(
        (WORKFLOWS / 'bootc-debs-builder.yml').read_text(encoding='utf-8'))


def test_one_build_job_and_publish() -> None:
    """No wave or per-package job remains; publish waits for the build."""
    jobs = _builder()['jobs']
    assert 'build' in jobs and 'publish' in jobs
    assert not [name for name in jobs if re.fullmatch(r'wave\d+|setup', name)]
    assert not [job for job in jobs.values() if 'uses' in job]
    assert jobs['publish']['needs'] == ['build']
    assert not (WORKFLOWS / 'build-one-deb.yml').exists()


def test_build_job_runs_in_buildenv_and_uploads_one_artifact() -> None:
    """The build job keeps the container, the secrets and a single upload."""
    job = _builder()['jobs']['build']
    assert job['container']['credentials']['username']
    assert {'SECRETVAL_GITHUB_TOKEN', 'MODULE_SIGNING_KEY',
            'MODULE_SIGNING_CERT', 'BUILD_ARCH'} <= set(job['env'])
    uploads = [s for s in job['steps']
               if str(s.get('uses', '')).startswith('actions/upload-artifact')]
    assert len(uploads) == 1
    assert uploads[0]['if'] == 'always()'
    assert uploads[0]['with']['name'] == 'debs-all-${{ inputs.arch }}'
    assert uploads[0]['with']['include-hidden-files'] is True
    assert sum('manifest-expand' in str(s.get('uses', ''))
               for s in job['steps']) == 1


def test_no_reference_to_removed_workflow() -> None:
    """Nothing in the workflows or actions points at build-one-deb."""
    for path in list(WORKFLOWS.glob('*.yml')) + list(ROOT.glob('*/action.yml')):
        assert 'build-one-deb' not in path.read_text(encoding='utf-8'), path
