"""Unit tests for ``next-version/next-version.sh`` on throwaway git repos."""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__
              ).resolve().parents[1] / 'next-version' / 'next-version.sh'
GIT_ENV = {
    'GIT_AUTHOR_NAME': 't',
    'GIT_AUTHOR_EMAIL': 't@t',
    'GIT_COMMITTER_NAME': 't',
    'GIT_COMMITTER_EMAIL': 't@t',
    # Ignore the host git configuration (signing, forced annotated tags).
    'GIT_CONFIG_GLOBAL': os.devnull,
    'GIT_CONFIG_SYSTEM': os.devnull,
}


def _git(repo: Path, *args: str) -> None:
    """Run a git command in a repository and assert it succeeds.

    Args:
        repo: Repository directory.
        *args: Arguments passed to git.
    """
    done = subprocess.run(['git', '-C', str(repo), *args],
                          check=False,
                          capture_output=True,
                          text=True,
                          env={
                              **os.environ,
                              **GIT_ENV
                          })
    assert done.returncode == 0, done.stderr


def _commit(repo: Path, message: str) -> None:
    """Create an empty commit.

    Args:
        repo: Repository directory.
        message: Commit message.
    """
    _git(repo, 'commit', '--allow-empty', '-q', '-m', message)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Initialise a throwaway git repository.

    Args:
        tmp_path: Pytest scratch directory.

    Returns:
        The repository directory.
    """
    _git(tmp_path, 'init', '-q', '-b', 'main')
    return tmp_path


def _run(repo: Path) -> dict[str, str]:
    """Run ``next-version.sh`` and parse its ``key=value`` lines.

    Args:
        repo: Repository directory used as the working directory.

    Returns:
        The printed outputs by name.
    """
    env = {k: v for k, v in os.environ.items() if k != 'GITHUB_OUTPUT'}
    out = subprocess.run(['bash', str(SCRIPT)],
                         cwd=repo,
                         check=True,
                         capture_output=True,
                         text=True,
                         env=env).stdout
    return dict(line.split('=', 1) for line in out.splitlines())


def test_no_tag_feat_gives_first_minor(repo: Path) -> None:
    """A feature with no tag yields the first minor version.

    Args:
        repo: Throwaway repository fixture.
    """
    _commit(repo, 'feat: first')
    assert _run(repo) == {
        'last': '0.0.0',
        'bump': 'minor',
        'next': '0.1.0',
        'edge': '0.1.0-edge.1'
    }


def test_fix_after_tag_is_patch(repo: Path) -> None:
    """A fix after a tag is a patch bump.

    Args:
        repo: Throwaway repository fixture.
    """
    _commit(repo, 'feat: base')
    _git(repo, 'tag', 'v1.2.3')
    _commit(repo, 'fix(core): bug')
    _commit(repo, 'docs: words')
    out = _run(repo)
    assert (out['bump'], out['next'],
            out['edge']) == ('patch', '1.2.4', '1.2.4-edge.2')


def test_breaking_marker_and_footer_are_major(repo: Path) -> None:
    """A breaking marker or footer is a major bump.

    Args:
        repo: Throwaway repository fixture.
    """
    _commit(repo, 'feat: base')
    _git(repo, 'tag', 'v1.2.3')
    _commit(repo, 'feat!: reshape')
    assert _run(repo)['next'] == '2.0.0'
    _git(repo, 'reset', '-q', '--hard', 'v1.2.3')
    _commit(repo, 'fix: x\n\nBREAKING CHANGE: gone')
    assert _run(repo)['next'] == '2.0.0'


def test_only_chores_means_none(repo: Path) -> None:
    """Only chores and refactors mean no bump.

    Args:
        repo: Throwaway repository fixture.
    """
    _commit(repo, 'feat: base')
    _git(repo, 'tag', 'v1.0.0')
    _commit(repo, 'chore: tidy')
    _commit(repo, 'refactor: move')
    out = _run(repo)
    assert (out['bump'], out['next']) == ('none', '1.0.0')


def test_prerelease_tags_are_ignored(repo: Path) -> None:
    """Pre-release tags do not count as the last release.

    Args:
        repo: Throwaway repository fixture.
    """
    _commit(repo, 'feat: base')
    _git(repo, 'tag', 'v1.0.0')
    _commit(repo, 'feat: more')
    _git(repo, 'tag', 'v1.1.0-edge.1')
    _commit(repo, 'fix: bug')
    out = _run(repo)
    assert (out['last'], out['next'],
            out['edge']) == ('1.0.0', '1.1.0', '1.1.0-edge.2')
