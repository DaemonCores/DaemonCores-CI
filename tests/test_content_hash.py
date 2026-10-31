"""Parity test for ``build-deb/content-hash.sh`` against the pre-split script.

The hash is the key of the published ``hashes.json``: if the extracted library
computed a different value, every package would rebuild once. The old
``build-deb.sh`` is read from git history and both paths hash the same fixture.
"""

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / 'build-deb' / 'content-hash.sh'
OLD_REV = '4a62cec'


def old_script() -> str:
    """Return the build-deb.sh text from before the extraction.

    Returns:
        The script source at ``OLD_REV``.
    """
    return subprocess.run(
        ['git', 'show', f'{OLD_REV}:build-deb/build-deb.sh'],
        cwd=ROOT, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def fixture_env(tmp_path: Path) -> dict[str, str]:
    """Build a source tree and the environment describing a package.

    Args:
        tmp_path: Pytest temporary directory.

    Returns:
        Environment variables for the hash functions.
    """
    src = tmp_path / 'src'
    (src / 'sub').mkdir(parents=True)
    (src / 'a.txt').write_text('alpha\n')
    (src / 'sub' / 'b.txt').write_text('beta\n')
    return {
        'PATH': '/usr/bin:/bin',
        'PACKAGE': 'demo',
        'ARCH': 'arm64',
        'SOURCES': str(src),
        'UPSTREAM': '',
        'BUILD': 'echo build',
        'EXTRA_ENV': 'VERSION=1.2\nGH_TOKEN=secret\nB=2',
        'EXTRA_ENV_CI': 'MODULE_SIGNING_ACTIVE=yes',
    }


def run_hash(prelude: str, env: dict[str, str]) -> str:
    """Source ``prelude`` and print ``content_hash``.

    Args:
        prelude: Shell text defining the functions.
        env: Environment for the shell.

    Returns:
        The hash printed on stdout.
    """
    script = (prelude + '\nif declare -F merge_extra_env >/dev/null; '
              'then merge_extra_env; else '
              'EXTRA_ENV="${EXTRA_ENV}${EXTRA_ENV:+$\'\\n\'}${EXTRA_ENV_CI}"; '
              'fi\ncontent_hash 2>/dev/null\n')
    return subprocess.run(['bash', '-c', script], env=env, check=True,
                          capture_output=True, text=True).stdout.strip()


def test_hash_matches_pre_split_script(fixture_env: dict[str, str]) -> None:
    """The extracted library yields the hash of the old script."""
    old = old_script().split('\nmain() {')[0]
    old = old.replace('set -euo pipefail', 'set -eu', 1)
    new = f'. {LIB}'
    expected = run_hash(old, fixture_env)
    assert len(expected) == 64
    assert run_hash(new, fixture_env) == expected


def test_secret_keys_do_not_feed_the_hash(fixture_env: dict[str, str]) -> None:
    """Rotating a token leaves the hash unchanged."""
    first = run_hash(f'. {LIB}', fixture_env)
    fixture_env['EXTRA_ENV'] = 'VERSION=1.2\nGH_TOKEN=other\nB=2'
    assert run_hash(f'. {LIB}', fixture_env) == first
