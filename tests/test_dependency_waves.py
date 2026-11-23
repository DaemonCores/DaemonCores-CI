"""Tests for ``scripts/dependency-waves.py`` (manifest deps -> wave outputs)."""

import json
import os
import subprocess
import sys
from pathlib import Path

from typed_docs import as_list, as_str, load_json

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "dependency-waves.py"
WORKFLOW = ROOT / ".github" / "workflows" / "bootc-debs-builder.yml"


def run_waves(tmp_path: Path,
              packages: list[dict[str, object]]) -> dict[str, list[str]]:
    """Run the script on ``packages`` and return the parsed wave outputs.

    Args:
        tmp_path: Pytest temporary directory, holding the manifest and output.
        packages: Package entries of the manifest.

    Returns:
        The wave outputs, keyed by output name.
    """
    manifest = tmp_path / "pkgs.json"
    manifest.write_text(json.dumps({"packages": packages}))
    out = tmp_path / "output"
    subprocess.run(
        [sys.executable, str(SCRIPT),
         str(manifest)],
        check=True,
        env={
            **os.environ, "GITHUB_OUTPUT": str(out)
        },
    )
    result: dict[str, list[str]] = {}
    for line in out.read_text().splitlines():
        key, value = line.split("=", 1)
        result[key] = [as_str(name) for name in as_list(load_json(value))]
    return result


def test_waves_follow_dependency_depth(tmp_path: Path) -> None:
    """Depth of the deepest dep chain decides the wave; names are sorted.

    Args:
        tmp_path: Pytest temporary directory.
    """
    got = run_waves(
        tmp_path, [
            {
                "name": "b"
            },
            {
                "name": "a",
                "deps": []
            },
            {
                "name": "c",
                "deps": ["a"]
            },
            {
                "name": "d",
                "deps": ["c", "b"]
            },
            {
                "name": "e",
                "deps": ["d"]
            },
        ]
    )
    assert got == {
        "wave0": ["a", "b"],
        "wave1": ["c"],
        "wave2": ["d"],
        "wave3": ["e"]
    }


def test_empty_waves_are_empty_arrays(tmp_path: Path) -> None:
    """Unused waves are still emitted as ``[]`` for the job conditions.

    Args:
        tmp_path: Pytest temporary directory.
    """
    got = run_waves(tmp_path, [{"name": "only"}])
    assert got == {"wave0": ["only"], "wave1": [], "wave2": [], "wave3": []}


def test_workflow_uses_the_file_not_a_heredoc() -> None:
    """The workflow calls the script by path and embeds no heredoc program."""
    text = WORKFLOW.read_text()
    assert ".ci-tooling/scripts/dependency-waves.py" in text
    assert "<<'PY'" not in text


def test_a_chain_deeper_than_the_wave_jobs_is_an_error(tmp_path: Path) -> None:
    """A fifth wave has no job: it must fail loudly, not drop the package.

    Args:
        tmp_path: Pytest temporary directory.
    """
    chain = [{
        "name": "p0"
    }] + [{
        "name": f"p{i}",
        "deps": [f"p{i - 1}"]
    } for i in range(1, 5)]
    manifest = tmp_path / "pkgs.json"
    manifest.write_text(json.dumps({"packages": chain}))
    done = subprocess.run([sys.executable,
                           str(SCRIPT),
                           str(manifest)],
                          env={
                              **os.environ, "GITHUB_OUTPUT":
                              str(tmp_path / "output")
                          },
                          capture_output=True,
                          text=True,
                          check=False)
    assert done.returncode != 0
    assert "p4" in done.stderr
