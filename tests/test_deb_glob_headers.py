"""build-deb.sh lists EVERY deb-glob pattern, not just the first one.

The kernel class republishes the linux-headers deb of the same build next
to the image deb. When the build ran (hash bump), the produced debs list
came from a single ``ls <glob>.deb``: only the image rode hashes.json, the
headers deb was pruned from the APT index and every cache hit restored a
headers-less set (run 42899558906, 'no linux-headers for flavour x64v3 in
the APT index'). DEB_GLOB now carries several space-separated patterns.
"""

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'build-deb' / 'build-deb.sh'
IMAGE = 'linux-image-common-vm-x64v3_7.2.15-1_amd64.deb'
HEADERS = 'linux-headers-vm-7.2.15-x64v3-xanmod1_7.2.15-1_amd64.deb'


def test_multi_glob_records_every_pattern(tmp_path: Path) -> None:
    """The metadata debs carry the image deb AND the republished headers deb.

    Before the fix the single ``ls <glob>.deb`` matched only the first
    pattern, so the headers deb stayed out of hashes.json and the
    end-of-run prune removed it from the published APT index.
    """
    pages = tmp_path / 'pages'
    pages.mkdir()
    debs = tmp_path / 'debs'
    debs.mkdir()
    for name, package in (
        (IMAGE, 'linux-image-common-vm-x64v3'),
        (HEADERS, 'linux-headers-vm-7.2.15-x64v3-xanmod1'),
    ):
        pkg = debs / '_' / name / 'DEBIAN'
        pkg.mkdir(parents=True)
        (pkg / 'control').write_text(
            f'Package: {package}\nVersion: 7.2.15-1\nArchitecture: amd64\n'
            'Maintainer: ci <ci@example.invalid>\nDescription: test\n'
        )
        subprocess.run([
            'dpkg-deb', '--build',
            str(pkg.parent),
            str(debs / name)
        ],
                       check=True,
                       capture_output=True)
    out = tmp_path / 'out'
    out.touch()
    env = {
        'PATH': '/usr/bin:/bin',
        'PACKAGE': 'linux-image-vm-x64v3',
        'ARCH': 'amd64',
        'DEB_GLOB': 'linux-image-common-vm-x64v3_* linux-headers-vm-*',
        'BUILD': 'true',
        'PAGES_URL': f'file://{pages}',
        'GITHUB_OUTPUT': str(out),
        'GITHUB_WORKSPACE': str(tmp_path),
        'DEBS_DIR': str(debs),
        'SKIP_BUILD': 'false',
    }
    subprocess.run(['bash', str(SCRIPT)],
                   env=env,
                   capture_output=True,
                   text=True)
    meta = json.loads(
        (debs / '.meta' / 'linux-image-vm-x64v3-amd64.json').read_text()
    )
    assert sorted(meta['debs']) == sorted([IMAGE, HEADERS]), meta


def test_single_glob_still_works(tmp_path: Path) -> None:
    """A one-pattern DEB_GLOB behaves exactly as before.

    Args:
        tmp_path: Scratch directory.
    """
    pages = tmp_path / 'pages'
    pages.mkdir()
    debs = tmp_path / 'debs'
    debs.mkdir()
    pkg = debs / '_a' / 'DEBIAN'
    pkg.mkdir(parents=True)
    (pkg / 'control').write_text(
        'Package: onlyimage\nVersion: 1\nArchitecture: amd64\n'
        'Maintainer: ci <ci@example.invalid>\nDescription: test\n'
    )
    subprocess.run([
        'dpkg-deb', '--build',
        str(pkg.parent),
        str(debs / 'onlyimage_1_amd64.deb')
    ],
                   check=True,
                   capture_output=True)
    (debs / 'unrelated_1_amd64.deb').touch()
    out = tmp_path / 'out'
    out.touch()
    env = {
        'PATH': '/usr/bin:/bin',
        'PACKAGE': 'onlyimage',
        'ARCH': 'amd64',
        'DEB_GLOB': 'onlyimage_*',
        'BUILD': 'true',
        'PAGES_URL': f'file://{pages}',
        'GITHUB_OUTPUT': str(out),
        'GITHUB_WORKSPACE': str(tmp_path),
        'DEBS_DIR': str(debs),
        'SKIP_BUILD': 'false',
    }
    subprocess.run(['bash', str(SCRIPT)],
                   env=env,
                   capture_output=True,
                   text=True)
    meta = json.loads((debs / '.meta' / 'onlyimage-amd64.json').read_text())
    assert meta['debs'] == ['onlyimage_1_amd64.deb'], meta
