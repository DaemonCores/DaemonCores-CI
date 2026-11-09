"""NVIDIA driver rolling pin (deb_helpers.sh nvidia_driver_detect).

The owner order 2026-11-09: the linux-modules-nvidia lane resolves its
driver release the way the kernel lane resolves its stream — auto detect,
no fixed pin. The helper takes NVIDIA_APT_REPO (the distro subdirectory of
NVIDIA's CUDA apt repos, default debian13) as the repo argument, parses the
Packages index of that repository directly, walks the candidates
NEWEST-FIRST and exports the FIRST Debian-revision-stripped release whose
open-gpu-kernel-modules tag exists. A preset NVIDIA_DRIVER_VERSION is
honored verbatim (the pin wins over detection). A lane with no tagged
candidate fails instead of forcing an untagged release (the 2026-11-04
paid lesson).

The tests are OFFLINE: NVIDIA_REPO_BASE points at a file:// fake repo and
NVIDIA_OPEN_REPO at a local bare git repository seeded with a few tags.
"""

import gzip
import os
import subprocess
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HELPER = REPO / "scripts" / "deb_helpers.sh"


def run_detect(script: str, **env_over: str) -> subprocess.CompletedProcess:
    """Run a snippet with deb_helpers.sh sourced (the same contract the
    builds get: deb_helpers.sh is imported implicitly once per build by
    build-deb.sh)."""
    env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ["HOME"],
        "LANG": "C",
        **env_over,
    }
    return subprocess.run(
        ["bash", "-c", f'source "{HELPER}"\nset -euo pipefail\n{script}'],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def make_apt_repo(base: Path, repo_dir: str, versions: list[str]) -> None:
    """Fake a CUDA apt repo layout: <base>/<repo-dir>/x86_64/Packages.gz
    carrying one libcuda1 record per version (the Debian-index record
    shape: Package first, blank-line separated)."""
    d = base / repo_dir / "x86_64"
    d.mkdir(parents=True, exist_ok=True)
    recs = []
    for v in versions:
        recs.append("\n".join([
            "Package: libcuda1",
            f"Version: {v}",
            "Architecture: amd64",
            "Multi-Arch: same",
            "",
        ]))
    (d / "Packages.gz").write_bytes(gzip.compress("\n".join(recs).encode()))


def make_tagged_repo(base: Path, tags: list[str]) -> str:
    """Local bare git repository with the given tags; file:// URL back."""
    home = base / "git-home"
    (home / ".git").mkdir(parents=True)
    git_env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    wd = base / "work"
    wd.mkdir()
    (wd / "f").write_text("x\n")
    for cmd in (
        ["git", "init", "-q", "-b", "main", str(wd)],
        ["git", "-C", str(wd), "add", "f"],
        ["git", "-C", str(wd), "commit", "-qm", "x"],
    ):
        subprocess.run(cmd, check=True, env=git_env)
    bare = base / "modules.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)],
                   check=True, env=git_env)
    subprocess.run(
        ["git", "-C", str(wd), "push", "-q", str(bare), "main"],
        check=True, env=git_env,
    )
    for t in tags:
        subprocess.run(["git", "-C", str(wd), "tag", t],
                       check=True, env=git_env)
    subprocess.run(["git", "-C", str(wd), "push", "-q", str(bare), "--tags"],
                   check=True, env=git_env)
    return f"file://{bare}"


def run_roll(tmp: Path, repo_dir: str, repo_versions: list[str],
             tags: list[str], **env_over: str) -> subprocess.CompletedProcess:
    """Whole fixture: fake apt repo + fake tag repo + one helper run."""
    base = tmp / "repo"
    make_apt_repo(base, repo_dir, repo_versions)
    nvidia_url = make_tagged_repo(tmp, tags)
    return run_detect(
        "nvidia_driver_detect && echo VER=${NVIDIA_DRIVER_VERSION}",
        NVIDIA_REPO_BASE=f"file://{base}",
        NVIDIA_OPEN_REPO=nvidia_url,
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_CONFIG_SYSTEM="/dev/null",
        **env_over,
    )


