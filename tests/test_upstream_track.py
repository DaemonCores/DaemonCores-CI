"""Unit tests for ``upstream-track/track.sh``.

The GitHub API is replaced by a directory tree served through ``file://``: a
release is the JSON file ``repos/<owner>/<repo>/releases/latest``.
"""

import hashlib
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
    assert '::notice::' in result.stderr


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


def _bore_patch(api: Path, version: str, applies: bool) -> None:
    """Write the ``bore.patch`` of one kernel version.

    The patch applies fuzz-0 to the fixture tree exactly when ``applies``.

    Args:
        api: API tree root.
        version: Kernel version the patch lives under.
        applies: Whether the patch context matches the fixture tree.
    """
    patch = api / version / 'bore.patch'
    patch.parent.mkdir(parents=True, exist_ok=True)
    context = ' feature' if applies else ' absent'
    patch.write_text(f'--- a/k\n+++ b/k\n@@ -1 +1,2 @@\n{context}\n+elected\n')


def _kernel_point(api: Path, cdn: Path, version: str,
                  applies: bool) -> dict[str, str]:
    """Serve one kernel release end to end.

    The fixture writes the real tarball, its digest line in
    ``sha256sums.asc`` and its bore patch.

    Args:
        api: API tree root.
        cdn: Directory serving the tarballs and sums.
        version: Kernel version.
        applies: Whether the bore patch applies fuzz-0.

    Returns:
        The tarball digest under the version key.
    """
    staging = api / f'linux-{version}'
    staging.mkdir(exist_ok=True)
    (staging / 'k').write_text('feature\n')
    tarball = cdn / f'linux-{version}.tar.xz'
    subprocess.run([
        'tar', '-cJf',
        str(tarball), '-C',
        str(api), f'linux-{version}'
    ],
                   check=True)
    digest = hashlib.sha256(tarball.read_bytes()).hexdigest()
    with (cdn / 'sha256sums.asc').open('a') as sums:
        sums.write(f'{digest}  linux-{version}.tar.xz\n')
    _bore_patch(api, version, applies)
    return {version: digest}


def _kernel_election(api: Path, entries: list[tuple[str,
                                                    bool]]) -> dict[str, str]:
    """Serve kernel.org stable releases judged by a bore patch set.

    The manifest declares ``require-patches: patches.yml`` so the dry-run
    election of the entry runs.

    Args:
        api: API tree root.
        entries: (version, applies) pairs, newest kernel.org release first.

    Returns:
        The tarball digests by version.
    """
    (api / 'packages.yml').write_text(
        KERNEL_MANIFEST.replace(
            'kernel-org: stable',
            'kernel-org: stable\n        require-patches: patches.yml'
        )
    )
    cdn = api / 'cdn'
    cdn.mkdir(exist_ok=True)
    digests: dict[str, str] = {}
    for version, applies in entries:
        digests.update(_kernel_point(api, cdn, version, applies))
    (api / 'patches.yml').write_text(
        'patches:\n'
        '  - name: bore\n'
        '    stage: common\n'
        f'    url: file://{api}/{{version}}/bore.patch\n'
    )
    releases = [{
        'moniker': 'stable',
        'version': version,
        'source': f'file://{cdn}/linux-{version}.tar.xz'
    } for version, _ in entries]
    (api / 'releases.json').write_text(json.dumps({'releases': releases}))
    return digests


def test_resolve_elects_the_newest_release_the_patch_set_applies_to(
    api: Path, tmp_path: Path
) -> None:
    """The election elects the newest release the patch set passes.

    The latest-passing-gate: the newest kernel.org release whose whole
    patch set dry-runs fuzz-0 wins, the failing newer one is a documented
    K-PORT, never a pinned series.

    Args:
        api: API tree root.
        tmp_path: Scratch directory for the election cache.
    """
    digests = _kernel_election(api, [('7.3.1', False), ('7.2.15', True)])
    result = _run(
        api,
        'resolve',
        PKG='kernel',
        BASE_ENV='A=1',
        KERNEL_ORG_URL=f'file://{api}',
        KERNEL_ELECT_CACHE=str(tmp_path / 'elect')
    )
    assert result.returncode == 0, result.stderr
    assert _env_output(api) == [
        'A=1', 'KERNEL_VER=7.2.15', f'KERNEL_SHA256={digests["7.2.15"]}'
    ]
    assert 'election: ' in result.stderr
    assert 'dry-run FAIL on 7.3.1' in result.stderr


