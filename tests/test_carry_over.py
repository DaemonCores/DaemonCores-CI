"""Tests for bootc-debs-end/carry-over.sh."""
import json
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__
              ).resolve().parent.parent / 'bootc-debs-end' / 'carry-over.sh'


def _site(root: Path) -> Path:
    """Create a published site with two packages.

    Args:
        root: Scratch directory.

    Returns:
        The site directory.
    """
    cache = root / 'site' / 'debs-cache'
    cache.mkdir(parents=True)
    (cache / 'a_1_amd64.deb').write_bytes(b'a')
    (cache / 'b_1_amd64.deb').write_bytes(b'b')
    (cache / 'hashes.json').write_text(
        json.dumps({
            'a-amd64': {
                'hash': 'ha',
                'debs': ['a_1_amd64.deb']
            },
            'b-amd64': {
                'hash': 'hb',
                'debs': ['b_1_amd64.deb']
            },
            'c-amd64': {
                'hash': 'hc',
                'debs': ['missing.deb']
            },
        }),
        encoding='utf-8'
    )
    return root / 'site'


def test_missing_packages_are_kept(tmp_path: Path) -> None:
    """A package absent from the artifact is restored, a present one is not.

    Args:
        tmp_path: Scratch directory.
    """
    site = _site(tmp_path)
    debs = tmp_path / 'debs'
    (debs / '.meta').mkdir(parents=True)
    (debs / '.meta' /
     'a-amd64.json').write_text('{"fresh": true}', encoding='utf-8')
    env = dict(os.environ, DEBS_DIR=str(debs), PAGES_URL=f'file://{site}')
    done = subprocess.run(['bash', str(SCRIPT)],
                          env=env,
                          capture_output=True,
                          text=True,
                          check=False)
    assert done.returncode == 0, done.stderr
    assert (debs / 'b_1_amd64.deb').read_bytes() == b'b'
    meta = json.loads(
        (debs / '.meta' / 'b-amd64.json').read_text(encoding='utf-8')
    )
    assert meta == {'hash': 'hb', 'skipped': True, 'debs': ['b_1_amd64.deb']}
    assert json.loads(
        (debs / '.meta' / 'a-amd64.json').read_text(encoding='utf-8')
    ) == {
        'fresh': True
    }
    assert not (debs / '.meta' / 'c-amd64.json').exists()
    assert 'could not restore' in done.stdout


def test_no_published_site_is_not_an_error(tmp_path: Path) -> None:
    """Without a published hashes.json nothing is kept and the script succeeds.

    Args:
        tmp_path: Scratch directory.
    """
    env = dict(
        os.environ,
        DEBS_DIR=str(tmp_path / 'debs'),
        PAGES_URL=f'file://{tmp_path}/nothing'
    )
    done = subprocess.run(['bash', str(SCRIPT)],
                          env=env,
                          capture_output=True,
                          text=True,
                          check=False)
    assert done.returncode == 0, done.stderr
