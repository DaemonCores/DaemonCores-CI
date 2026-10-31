"""Tests of the unsigned-kernel twin of the ``both`` signing mode.

The discover step of ``bootc-build.yml`` is run on a scratch repository of
Containerfiles, and the manifest filter is run on a small manifest; the wiring
of the other files is checked on the text, since a workflow cannot be run
outside GitHub.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / '.github' / 'workflows'
JQ_WORKS = shutil.which('jq') is not None and subprocess.run(['jq', '-n', '1'],
                                                             capture_output=True
                                                             ).returncode == 0
needs_jq = pytest.mark.skipif(not JQ_WORKS, reason='a working jq is required')


def _discover_script() -> str:
    """Return the shell of the discover step of ``bootc-build.yml``.

    Returns:
        The ``run`` script of the step that builds the matrices.
    """
    workflow = cast(
        dict[str, Any],
        yaml.safe_load((WORKFLOWS / 'bootc-build.yml').read_text())
    )
    steps = workflow['jobs']['discover']['steps']
    return next(s['run'] for s in steps if s.get('id') == 'list')


def _discover(tmp_path: Path, nosign: bool) -> dict[str, list[dict[str, Any]]]:
    """Run the discover step on a repository with three Containerfiles.

    Args:
        tmp_path: Scratch repository root.
        nosign: Value of the ``nosign_images`` input.

    Returns:
        The wave 1 and the manifest matrices.
    """
    generic = (
        'LABEL org.daemoncores.arm64-4k-image="true"\n'
        'LABEL org.daemoncores.unsigned-kernel-image="true"\n'
    )
    (tmp_path / 'Containerfile').write_text('FROM debian:trixie\n' + generic)
    (tmp_path / 'Containerfile.board').write_text('FROM debian:trixie\n')
    (tmp_path / 'Containerfile.iso'
     ).write_text('FROM ghcr.io/daemoncores/debian-bootc:latest_amd64\n')
    out = tmp_path / 'out'
    env = dict(
        os.environ,
        GITHUB_OUTPUT=str(out),
        GITHUB_REPOSITORY='DaemonCores/debian-bootc',
        NOSIGN_IMAGES='true' if nosign else 'false'
    )
    subprocess.run(['bash', '-c', _discover_script()],
                   cwd=tmp_path,
                   env=env,
                   check=True,
                   capture_output=True)
    lines = dict(
        ln.split('=', 1) for ln in out.read_text().splitlines() if '=' in ln
    )
    return {
        'wave1': json.loads(lines['matrix_wave1']),
        'manifest': json.loads(lines['manifest_matrix'])
    }


@needs_jq
def test_there_is_one_image_per_tag_and_arch(tmp_path: Path) -> None:
    """No lock or autoupdate dimension is left in the matrices.

    Args:
        tmp_path: Scratch repository root.
    """
    result = _discover(tmp_path, nosign=False)
    keys = {key for entry in result['wave1'] for key in entry}
    # No GPU dimension: one image carries every vendor.
    assert keys == {'file', 'tag', 'arch', 'kernel_suffix', 'twin_suffix'}
    assert all('lock' not in entry for entry in result['manifest'])
    pairs = [(e['tag'], e['arch']) for e in result['wave1']]
    assert len(pairs) == len(set(pairs))


@needs_jq
def test_twin_only_for_the_generic_images(tmp_path: Path) -> None:
    """The twin exists for amd64 and the 16 KB arm64 kernel, nothing else.

    The twin is not a matrix entry of its own: it is built by the job of its
    primary image, which learns the twin kernel from ``twin_suffix``.

    Args:
        tmp_path: Scratch repository root.
    """
    result = _discover(tmp_path, nosign=True)
    assert not [e for e in result['wave1'] if e['tag'].endswith('-nosign')]
    twin = {(e['tag'], e['arch']): e['twin_suffix'] for e in result['wave1']}
    assert twin == {
        ('latest', 'amd64'): '-nosign',
        ('latest', 'arm64'): '-16k-nosign',
        ('latest-4k', 'arm64'): '',
        ('board', 'amd64'): '',
        ('board', 'arm64'): '',
    }
    manifest = {e['tag']: e['arches'] for e in result['manifest']}
    assert manifest['latest-nosign'] == ['amd64', 'arm64']
    assert 'latest-4k-nosign' not in manifest
    assert 'board-nosign' not in manifest
    plain = {
        e['kernel_suffix']
        for e in result['wave1'] if e['tag'] == 'latest-4k'
    }
    assert plain == {''}


@needs_jq
def test_no_twin_without_the_input(tmp_path: Path) -> None:
    """Without ``nosign_images`` no -nosign image is planned.

    Args:
        tmp_path: Scratch repository root.
    """
    result = _discover(tmp_path, nosign=False)
    assert not [e for e in result['wave1'] if e['twin_suffix']]
    assert not [e for e in result['manifest'] if 'nosign' in e['tag']]


def _expand(unsigned: str) -> list[str]:
    """Run ``expand.jq`` on a manifest holding one unsigned-kernel twin.

    Args:
        unsigned: Value of ``UNSIGNED_KERNEL``.

    Returns:
        The kept package names.
    """
    manifest = {
        'packages': [{
            'name': 'kernel'
        }, {
            'name': 'kernel-nosign',
            'when': {
                'arch': 'amd64',
                'unsigned-kernel': True
            }
        }]
    }
    result = subprocess.run([
        'jq', '-c', '-f',
        str(ROOT / 'manifest-expand' / 'expand.jq')
    ],
                            input=json.dumps(manifest),
                            env=dict(os.environ, UNSIGNED_KERNEL=unsigned),
                            capture_output=True,
                            text=True,
                            check=True)
    return [p['name'] for p in json.loads(result.stdout)['packages']]


@needs_jq
def test_manifest_drops_the_twin_unless_asked() -> None:
    """Only ``no`` drops the twin; an unfiltered lookup keeps it."""
    assert _expand('no') == ['kernel']
    assert _expand('yes') == ['kernel', 'kernel-nosign']
    assert _expand('') == ['kernel', 'kernel-nosign']


def test_pipeline_wires_the_twin_from_the_both_mode() -> None:
    """Only the both mode with a key plans the twin kernel and images."""
    text = (WORKFLOWS / 'full-pipeline.yml').read_text()
    assert '[ "${mode}" = both ] && unsigned_kernel=yes' in text
    assert text.count(
        'unsigned_kernel: ${{ needs.prepare.outputs.unsigned_kernel }}'
    ) == 2
    twin_images = "nosign_images: ${{ needs.prepare.outputs.unsigned_kernel"
    assert twin_images + " == 'yes' }}" in text


def test_one_apt_suite_and_one_deb_cache() -> None:
    """The twin kernels share the suite and the cache of every other deb."""
    end = (ROOT / 'bootc-debs-end' / 'action.yml').read_text()
    script = (ROOT / 'build-deb' / 'build-deb.sh').read_text()
    assert 'trixie-nosign' not in end
    assert 'debs-cache-nosign' not in end + script
    assert 'variant_suffix' not in (
        WORKFLOWS / 'bootc-debs-builder.yml').read_text()


def test_signing_flag_only_hashes_the_packages_that_sign() -> None:
    """MODULE_SIGNING_ACTIVE reaches the hash of the signing packages only."""
    text = (ROOT / 'build-deb' / 'build-all.sh').read_text()
    assert '[[ "${signs}" != yes ]] || extra+="MODULE_SIGNING_ACTIVE=' in text
    definition = (ROOT / 'pkg-def' / 'pkg-def.sh').read_text()
    assert "grep -q 'kernel_signing_setup'" in definition