def test_resolve_walks_an_old_point_release_of_a_failing_series(
    api: Path, tmp_path: Path
) -> None:
    """A failing series is walked down before the next lower series.

    When the newest release of a series fails, the probe descends to the
    older point releases of the same series from the stable git tags
    before it falls to the next lower series.

    Args:
        api: API tree root.
        tmp_path: Scratch directory for the election cache.
    """
    digests = _kernel_election(api, [('7.3.3', False), ('7.2.15', False)])
    digests.update(_kernel_point(api, api / 'cdn', '7.3.2', True))
    repo = api / 'stable.git'
    git = ['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@t']
    subprocess.run(['git', 'init', '-q', '-b', 'main', str(repo)], check=True)
    subprocess.run([*git, 'commit', '-q', '--allow-empty', '-m', 'seed'],
                   check=True)
    for tag in ('7.3.3', '7.3.2'):
        subprocess.run([*git, 'tag', '-m', f'v{tag}', f'v{tag}'], check=True)
    result = _run(
        api,
        'resolve',
        PKG='kernel',
        BASE_ENV='A=1',
        KERNEL_ORG_URL=f'file://{api}',
        GIT_TAGS_URL=f'file://{repo}',
        KERNEL_ELECT_CACHE=str(tmp_path / 'elect')
    )
    assert result.returncode == 0, result.stderr
    assert _env_output(api) == [
        'A=1', 'KERNEL_VER=7.3.2', f'KERNEL_SHA256={digests["7.3.2"]}'
    ]
    assert 'dry-run FAIL on 7.3.3' in result.stderr


def _kernel_with_engine(api: Path, available: set[str]) -> None:
    """Point ``require-patches: engine`` at a fake product checkout.

    The series dirs use the xanmod layout ({seriesdir}/{topic}); some
    series are unavailable (e.g. 7.3 without a bore sibling).

    Args:
        api: API tree root (receiving the kernel-src patch set).
        available: Series whose bore sibling exists.
    """
    (api / 'packages.yml').write_text(
        KERNEL_MANIFEST.replace(
            'kernel-org: stable',
            'kernel-org: stable\n        require-patches: engine'
        )
    )
    cdn = api / 'cdn'
    cdn.mkdir(exist_ok=True)
    (cdn / 'sha256sums.asc').write_text(
        f'{DIGEST}  linux-7.3.1.tar.xz\n{DIGEST}  linux-7.2.11.tar.xz\n'
    )
    # Owner 11/11 (R-871b): the election set resolves from the PRODUCT
    # checkout being tracked — kernel-src/patches next to the manifest.
    patches = api / 'kernel-src' / 'patches'
    patches.mkdir(parents=True)
    for series in ('7.3', '7.2'):
        sdir = api / f'linux-{series}.y-xanmod'
        sdir.mkdir(exist_ok=True)
        (sdir / 'topic').mkdir(exist_ok=True)
        if series in available:
            (sdir / 'topic' / '0001-xanmod-bore.patch').write_text('x')
    manifest = (
        'patches:\n'
        f'  - url: file://{api}/{{seriesdir}}/topic/0001-xanmod-bore.patch\n'
        '    topic: topic\n'
    )
    # The election set names kernel.yml + pve.yml + the kernel-tree gpu
    # sets: empty sets are a no-op of the dry-run judge (the empty-manifest
    # contract).
    (patches / 'kernel.yml').write_text(manifest)
    for name in ('pve.yml', 'gpu/amd.yml', 'gpu/intel.yml'):
        dest = patches / name
        dest.parent.mkdir(exist_ok=True)
        dest.write_text('patches: []\n')
    releases = [{
        'moniker': 'stable',
        'version': '7.3.1',
        'source': f'file://{cdn}/linux-7.3.1.tar.xz'
    }, {
        'moniker': 'stable',
        'version': '7.2.11',
        'source': f'file://{cdn}/linux-7.2.11.tar.xz'
    }]
    (api / 'releases.json').write_text(json.dumps({'releases': releases}))


def test_resolve_engine_mode_falls_back_to_the_covered_series(
    api: Path, tmp_path: Path
) -> None:
    """``require-patches: engine`` probes the product's kernel-src set.

    The newest series whose every patch is upstream wins (7.3 without
    bore → 7.2).

    Args:
        api: API tree fixture.
        tmp_path: Scratch directory for the election cache.
    """
    _kernel_with_engine(api, {'7.2'})
    result = _run(
        api,
        'resolve',
        PKG='kernel',
        BASE_ENV='A=1',
        KERNEL_ORG_URL=f'file://{api}',
    )
    assert result.returncode == 0, result.stderr
    assert _env_output(api) == [
        'A=1', 'KERNEL_VER=7.2.11', f'KERNEL_SHA256={DIGEST}'
    ]


