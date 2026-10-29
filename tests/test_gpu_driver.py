"""Tests of the ``when.gpu-driver`` opt-in flag of the manifest.

The filter in ``manifest-expand/expand.jq`` is run on a small manifest, and
the wiring of the workflows is checked on the text, since a workflow cannot
be run outside GitHub. Nothing here runs a real build: the flag only decides
which packages the plan carries.
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


def _run_filter(manifest: Any, **env: str) -> list[str]:
    """Run ``expand.jq`` on a manifest with the given env.

    Args:
        manifest: JSON-serialisable manifest.
        env: Extra environment variables for the jq process.

    Returns:
        The kept package names.
    """
    result = subprocess.run([
        'jq', '-c', '-f',
        str(ROOT / 'manifest-expand' / 'expand.jq')
    ],
                            input=json.dumps(manifest),
                            env=dict(os.environ, **env),
                            capture_output=True,
                            text=True,
                            check=True)
    return [p['name'] for p in json.loads(result.stdout)['packages']]


def _manifest() -> Any:
    """A two-package manifest: one plain, one gpu-driver marked.

    Returns:
        The manifest object.
    """
    return {
        'packages': [{
            'name': 'mesa'
        }, {
            'name': 'nvidia-libs',
            'when': {
                'gpu-driver': True
            }
        }]
    }


@needs_jq
class TestManifestFilter:
    """The ``when.gpu-driver`` packages follow ``GPU_DRIVER`` only."""

    def test_no_drops_the_marked_packages(self) -> None:
        """``no`` keeps only the unmarked packages."""
        assert _run_filter(_manifest(), GPU_DRIVER='no') == ['mesa']

    def test_yes_and_empty_keep_everything(self) -> None:
        """``yes`` and an empty value keep every package.

        The build-one-deb path sets no variable at all, so the empty value
        must behave as keep.
        """
        assert _run_filter(_manifest(), GPU_DRIVER='yes') == \
            ['mesa', 'nvidia-libs']
        assert _run_filter(_manifest(), GPU_DRIVER='') == \
            ['mesa', 'nvidia-libs']

    def test_coexists_with_the_signing_filter(self) -> None:
        """unsigned-kernel kept while gpu-driver is dropped, and the reverse."""
        unsigned = {
            'packages': [{
                'name': 'mesa'
            }, {
                'name': 'kernel-nosign',
                'when': {
                    'unsigned-kernel': True
                }
            }]
        }
        assert _run_filter(unsigned, GPU_DRIVER='', UNSIGNED_KERNEL='no') == \
            ['mesa']
        assert _run_filter(_manifest(), GPU_DRIVER='no', UNSIGNED_KERNEL='no'
                           ) == ['mesa']


class TestWiring:
    """The workflows carry the flag end to end."""

    def _action_env(self) -> dict[str, Any]:
        """Read the env of the first step of the manifest-expand action.

        Returns:
            The parsed ``env`` mapping.
        """
        action = cast(dict[str, Any],
                      yaml.safe_load((ROOT / 'manifest-expand'
                                      / 'action.yml').read_text()))
        return action['runs']['steps'][0]['env']

    def test_action_declares_and_passes_gpu_driver(self) -> None:
        """The action input reaches the ``GPU_DRIVER`` env of the step.

        Args:
            fixture_action: Parsed ``action.yml``.
        """
        action = cast(dict[str, Any],
                      yaml.safe_load((ROOT / 'manifest-expand'
                                      / 'action.yml').read_text()))
        assert action['inputs'].get('gpu-driver', {}).get('default') == ''
        assert self._action_env().get(
            'GPU_DRIVER') == '${{ inputs.gpu-driver }}'

    def test_debs_builder_drops_by_default(self) -> None:
        """The deb builder maps a ``gpu_driver`` input to the filter.

        A push plan (no dispatch) writes ``no``, so the EULA-restricted
        entries never join a public build by accident.
        """
        text = (WORKFLOWS / 'bootc-debs-builder.yml').read_text()
        assert "gpu_driver == 'yes' && 'yes' || 'no'" in text
        assert 'gpu-driver: ${{ inputs.gpu_driver' in text
        assert 'gpu_driver:' in text

    def test_full_pipeline_forwards_and_never_defaults_to_yes(self) -> None:
        """The pipeline input forwards per arch and defaults to false."""
        text = (WORKFLOWS / 'full-pipeline.yml').read_text()
        assert text.count("gpu_driver: ${{ inputs.gpu_driver == 'true' && "
                          "'yes' || 'no' }}") == 2
        assert "gpu_driver:\n        type: string\n        default: 'false'" \
            in text


class TestGpuUsageTwins:
    """The ia/gaming usage twins follow the -nosign twin pattern."""

    def _text(self) -> str:
        """The current bootc-build.yml workflow text.

        Returns:
            The raw file text.
        """
        return (WORKFLOWS / "bootc-build.yml").read_text()

    def test_discover_filters_on_the_label_and_the_input(self) -> None:
        """The ia twin is gated on its label plus the input, gaming not."""
        text = self._text()
        # Both labels are grepped in the discover step.
        assert "gpu-ia-image" in text
        assert "gpu-gaming-image" in text
        # Only the ia label sits on the input-gated line.
        gated = [
            line for line in text.splitlines()
            if "gpu-ia-image" in line and "grep -qE" in line
        ]
        assert len(gated) == 1
        assert "GPU_DRIVER_IMAGES" in gated[0]
        ungated = [
            line for line in text.splitlines()
            if "gpu-gaming-image" in line and "grep -qE" in line
        ]
        assert len(ungated) == 1
        assert "GPU_DRIVER_IMAGES" not in ungated[0]
        assert "GPU_DRIVER_IMAGES: ${{ inputs.gpu_driver_images }}" in text

    def test_ia_twin_is_amd64_only(self) -> None:
        """The ia rows stay on amd64: linux-modules-nvidia is amd64-only."""
        text = self._text()
        # The ia flag is per-arch (the gaming one is not).
        nv = [line for line in text.splitlines() if "--argjson nv" in line]
        gaming = [
            line for line in text.splitlines() if "--argjson gmg" in line
        ]
        assert len(nv) == len(gaming) == 1
        assert '$arch" == amd64' in nv[0] and '[[ ' in nv[0]
        # The gaming flag is set once per Containerfile, not per arch.
        assert "--argjson gmg \"$has_gaming\"" in gaming[0]
        # The ia manifest row carries amd64 only, gaming both arches.
        lines = text.splitlines()
        ia_rows = [
            lines[i + 1] for i, line in enumerate(lines)
            if "--arg tag " in line and "${tag}-ia" in line
        ]
        gaming_rows = [
            lines[i + 1] for i, line in enumerate(lines)
            if "--arg tag " in line and "${tag}-gaming" in line
        ]
        assert len(ia_rows) == len(gaming_rows) == 1
        assert "[\"amd64\"]" in ia_rows[0]
        assert "[\"amd64\", \"arm64\"]" in gaming_rows[0]

    def test_matrix_and_manifest_carry_the_twins(self) -> None:
        """Each matrix entry flags the twins; the manifest gains -ia/-gaming."""
        text = self._text()
        assert "twin_suffix: $ts, nvidia: $nv, gaming: $gmg}]" in text
        assert "--arg tag \"${tag}-gaming\"" in text
        assert "--arg tag \"${tag}-ia\"" in text

    def test_build_passes_the_gpu_packages_only_to_the_twins(self) -> None:
        """The build-args feed the twins; the public build keeps both empty."""
        text = self._text()
        assert "vars.NVIDIA_IA_PKGS ||" in text
        assert 'GPU_IA_PKGS=$( [[ "$tag" == *-ia ]]' in text
        assert 'GPU_GAMING_PKGS=$( [[ "$tag" == *-gaming ]]' in text
        assert 'build_one "${CF_TAG}-gaming"' in text
        assert '"${GAMING_HASH}"' in text
        assert 'build_one "${CF_TAG}-ia"' in text
        assert '"${IA_HASH}"' in text
        assert "ia_hash=$(printf" in text and "nia" in text
        assert "gaming_hash=$(printf" in text and "ngaming" in text
