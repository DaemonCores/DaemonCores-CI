"""Unit tests for ``upstream-track/track.sh``.

The GitHub API is replaced by a directory tree served through ``file://``: a
release is the JSON file ``repos/<owner>/<repo>/releases/latest``.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'upstream-track' / 'track.sh'
DIGEST = 'a' * 64
MANIFEST = """packages:
  - name: tool
    track:
      TOOL_VER:
        repo: acme/tool
        digests:
          TOOL_SHA256: "tool-{version}.tar.xz"
  - name: lib
    track:
      LIB_VER:
        repo: acme/lib
        strip-v: true
  - name: rolling
    track:
      ROLL_COMMIT:
        repo: acme/rolling
        head: true
  - name: plain
"""


def _release(root: Path, repo: str, tag: str, assets: dict[str, str]) -> None:
    """Write the ``releases/latest`` document of ``repo``.

    Args:
        root: API tree root.
        repo: Repository as ``owner/name``.
        tag: Release tag name.
        assets: Asset digests by asset name.
    """
    path = root / 'repos' / repo / 'releases'
    path.mkdir(parents=True, exist_ok=True)
    body = {
        'tag_name':
        tag,
        'assets': [{
            'name': name,
            'digest': digest
        } for name, digest in assets.items()]
    }
    (path / 'latest').write_text(json.dumps(body))


@pytest.fixture(name='api')
def fixture_api(tmp_path: Path) -> Path:
    """Build an API tree with a manifest and one release per repository.

    Args:
        tmp_path: Pytest scratch directory.

    Returns:
        The API tree root.
    """
    (tmp_path / 'packages.yml').write_text(MANIFEST)
    _release(
        tmp_path, 'acme/tool', 'v2.0', {'tool-2.0.tar.xz': f'sha256:{DIGEST}'}
    )
    _release(tmp_path, 'acme/lib', 'v2026.4', {})
    return tmp_path


def _run(api: Path, mode: str,
         **extra: str) -> subprocess.CompletedProcess[str]:
    """Run the script in ``mode`` against the file:// API tree.

    Args:
        api: API tree root.
        mode: Script mode, ``resolve`` or ``check``.
        **extra: Additional environment variables.

    Returns:
        The completed process.
    """
    env = {
        **os.environ, 'MAN': str(api / 'packages.yml'),
        'GITHUB_API_URL': f'file://{api}',
        'GITHUB_OUTPUT': str(api / 'out'),
        **extra
    }
    return subprocess.run(['bash', str(SCRIPT), mode],
                          check=False,
                          capture_output=True,
                          text=True,
                          env=env,
                          timeout=60)


def _env_output(api: Path) -> list[str]:
    """Return the lines of the ``env`` step output.

    Args:
        api: API tree root holding the output file.

    Returns:
        The lines between the header and the end marker.
    """
    text = (api / 'out').read_text().splitlines()
    return text[1:text.index('TRACK_ENV_EOF')]


def test_resolve_sets_version_and_digest(api: Path) -> None:
    """The latest tag and the asset digest replace the base values.

    Args:
        api: API tree fixture.
    """
    result = _run(api, 'resolve', PKG='tool', BASE_ENV='CARGO_HOME=/x')
    assert result.returncode == 0, result.stderr
    assert _env_output(api) == [
        'CARGO_HOME=/x', 'TOOL_VER=v2.0', f'TOOL_SHA256={DIGEST}'
    ]


def test_resolve_strips_leading_v_on_request(api: Path) -> None:
    """``strip-v`` publishes the tag without its ``v``.

    Args:
        api: API tree fixture.
    """
    result = _run(api, 'resolve', PKG='lib', BASE_ENV='')
    assert result.returncode == 0, result.stderr
    assert _env_output(api) == ['LIB_VER=2026.4']


def test_resolve_replaces_a_base_value(api: Path) -> None:
    """A key already in the base env is replaced, not duplicated.

    Args:
        api: API tree fixture.
    """
    _run(api, 'resolve', PKG='lib', BASE_ENV='LIB_VER=old\nOTHER=1')
    assert _env_output(api) == ['OTHER=1', 'LIB_VER=2026.4']


def test_resolve_leaves_unresolved_entry_unset(api: Path) -> None:
    """A missing digest leaves the whole entry unset and warns.

    Args:
        api: API tree fixture.
    """
    _release(api, 'acme/tool', 'v2.0', {})
    result = _run(api, 'resolve', PKG='tool', BASE_ENV='A=1')
    assert result.returncode == 0
    assert _env_output(api) == ['A=1']
    assert '::warning::' in result.stderr


def test_resolve_strict_fails_when_unresolved(api: Path) -> None:
    """``STRICT=true`` turns an unresolved entry into a failure.

    Args:
        api: API tree fixture.
    """
    (api / 'repos' / 'acme' / 'lib' / 'releases' / 'latest').unlink()
    result = _run(api, 'resolve', PKG='lib', BASE_ENV='', STRICT='true')
    assert result.returncode == 1


def _published(api: Path, debs: dict[str, list[str]]) -> str:
    """Write a hashes.json and return its base URL.

    Args:
        api: API tree root.
        debs: Published deb file names by package key.

    Returns:
        The base URL of the published pages.
    """
    cache = api / 'pages' / 'debs-cache'
    cache.mkdir(parents=True)
    (cache / 'hashes.json').write_text(
        json.dumps({
            k: {
                'hash': 'h',
                'debs': v
            }
            for k, v in debs.items()
        })
    )
    return f'file://{api}/pages'


def test_check_reports_unpublished_release(api: Path) -> None:
    """A latest release missing from the published debs is behind.

    Args:
        api: API tree fixture.
    """
    url = _published(
        api, {
            'tool-amd64': ['tool_1.0_amd64.deb'],
            'lib-amd64': ['lib_2026.4_amd64.deb']
        }
    )
    result = _run(api, 'check', PAGES_URL=url, STRICT='true')
    assert result.returncode == 1
    assert 'tool TOOL_VER: upstream v2.0 is not published' in result.stdout
    assert 'lib LIB_VER: v2026.4 is published' in result.stdout


def test_check_passes_when_current(api: Path) -> None:
    """Every tracked release published means a clean strict check.

    Args:
        api: API tree fixture.
    """
    url = _published(
        api, {
            'tool-amd64': ['tool_2.0_amd64.deb'],
            'lib-amd64': ['lib_2026.4_amd64.deb']
        }
    )
    assert _run(api, 'check', PAGES_URL=url, STRICT='true').returncode == 0


def test_resolve_follows_branch_head(api: Path) -> None:
    """A ``head`` entry receives the head commit of the default branch.

    Args:
        api: API tree fixture.
    """
    path = api / 'repos' / 'acme' / 'rolling' / 'commits'
    path.mkdir(parents=True)
    (path / 'HEAD').write_text(json.dumps({'sha': 'c' * 40}))
    result = _run(api, 'resolve', PKG='rolling', BASE_ENV='A=1')
    assert result.returncode == 0, result.stderr
    assert _env_output(api) == ['A=1', f'ROLL_COMMIT={"c" * 40}']


KERNEL_MANIFEST = """packages:
  - name: tool
    track:
      TOOL_VER:
        repo: acme/tool
        digests:
          TOOL_SHA256: "tool-{version}.tar.xz"
  - name: lib
    track:
      LIB_VER:
        repo: acme/lib
        strip-v: true
  - name: kernel
    track:
      KERNEL_VER:
        kernel-org: stable
        digests:
          KERNEL_SHA256: "linux-{version}.tar.xz"