def test_resolve_skips_a_kernel_whose_digest_is_not_published(
    api: Path
) -> None:
    """A release listed before its checksum exists falls back to the next.

    Args:
        api: API tree fixture.
    """
    _kernel_with_patches(api, {'7.2', '7.1'})
    (api / 'cdn' /
     'sha256sums.asc').write_text(f'{DIGEST}  linux-7.1.20.tar.xz\n')
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


def test_resolve_skips_when_no_kernel_digest_is_published(api: Path) -> None:
    """With no eligible release the build is a skip, not a failure.

    Args:
        api: API tree fixture.
    """
    _kernel_with_patches(api, {'7.2', '7.1'})
    (api / 'cdn' / 'sha256sums.asc').write_text('')
    result = _run(
        api,
        'resolve',
        PKG='kernel',
        BASE_ENV='A=1',
        KERNEL_ORG_URL=f'file://{api}'
    )
    assert result.returncode == 0, result.stderr
    assert 'no upstream release is eligible' in result.stderr
    assert 'skip=true' in (api / 'out').read_text().splitlines()
    assert not any(line.startswith('KERNEL_VER') for line in _env_output(api))


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
    assert 'skip=true' in (api / 'out').read_text().splitlines()


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
    assert 'skip=false' in (api / 'out').read_text().splitlines()


VARIANT_MANIFEST = """packages:
  - name: family
    when:
      arch: amd64
    variant-name-prefix: fam-
    variants:
      a-x64:
        when: {arch: amd64}
      b-arm:
        when: {arch: arm64}
      c-any: {}
    track:
      FAM_VER:
        repo: acme/fam
"""


def _variant_api(tmp_path: Path, debs: dict[str, list[str]]) -> Path:
    """Build an API tree with a variant-expanded family entry.

    Args:
        tmp_path: Pytest scratch directory.
        debs: Published deb file names by cache key.

    Returns:
        The API tree root.
    """
    tmp_path.joinpath('packages.yml').write_text(VARIANT_MANIFEST)
    _release(tmp_path, 'acme/fam', 'v8.0.0', {})
    _published(tmp_path, debs)
    return tmp_path


def test_check_expands_the_variant_cache_keys(api: Path) -> None:
    """A variant family publishes under its per-variant keys, on its arch.

    Args:
        api: API tree fixture.
    """
    _variant_api(
        api, {
            'fam-a-x64-amd64': ['fam-a-x64_8.0.0_amd64.deb'],
            'fam-c-any-amd64': ['fam-c-any_8.0.0_amd64.deb'],
            'fam-b-arm-arm64': ['fam-b-arm_8.0.0_arm64.deb']
        }
    )
    result = _run(api, 'check', PAGES_URL=f'file://{api}/pages', STRICT='true')
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'family FAM_VER: v8.0.0 is published' in result.stdout


def test_check_one_variant_missing_is_behind(api: Path) -> None:
    """A family is behind when one expanded variant lacks the release.

    Args:
        api: API tree fixture.
    """
    _variant_api(
        api, {
            'fam-a-x64-amd64': ['fam-a-x64_8.0.0_amd64.deb'],
            'fam-c-any-amd64': ['fam-c-any_4.0.0_amd64.deb']
        }
    )
    result = _run(api, 'check', PAGES_URL=f'file://{api}/pages', STRICT='true')
    assert result.returncode == 1
    assert 'family FAM_VER: upstream v8.0.0 is not published' \
        ' (fam-c-any-amd64)' in result.stdout


def test_check_skips_the_other_architecture(api: Path) -> None:
    """An entry declared for another arch is not checked here.

    Args:
        api: API tree fixture.
    """
    _variant_api(api, {'fam-b-arm-arm64': ['fam-b-arm_8.0.0_arm64.deb']})
    result = _run(
        api,
        'check',
        PAGES_URL=f'file://{api}/pages',
        STRICT='true',
        ARCH='arm64'
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'family FAM_VER' not in result.stdout
    # The arm64 job reports nothing for the family at all: its `when` gate
    # excludes it here, neither via the family nor via a per-variant key.
    assert 'fam-b-arm' not in result.stdout
