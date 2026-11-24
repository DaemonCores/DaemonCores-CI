"""Tests for scripts/release-media.sh, run against a fake gh and curl."""

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / 'scripts' / 'release-media.sh'

FAKE_GH = """#!/bin/bash
echo "gh $*" >>"$FAKE_LOG"
case "$*" in
  *"--paginate repos/o/r/releases --jq"*) printf '%s' "$FAKE_DRAFT" ;;
  *"--paginate repos/o/r/releases/7/assets"*) printf '%s' "$FAKE_ASSET" ;;
  *"-X POST repos/o/r/releases -f"*) echo 42 ;;
  *generate-notes*) echo "generated notes" ;;
esac
"""

FAKE_CURL = """#!/bin/bash
echo "curl $*" >>"$FAKE_LOG"
"""


def _run(tmp_path: Path,
         *args: str,
         draft: str = '',
         asset: str = '') -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run the script with fake gh and curl first in PATH.

    Args:
        tmp_path: Scratch directory.
        *args: Arguments of the script.
        draft: Id the fake API returns for the pending draft.
        asset: Id the fake API returns for an existing asset.

    Returns:
        The completed process and the commands the fakes recorded.
    """
    for name, body in (('gh', FAKE_GH), ('curl', FAKE_CURL)):
        fake = tmp_path / name
        _ = fake.write_text(body, encoding='utf-8')
        fake.chmod(0o755)
    log = tmp_path / 'log'
    log.touch()
    env = {
        **os.environ, 'PATH': f'{tmp_path}:{os.environ["PATH"]}',
        'GITHUB_REPOSITORY': 'o/r',
        'GH_TOKEN': 't',
        'FAKE_LOG': str(log),
        'FAKE_DRAFT': draft,
        'FAKE_ASSET': asset
    }
    done = subprocess.run(['bash', str(SCRIPT), *args],
                          capture_output=True,
                          text=True,
                          env=env,
                          check=False)
    return done, log.read_text(encoding='utf-8').splitlines()


def test_draft_is_created_once_then_reused(tmp_path: Path) -> None:
    """A commit gets one draft; a second call returns the same one.

    Args:
        tmp_path: Scratch directory.
    """
    made, calls = _run(tmp_path, 'draft', 'abc')
    assert made.stdout == '42\n'
    assert any('tag_name=pending-abc' in c and 'draft=true' in c for c in calls)
    (tmp_path / 'again').mkdir()
    found, calls = _run(tmp_path / 'again', 'draft', 'abc', draft='7\n')
    assert found.stdout == '7\n'
    assert not any('-X POST' in c for c in calls)


def test_upload_replaces_a_stale_asset(tmp_path: Path) -> None:
    """An asset of the same name is deleted before the new upload.

    Args:
        tmp_path: Scratch directory.
    """
    done, calls = _run(tmp_path, 'upload', '7', '/x/a.iso', asset='99\n')
    assert done.returncode == 0, done.stderr
    assert any('-X DELETE repos/o/r/releases/assets/99' in c for c in calls)
    assert any('releases/7/assets?name=a.iso' in c for c in calls)


def test_publish_promotes_the_draft(tmp_path: Path) -> None:
    """With media built, the draft becomes the version release.

    Args:
        tmp_path: Scratch directory.
    """
    _, calls = _run(tmp_path, 'publish', 'abc', 'v1.0.0', 'stable', draft='7\n')
    patch = [c for c in calls if '-X PATCH repos/o/r/releases/7' in c]
    assert patch
    assert 'tag_name=v1.0.0' in patch[0]
    assert 'draft=false' in patch[0]
    assert 'make_latest=true' in patch[0]


def test_publish_creates_the_release_without_media(tmp_path: Path) -> None:
    """With no draft the release is created as a pre-release on edge.

    Args:
        tmp_path: Scratch directory.
    """
    _, calls = _run(tmp_path, 'publish', 'abc', 'v1.0.0-edge.3', 'edge')
    create = [c for c in calls if '-X POST repos/o/r/releases -f' in c]
    assert create
    assert 'prerelease=true' in create[0]


def test_publish_refuses_an_unknown_channel(tmp_path: Path) -> None:
    """Only edge and stable publish.

    Args:
        tmp_path: Scratch directory.
    """
    done, _ = _run(tmp_path, 'publish', 'abc', 'v1', 'security')
    assert done.returncode != 0
    assert 'Unknown channel' in done.stderr
