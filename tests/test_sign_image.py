import os
import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "sign-image" / "sign.sh"
DIGEST = "sha256:" + "a" * 64


def run(tmp_path, refs, key=""):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "calls.log"
    stub = bindir / "cosign"
    stub.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\n')
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    env = {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "REFS": refs,
        "COSIGN_PRIVATE_KEY": key,
    }
    r = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)
    calls = log.read_text().splitlines() if log.exists() else []
    return r, calls


def test_signs_each_ref_lowercased(tmp_path):
    r, calls = run(tmp_path, f"GHCR.io/Org/Img@{DIGEST}\n\nghcr.io/org/two@{DIGEST}\n", key="k")
    assert r.returncode == 0
    assert calls == [
        f"sign --yes --key env://COSIGN_PRIVATE_KEY ghcr.io/org/img@{DIGEST}",
        f"sign --yes --key env://COSIGN_PRIVATE_KEY ghcr.io/org/two@{DIGEST}",
    ]


def test_keyless_warns(tmp_path):
    r, calls = run(tmp_path, f"ghcr.io/org/img@{DIGEST}")
    assert r.returncode == 0
    assert "signing keyless" in r.stdout
    assert calls == [f"sign --yes ghcr.io/org/img@{DIGEST}"]


def test_unpinned_ref_rejected(tmp_path):
    r, calls = run(tmp_path, "ghcr.io/org/img:latest", key="k")
    assert r.returncode == 1
    assert calls == []