"""


def _kernel_org(api: Path, version: str, sums: str) -> None:
    """Write a kernel.org ``releases.json`` and the ``sha256sums.asc``.

    Args:
        api: API tree root.
        version: Version of the stable release.
        sums: Content of ``sha256sums.asc``.
    """
    (api / 'packages.yml').write_text(KERNEL_MANIFEST)
    cdn = api / 'cdn'
    cdn.mkdir(exist_ok=True)
    (cdn / 'sha256sums.asc').write_text(sums)
    releases = [{
        'moniker': 'mainline',
        'version': '9.9-rc1',
        'source': f'file://{cdn}/linux-9.9-rc1.tar.xz'
    }, {
        'moniker': 'stable',
        'version': version,
        'source': f'file://{cdn}/linux-{version}.tar.xz'
    }]
    (api / 'releases.json').write_text(json.dumps({'releases': releases}))


def test_resolve_follows_kernel_org_stream(api: Path) -> None:
    """The kernel.org moniker gives the version and the tarball digest.

    Args:
        api: API tree fixture.
    """
    _kernel_org(api, '7.2.11', f'{DIGEST}  linux-7.2.11.tar.xz\n')
    result = _run(
        api,
        'resolve',
        PKG='kernel',
        BASE_ENV='A=1',
        KERNEL_ORG_URL=f'file://{api}'
    )
    assert result.returncode == 0, result.stderr
    assert _env_output(api) == [
        'A=1', 'KERNEL_VER=7.2.11', f'KERNEL_SHA256={DIGEST}'
    ]


def test_resolve_kernel_without_digest_stays_unset(api: Path) -> None:
    """A release with no line in ``sha256sums.asc`` is left unresolved.

    Args:
        api: API tree fixture.
    """
    _kernel_org(api, '7.2.11', f'{DIGEST}  linux-7.2.10.tar.xz\n')
    result = _run(
        api,
        'resolve',
        PKG='kernel',
        BASE_ENV='A=1',
        KERNEL_ORG_URL=f'file://{api}'
    )
    assert result.returncode == 0
    assert _env_output(api) == ['A=1']
    assert '::warning::' in result.stderr


def test_check_matches_kernel_deb_revision(api: Path) -> None:
    """A kernel deb ``<version>-<revision>`` counts as the published release.

    Args:
        api: API tree fixture.
    """
    _kernel_org(api, '7.2.11', f'{DIGEST}  linux-7.2.11.tar.xz\n')
    url = _published(
        api, {
            'tool-amd64': ['tool_2.0_amd64.deb'],
            'lib-amd64': ['lib_2026.4_amd64.deb'],
            'kernel-amd64': ['linux-image-common_7.2.11-1_amd64.deb']
        }
    )
    result = _run(
        api,
        'check',
        PAGES_URL=url,
        STRICT='true',
        KERNEL_ORG_URL=f'file://{api}'
    )
    assert result.returncode == 0, result.stdout


def _kernel_with_patches(api: Path, available: set[str]) -> None:
    """Write two stable series and a patch manifest served for some of them.

    Args:
        api: API tree root.
        available: Series (e.g. ``7.2``) whose patch file exists.
    """
    (api / 'packages.yml').write_text(
        KERNEL_MANIFEST.replace(
            'kernel-org: stable',
            'kernel-org: stable\n        require-patches: patches.yml'
        )
    )
    cdn = api / 'cdn'
    cdn.mkdir(exist_ok=True)
    (cdn / 'sha256sums.asc').write_text(
        f'{DIGEST}  linux-7.2.11.tar.xz\n{DIGEST}  linux-7.1.20.tar.xz\n'
    )
    for series in ('7.2', '7.1'):
        (api / series).mkdir(exist_ok=True)
        if series in available:
            (api / series / 'bore.patch').write_text('x')
    (api / 'patches.yml'
     ).write_text(f'patches:\n  - url: file://{api}/{{series}}/bore.patch\n')
    releases = [{
        'moniker': 'stable',
        'version': '7.2.11',
        'source': f'file://{cdn}/linux-7.2.11.tar.xz'
    }, {
        'moniker': 'stable',
        'version': '7.1.20',
        'source': f'file://{cdn}/linux-7.1.20.tar.xz'
    }]
    (api / 'releases.json').write_text(json.dumps({'releases': releases}))


def test_resolve_skips_a_kernel_the_patches_do_not_cover(api: Path) -> None:
    """The newest release with every patch upstream wins.

    Args:
        api: API tree fixture.
    """
    _kernel_with_patches(api, {'7.1'})
    result = _run(
        api,
        'resolve',
        PKG='kernel',
        BASE_ENV='A=1',
        KERNEL_ORG_URL=f'file://{api}'
    )
    assert result.returncode == 0, result.stderr
    assert _env_output(api) == [
        'A=1', 'KERNEL_VER=7.1.20', f'KERNEL_SHA256={DIGEST}'
    ]


def test_resolve_skips_a_kernel_whose_digest_is_not_published(
    api: Path
) -> None:
    """A release listed before its checksum exists falls back to the next.

    Args:
        api: API tree fixture.
    """
    _kernel_with_patches(api, {'7.2', '7.1'})
    (api / 'cdn' / 'sha256sums.asc'
     ).write_text(f'{DIGEST}  linux-7.1.20.tar.xz\n')
    result = _run(
        api,
        'resolve',
        PKG='kernel',
        BASE_ENV='A=1',
        KERNEL_ORG_URL=f'file://{api}'
    )
    assert result.returncode == 0, result.stderr
    assert _env_output(api) == [
        'A=1', 'KERNEL_VER=7.1.20', f'KERNEL_SHA256={DIGEST}'
    ]
    assert 'no sha256 for linux-7.2.11.tar.xz' in result.stderr


def test_resolve_takes_the_newest_kernel_when_patches_cover_it(
    api: Path
) -> None:
    """With every patch available the newest release is kept.

    Args:
        api: API tree fixture.
    """
    _kernel_with_patches(api, {'7.2', '7.1'})
    result = _run(
        api,
        'resolve',
        PKG='kernel',
        BASE_ENV='A=1',
        KERNEL_ORG_URL=f'file://{api}'
    )
    assert result.returncode == 0, result.stderr
    assert 'KERNEL_VER=7.2.11' in _env_output(api)


def test_resolve_refuses_a_kernel_older_than_the_published_one(
    api: Path
) -> None:
    """A release below the published kernel is never resolved.

    Args:
        api: API tree fixture.
    """
    _kernel_with_patches(api, {'7.1'})
    url = _published(
        api, {'kernel-amd64': ['linux-image-common_7.2.10-1_amd64.deb']}
    )
    result = _run(
        api,
        'resolve',
        PKG='kernel',
        BASE_ENV='A=1',
        PAGES_URL=url,
        KERNEL_ORG_URL=f'file://{api}'
    )
    assert 'older than the published 7.2.10' in result.stderr
    assert not any(line.startswith('KERNEL_VER') for line in _env_output(api))


def test_resolve_finds_the_patches_next_to_the_source_manifest(
    api: Path, tmp_path: Path
) -> None:
    """An expanded copy elsewhere still resolves ``require-patches``.

    Args:
        api: API tree fixture.
        tmp_path: Scratch directory holding the expanded copy.
    """
    _kernel_with_patches(api, {'7.1'})
    expanded = tmp_path / 'expanded'
    expanded.mkdir()
    (expanded / 'packages.yml').write_text((api / 'packages.yml').read_text())
    result = _run(
        api,
        'resolve',
        PKG='kernel',
        BASE_ENV='A=1',
        MAN=str(expanded / 'packages.yml'),
        SOURCE_MAN=str(api / 'packages.yml'),
        KERNEL_ORG_URL=f'file://{api}'
    )
    assert result.returncode == 0, result.stderr
    assert 'KERNEL_VER=7.1.20' in _env_output(api)


def test_resolve_reports_a_missing_patch_manifest(api: Path) -> None:
    """A patch manifest that cannot be read is an error, not a free pass.

    Args:
        api: API tree fixture.
    """
    _kernel_with_patches(api, {'7.2', '7.1'})
    (api / 'patches.yml').unlink()
    result = _run(
        api,
        'resolve',
        PKG='kernel',
        BASE_ENV='A=1',
        KERNEL_ORG_URL=f'file://{api}'
    )
    assert 'patch manifest' in result.stderr
    assert not any(line.startswith('KERNEL_VER') for line in _env_output(api))