def test_pinned_version_is_honored_verbatim() -> None:
    """A preset NVIDIA_DRIVER_VERSION wins over detection: exported VERBATIM
    (Debian revision included — a pin is literal, never re-sorted), with no
    repo or fetch touched (the URLs below point nowhere)."""
    with tempfile.TemporaryDirectory() as td:
        r = run_detect(
            "nvidia_driver_detect && echo VER=${NVIDIA_DRIVER_VERSION}",
            NVIDIA_DRIVER_VERSION="615.71.09-1",
            NVIDIA_REPO_BASE="file:///nonexistent-never-reached",
            NVIDIA_OPEN_REPO="/nonexistent-never-reached",
        )
        assert r.returncode == 0, r.stderr
        assert "pinned 615.71.09-1" in r.stdout
        assert "VER=615.71.09-1" in r.stdout


def test_rolling_takes_the_newest_tagged_candidate() -> None:
    """Candidates walk newest-first; a release WITHOUT an
    open-gpu-kernel-modules tag is skipped (never forced) and the first
    tagged older one wins, its Debian revision stripped."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        r = run_roll(
            tmp, "debian13",
            ["620.70.06-1", "615.71.10-1", "615.71.09-2"],
            ["615.71.10", "615.71.09", "640.10.09"],
        )
        assert r.returncode == 0, r.stderr
        assert "VER=615.71.10" in r.stdout
        assert "620.70.06" not in r.stdout


def test_nvidia_apt_repo_is_an_argument() -> None:
    """NVIDIA_APT_REPO (env) selects the distro subdirectory of the repos
    and the positional argument wins over the env."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        base = tmp / "repo"
        # Two distro indexes: debian10 carries a release debian13 does not —
        # whichever index the helper reads decides the version.
        make_apt_repo(base, "debian10", ["640.10.09-1"])
        make_apt_repo(base, "debian13", ["615.71.10-1"])
        tags, url = ["640.10.09", "615.71.10"], None
        url = make_tagged_repo(tmp, tags)
        common = {
            "NVIDIA_REPO_BASE": f"file://{base}",
            "NVIDIA_OPEN_REPO": url,
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        }
        by_env = run_detect(
            "nvidia_driver_detect && echo VER=${NVIDIA_DRIVER_VERSION}",
            NVIDIA_APT_REPO="debian10", **common,
        )
        assert by_env.returncode == 0, by_env.stderr
        assert "VER=640.10.09" in by_env.stdout
        by_arg = run_detect(
            "nvidia_driver_detect debian13 && echo VER=${NVIDIA_DRIVER_VERSION}",
            NVIDIA_APT_REPO="debian10", **common,
        )
        assert by_arg.returncode == 0, by_arg.stderr
        assert "VER=615.71.10" in by_arg.stdout


def test_default_repo_is_debian13() -> None:
    """No NVIDIA_APT_REPO and no argument = the DEFAULT repo debian13."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        r = run_roll(tmp, "debian13", ["615.71.10-1"], ["615.71.10"])
        assert r.returncode == 0, r.stderr
        assert "VER=615.71.10" in r.stdout
        assert "repo debian13" in r.stdout


def test_unreachable_repo_fails_the_build() -> None:
    """An unreachable Packages index exits 1: the lane fails and keeps the
    last-good deb instead of building a guessed version."""
    r = run_detect(
        "nvidia_driver_detect debian404",
        NVIDIA_REPO_BASE="file:///nonexistent-repo",
        NVIDIA_OPEN_REPO="/nonexistent",
    )
    assert r.returncode == 1
    assert "unreachable" in r.stderr


def test_no_tagged_candidate_fails_the_lane() -> None:
    """Every repo release untagged = abort (exit 1), NEVER a forced untagged
    build — the all-or-nothing pairing gate."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        r = run_roll(
            tmp, "debian13", ["620.70.06-1", "610.57.04-1"],
            ["640.10.09"],
        )
        assert r.returncode == 1, r.stdout
        assert "no release" in r.stderr
