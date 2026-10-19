"""Unit tests for ``scripts/rotate-apt-key.sh``.

A stub ``gh`` records what it receives on stdin, so the tests can assert that
the private key reaches ``gh secret set`` and nowhere else.
"""

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'rotate-apt-key.sh'
GH_STUB = """#!/bin/bash
echo "$*" >"$GH_ARGS"
cat >"$GH_STDIN"
"""


@pytest.fixture(name='env')
def fixture_env(tmp_path: Path) -> dict[str, str]:
    """Build an environment with a stub gh first on PATH.

    Args:
        tmp_path: Pytest scratch directory.

    Returns:
        The environment mapping for the script under test.
    """
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    gh = bindir / 'gh'
    gh.write_text(GH_STUB)
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    return {
        **os.environ,
        'PATH': f'{bindir}:{os.environ["PATH"]}',
        'GH_ARGS': str(tmp_path / 'gh.args'),
        'GH_STDIN': str(tmp_path / 'gh.stdin'),
    }


def _run(args: list[str], env: dict[str,
                                    str]) -> subprocess.CompletedProcess[str]:
    """Run ``rotate-apt-key.sh``.

    Args:
        args: Command-line arguments for the script.
        env: Environment for the script.

    Returns:
        The completed process.
    """
    return subprocess.run([str(SCRIPT), *args],
                          check=False,
                          capture_output=True,
                          text=True,
                          env=env,
                          timeout=120)


def test_rotate_sends_private_key_only_to_gh(env: dict[str, str]) -> None:
    """The private key goes to `gh secret set`, never to stdout/stderr.

    Args:
        env: Stub environment fixture.
    """
    result = _run(['rotate'], env)
    assert result.returncode == 0, result.stderr
    assert Path(env['GH_ARGS']).read_text().strip() == 'secret set APT_GPG_KEY'
    secret = Path(env['GH_STDIN']).read_text()
    assert 'BEGIN PGP PRIVATE KEY BLOCK' in secret
    body = ''.join(secret.splitlines()[3:-2])
    assert body
    assert body not in result.stdout + result.stderr
    assert re.fullmatch(r'[0-9A-F]{40}', result.stdout.strip())
    assert 'PRIVATE' not in result.stdout + result.stderr


def test_rotate_targets_the_repository_when_known(env: dict[str, str]) -> None:
    """GITHUB_REPOSITORY is forwarded to gh as --repo.

    Args:
        env: Stub environment fixture.
    """
    env['GITHUB_REPOSITORY'] = 'owner/repo'
    assert _run(['rotate'], env).returncode == 0
    assert Path(
        env['GH_ARGS']
    ).read_text().strip() == ('secret set APT_GPG_KEY --repo owner/repo')


def test_unknown_subcommand_is_refused(env: dict[str, str]) -> None:
    """Only `rotate` exists; the hash pin is derived at build time now.

    Args:
        env: Stub environment fixture.
    """
    assert _run(['publish-hash', 'A' * 40], env).returncode != 0
