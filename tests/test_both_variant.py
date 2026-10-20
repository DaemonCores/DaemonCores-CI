"""Tests of the unsigned variant of the ``both`` signing mode.

The matrix expansion of ``bootc-build.yml`` is extracted from the workflow
and run on a small matrix; the wiring of the other files is checked on the
text, since a workflow cannot be run outside GitHub.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JQ_WORKS = shutil.which('jq') is not None and subprocess.run(['jq', '-n', '1'],
                                                             capture_output=True
                                                             ).returncode == 0
BUILD = (ROOT / '.github' / 'workflows' / 'bootc-build.yml').read_text()
START = 'primary_fields='
END = '          has_derived="false"'


def _expand(nosign: bool) -> dict[str, list[dict[str, str]]]:
    """Run the variant expansion of the discover step on a sample matrix.

    Args:
        nosign: Value of the ``nosign_images`` input.

    Returns:
        The three matrices after expansion.
    """
    block = BUILD[BUILD.index(START):BUILD.index(END)]
    wave1 = [{
        'tag': 'latest',
        'arch': 'amd64'
    }, {
        'tag': 'latest-4k',
        'arch': 'arm64'
    }]
    wave2 = [{'tag': 'iso', 'arch': 'amd64'}]
    manifest = [{
        'tag': 'latest',
        'lock': False
    }, {
        'tag': 'latest-4k',
        'lock': False
    }, {
        'tag': 'iso',
        'lock': False
    }]
    script = (
        f'matrix_wave1=\'{json.dumps(wave1)}\'\n'
        f'matrix_wave2=\'{json.dumps(wave2)}\'\n'
        f'manifest_matrix=\'{json.dumps(manifest)}\'\n'
        f'NOSIGN_IMAGES={"true" if nosign else "false"}\n'
        f'{block}\n'
        'jq -cn --argjson a "$matrix_wave1" --argjson b "$matrix_wave2" '
        '--argjson c "$manifest_matrix" '
        '\'{wave1: $a, wave2: $b, manifest: $c}\''
    )
    out = subprocess.run(['bash', '-c', script],
                         capture_output=True,
                         text=True,
                         check=True)
    return json.loads(out.stdout)  # type: ignore[no-any-return]


@pytest.mark.skipif(not JQ_WORKS, reason='jq is not usable here')
def test_without_nosign_images_only_the_primary_variant() -> None:
    """Off by default: no ``-nosign`` entry, primary suite everywhere."""
    got = _expand(False)
    assert [e['tag'] for e in got['wave1']] == ['latest', 'latest-4k']
    assert {e['suite'] for e in got['wave1'] + got['wave2']} == {'trixie'}
    assert {e['cache_dir'] for e in got['wave1']} == {'debs-cache'}
    assert len(got['manifest']) == 3


@pytest.mark.skipif(not JQ_WORKS, reason='jq is not usable here')
def test_nosign_images_duplicate_wave1_and_manifests_only() -> None:
    """Every wave-1 image gets an unsigned twin; the derived ISO does not."""
    got = _expand(True)
    assert [e['tag'] for e in got['wave1']
            ] == ['latest', 'latest-4k', 'latest-nosign', 'latest-4k-nosign']
    twin = got['wave1'][2]
    assert (twin['suite'], twin['component'], twin['cache_dir']
            ) == ('trixie-nosign', 'nosign', 'debs-cache-nosign')
    assert twin['variant'] == '-nosign'
    assert got['wave1'][0]['suite'] == 'trixie'
    assert [e['tag'] for e in got['wave2']] == ['iso']
    assert [e['tag'] for e in got['manifest']] == [
        'latest', 'latest-4k', 'iso', 'latest-nosign', 'latest-4k-nosign'
    ]


@pytest.mark.parametrize(
    'path', [
        '.github/workflows/build-one-deb.yml',
        '.github/workflows/bootc-debs-builder.yml',
    ]
)
def test_variant_suffix_reaches_the_artifact_names(path: str) -> None:
    """Artifacts of the two variants must not share a name.

    Args:
        path: Workflow file that must carry the variant suffix.
    """
    text = (ROOT / path).read_text()
    assert 'variant_suffix' in text
    if 'build-one-deb' in path:
        assert text.count('${{ inputs.variant_suffix }}') >= 4


def test_unsigned_debs_never_use_the_signed_cache() -> None:
    """The deb cache folder follows the variant suffix."""
    script = (ROOT / 'build-deb' / 'build-deb.sh').read_text()
    assert 'CACHE_DIR="debs-cache${VARIANT_SUFFIX:-}"' in script
    assert '/debs-cache/' not in script


def test_publish_adds_the_unsigned_suite() -> None:
    """The end action publishes trixie-nosign next to trixie."""
    action = (ROOT / 'bootc-debs-end' / 'action.yml').read_text()
    assert 'codename: trixie-nosign' in action
    assert 'components: nosign' in action
    assert 'pattern: debs-*-amd64-nosign' in action
    assert 'debs-cache-nosign' in action
