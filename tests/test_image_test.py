"""Unit tests for ``image-test/image-test.sh`` and ``image-test/lib.sh``.

Everything that needs QEMU, podman or root is out of reach here; what is
tested: sub-command argument parsing, state directory handling, the Secure
Boot varstore fingerprint and cache (virt-fw-vars stubbed), manifest
validation, category selection, and stopping a VM (``sudo``/``ssh`` stubbed).
"""

import os
import shutil
import socket
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "image-test"
SCRIPT = ROOT / "image-test.sh"
LIB = ROOT / "lib.sh"

needs_yq = pytest.mark.skipif(shutil.which("yq") is None, reason="yq is required")

MANIFEST = textwrap.dedent(
    """\
    boot_timeout: 60
    categories:
      - {name: plain, arch: amd64, suffix: "", secure_boot: 1, reference: true}
      - {name: nosign, arch: amd64, suffix: "-nosign", secure_boot: 1}
      - {name: k4, arch: amd64, suffix: "-4k", secure_boot: 0}
    tests:
      - {name: a, run: "true", diag: "echo a"}
    """
)


def sh(snippet: str, env: dict[str, str], tmp: Path) -> subprocess.CompletedProcess[str]:
    """Run a bash snippet with lib.sh sourced under the harness's own options."""
    full = {"PATH": f"{tmp / 'bin'}:{os.environ['PATH']}", "HOME": str(tmp), **env}
    return subprocess.run(
        ["bash", "-c", f'set -euo pipefail\nsource "{LIB}"\n{snippet}'],
        capture_output=True, text=True, env=full,
    )


def cli(args: list[str], env: dict[str, str], tmp: Path) -> subprocess.CompletedProcess[str]:
    full = {"PATH": f"{tmp / 'bin'}:{os.environ['PATH']}", "HOME": str(tmp), **env}
    return subprocess.run(
        ["bash", str(SCRIPT), *args], capture_output=True, text=True, env=full,
    )


@pytest.fixture
def stubs(tmp_path: Path) -> Path:
    """A bin dir with a pass-through sudo and an ssh that only logs."""
    b = tmp_path / "bin"
    b.mkdir()
    (b / "sudo").write_text('#!/bin/bash\nexec "$@"\n')
    (b / "ssh").write_text(f'#!/bin/bash\necho "$@" >>"{tmp_path}/ssh.log"\n')
    for f in b.iterdir():
        f.chmod(0o755)
    return tmp_path


def manifest(tmp: Path, text: str = MANIFEST) -> Path:
    p = tmp / "tests.yml"
    p.write_text(text)
    return p


# --- argument parsing ------------------------------------------------------


def test_unknown_subcommand_is_rejected(stubs: Path) -> None:
    r = cli(["frobnicate"], {"IMAGE_TEST_STATE": str(stubs / "st")}, stubs)
    assert r.returncode == 2
    assert "unknown sub-command" in r.stderr


@pytest.mark.parametrize("args", [["test"], ["test", ""], ["test", "i", "1", "x"], ["prepare", "x"], ["teardown", "x"]])
def test_bad_arity_prints_usage(stubs: Path, args: list[str]) -> None:
    r = cli(args, {"IMAGE_TEST_STATE": str(stubs / "st")}, stubs)
    assert r.returncode == 2
    assert "usage:" in r.stderr


def test_ram_max_must_be_an_integer(stubs: Path) -> None:
    r = cli(["test", "img:latest", "abc"], {"IMAGE_TEST_STATE": str(stubs / "st")}, stubs)
    assert r.returncode == 2
    assert "ram-max" in r.stderr


def test_no_subcommand_needs_image(stubs: Path) -> None:
    r = cli([], {"IMAGE_TEST_STATE": str(stubs / "st")}, stubs)
    assert r.returncode == 2
    assert "IMAGE is not set" in r.stderr


def test_test_without_prepare_fails_clearly(stubs: Path) -> None:
    r = cli(["test", "img:latest", "15"], {"IMAGE_TEST_STATE": str(stubs / "st")}, stubs)
    assert r.returncode == 1
    assert "image-test.sh prepare" in r.stdout


def test_help_exits_zero(stubs: Path) -> None:
    r = cli(["--help"], {}, stubs)
    assert r.returncode == 0
    assert "prepare" in r.stdout and "teardown" in r.stdout


# --- state directory -------------------------------------------------------


def test_state_dir_from_env_is_private(stubs: Path) -> None:
    st = stubs / "custom" / "st"
    r = sh("state_init; stat -c %a \"${STATE}\"; ls \"${STATE}\"", {"IMAGE_TEST_STATE": str(st)}, stubs)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split()[0] == "700"
    assert {"logs", "varstore"} <= set(r.stdout.split()[1:])


def test_state_dir_defaults_under_runner_temp(stubs: Path) -> None:
    rt = stubs / "rt"
    r = sh("state_init; echo \"${STATE}\"", {"RUNNER_TEMP": str(rt)}, stubs)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == f"{rt}/image-test"
    assert (rt / "image-test" / "logs").is_dir()


def test_state_roundtrip_and_env_wins(stubs: Path) -> None:
    env = {"IMAGE_TEST_STATE": str(stubs / "st")}
    r = sh(
        'state_init; ARCH=arm64; KVM=0; OVMF_DIR="/a b"; TESTS_FILE=/m/t.yml; save_state\n'
        'ARCH=amd64; KVM=1; OVMF_DIR=; TESTS_FILE=; load_state\n'
        'echo "${ARCH}|${KVM}|${OVMF_DIR}|${TESTS_FILE}"',
        env, stubs,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "arm64|0|/a b|/m/t.yml"
    r = sh('TESTS_FILE=/other.yml; load_state; echo "${TESTS_FILE}"', {**env, "ARCH": "amd64"}, stubs)
    assert r.stdout.strip() == "/other.yml"
    assert sh('echo "${ARCH}"; load_state', {**env}, stubs).stdout.startswith("amd64")


def test_state_dir_root_is_refused(stubs: Path) -> None:
    r = sh("state_init", {"IMAGE_TEST_STATE": "/"}, stubs)
    assert r.returncode == 1
    assert "refusing" in r.stdout


# --- varstore fingerprint and cache ---------------------------------------


def certdir(tmp: Path, name: str, crt: str, mok: dict[str, str]) -> Path:
    d = tmp / name
    (d / "mok.d").mkdir(parents=True)
    (d / "sb.crt").write_text(crt)
    for k, v in mok.items():
        (d / "mok.d" / k).write_text(v)
    return d


def test_fingerprint_tracks_crt_and_mok(stubs: Path) -> None:
    a = certdir(stubs, "a", "CRT1", {"x.pem": "X"})
    same = certdir(stubs, "same", "CRT1", {"x.pem": "X"})
    crt = certdir(stubs, "crt", "CRT2", {"x.pem": "X"})
    mok = certdir(stubs, "mok", "CRT1", {"x.pem": "Y"})
    extra = certdir(stubs, "extra", "CRT1", {"x.pem": "X", "z.pem": "Z"})
    nomok = certdir(stubs, "nomok", "CRT1", {})
    fps = {}
    for d in (a, same, crt, mok, extra, nomok):
        r = sh(f'sb_fingerprint "{d}"', {}, stubs)
        assert r.returncode == 0, r.stderr
        fps[d.name] = r.stdout.strip()
    assert fps["a"] == fps["same"]
    assert len({fps[k] for k in ("a", "crt", "mok", "extra", "nomok")}) == 5
    other_arch = sh(f'ARCH=arm64; sb_fingerprint "{a}"', {}, stubs).stdout.strip()
    assert other_arch != fps["a"]


FAKE_VIRT_FW = """#!/bin/bash
# Stub of virt-fw-vars: records the call, writes the -o file.
while [ $# -gt 0 ]; do [ "$1" = -o ] && out=$2; shift; done
echo build >>"$(dirname "$0")/calls"
echo "varstore" >"$out"
"""


def varstore_env(tmp: Path) -> dict[str, str]:
    st = tmp / "st"
    ovmf = tmp / "ovmf"
    ovmf.mkdir()
    (ovmf / "OVMF_VARS_4M.fd").write_text("blank")
    venv = st / "sbtool" / "bin"
    venv.mkdir(parents=True)
    (venv / "virt-fw-vars").write_text(FAKE_VIRT_FW)
    (venv / "virt-fw-vars").chmod(0o755)
    return {"IMAGE_TEST_STATE": str(st), "OVMF_DIR": str(ovmf)}


def test_varstore_cache_hit_then_rebuild_on_change(stubs: Path) -> None:
    env = varstore_env(stubs)
    a = certdir(stubs, "a", "CRT1", {})
    b = certdir(stubs, "b", "CRT2", {})
    calls = stubs / "st" / "sbtool" / "bin" / "calls"
    snippet = (
        'state_init; SB_GUID=g; SB_CERTS=/c\n'
        f'prepare_sb_varstore "{a}"; prepare_sb_varstore "{a}"; prepare_sb_varstore "{b}"'
    )
    r = sh(snippet, env, stubs)
    assert r.returncode == 0, r.stderr
    assert calls.read_text().count("build") == 2
    assert r.stdout.count("cache hit") == 1 and r.stdout.count("cache miss") == 2


def test_varstore_given_to_vm_is_a_pristine_copy(stubs: Path) -> None:
    env = varstore_env(stubs)
    a = certdir(stubs, "a", "CRT1", {})
    r = sh(
        'state_init; SB_GUID=g; SB_CERTS=/c\n'
        f'prepare_sb_varstore "{a}"\n'
        'echo "BootOrder-leak" >>"${VARS_RUN}"\n'
        f'prepare_sb_varstore "{a}"\n'
        'cat "${VARS_RUN}"',
        env, stubs,
    )
    assert r.returncode == 0, r.stderr
    assert "leak" not in r.stdout.split("cache hit")[-1]
    assert (stubs / "st" / "OVMF_VARS.fd").read_text() == "varstore\n"


def test_varstore_without_tooling_fails(stubs: Path) -> None:
    a = certdir(stubs, "a", "CRT1", {})
    r = sh(f'state_init; prepare_sb_varstore "{a}"', {"IMAGE_TEST_STATE": str(stubs / "st")}, stubs)
    assert r.returncode == 1
    assert "Secure Boot needed" in r.stdout


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl is required")
def test_build_skips_private_keys_and_passes_certs(stubs: Path) -> None:
    env = varstore_env(stubs)
    d = certdir(stubs, "a", "CRT1", {})
    pem = stubs / "m.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=t",
         "-keyout", str(stubs / "m.key"), "-out", str(pem)],
        check=True, capture_output=True,
    )
    (d / "mok.d" / "pub.pem").write_text(pem.read_text())
    (d / "mok.d" / "priv.pem").write_text((stubs / "m.key").read_text())
    (d / "mok.d" / "note.txt").write_text("ignored")
    stub = stubs / "st" / "sbtool" / "bin" / "virt-fw-vars"
    stub.write_text('#!/bin/bash\nprintf "%s\\n" "$@" >"$(dirname "$0")/args"\nwhile [ $# -gt 0 ]; do [ "$1" = -o ] && : >"$2"; shift; done\n')
    r = sh(f'state_init; SB_GUID=g; SB_CERTS=/c; build_sb_vars "{d}" "{stubs}/out.fd"', env, stubs)
    assert r.returncode == 0, r.stderr
    args = (stubs / "st" / "sbtool" / "bin" / "args").read_text()
    assert "pub.pem.pem" in args
    assert "priv.pem" not in args and "note.txt" not in args
    assert "--secure-boot" in args


# --- manifest --------------------------------------------------------------


@needs_yq
def test_validate_manifest_accepts_complete_manifest(stubs: Path) -> None:
    m = manifest(stubs)
    r = sh("validate_manifest", {"TESTS_FILE": str(m)}, stubs)
    assert r.returncode == 0, r.stdout + r.stderr


@needs_yq
@pytest.mark.parametrize("missing", ["diag", "run", "name"])
def test_validate_manifest_rejects_missing_field(stubs: Path, missing: str) -> None:
    fields = {"name": "a", "run": "true", "diag": "echo a"}
    del fields[missing]
    body = ", ".join(f'{k}: "{v}"' for k, v in fields.items())
    m = manifest(stubs, f"tests:\n  - {{{body}}}\n")
    r = sh("validate_manifest", {"TESTS_FILE": str(m)}, stubs)
    assert r.returncode == 1
    assert "::error::" in r.stdout


@needs_yq
def test_validate_manifest_checks_reboot_tests(stubs: Path) -> None:
    m = manifest(stubs, 'tests:\n  - {name: a, run: x, diag: y}\nreboot_tests:\n  - {name: b, run: x}\n')
    r = sh("validate_manifest", {"TESTS_FILE": str(m)}, stubs)
    assert r.returncode == 1
    assert "reboot_tests" in r.stdout


@needs_yq
@pytest.mark.parametrize(
    ("tag", "category", "secure"),
    [
        ("img:latest_amd64", "plain", "1"),
        ("img:latest-nosign_amd64", "nosign", "1"),
        ("img:minimal-4k_amd64", "k4", "0"),
    ],
)
def test_select_category_secure_boot_per_tag(stubs: Path, tag: str, category: str, secure: str) -> None:
    m = manifest(stubs)
    r = sh(
        f'IMAGE="{tag}"; select_category >/dev/null; echo "${{CATEGORY}} ${{SECURE_BOOT}}"',
        {"TESTS_FILE": str(m), "ARCH": "amd64"}, stubs,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.split() == [category, secure]


@needs_yq
def test_secure_boot_env_overrides_category(stubs: Path) -> None:
    m = manifest(stubs)
    r = sh('IMAGE="img:x-4k_amd64"; select_category >/dev/null; echo "${SECURE_BOOT}"',
           {"TESTS_FILE": str(m), "SECURE_BOOT": "1"}, stubs)
    assert r.stdout.strip() == "1"


@needs_yq
@pytest.mark.parametrize(
    ("env_sb", "arch", "needed"),
    [("", "amd64", 0), ("0", "amd64", 1), ("1", "amd64", 0)],
)
def test_needs_secure_boot(stubs: Path, env_sb: str, arch: str, needed: int) -> None:
    m = manifest(stubs)
    env = {"TESTS_FILE": str(m), "ARCH": arch, "SECURE_BOOT": env_sb}
    r = sh("manifest_needs_secure_boot && echo yes || echo no", env, stubs)
    want = {0: "yes", 1: "no"}[needed]
    assert r.stdout.strip() == want


@needs_yq
def test_no_secure_boot_category_for_arch(stubs: Path) -> None:
    m = manifest(stubs, "categories:\n  - {name: p, arch: amd64, suffix: '', secure_boot: 0, reference: true}\n"
                        "tests:\n  - {name: a, run: x, diag: y}\n")
    r = sh("manifest_needs_secure_boot && echo yes || echo no", {"TESTS_FILE": str(m)}, stubs)
    assert r.stdout.strip() == "no"


# --- stopping the VM -------------------------------------------------------


def test_stop_vm_without_pidfile_is_a_noop(stubs: Path) -> None:
    r = sh("state_init; stop_vm", {"IMAGE_TEST_STATE": str(stubs / "st")}, stubs)
    assert r.returncode == 0, r.stderr


def test_stop_vm_kills_a_guest_that_ignores_poweroff(stubs: Path) -> None:
    st = stubs / "st"
    st.mkdir()
    proc = subprocess.Popen(["sleep", "60"])
    try:
        (st / "qemu.pid").write_text(str(proc.pid))
        r = sh("stop_vm", {"IMAGE_TEST_STATE": str(st), "STOP_GRACE_S": "1"}, stubs)
        assert r.returncode == 0, r.stderr
        assert "poweroff requested" in r.stdout and "killing QEMU" in r.stdout
        assert proc.wait(timeout=10) != 0
        assert not (st / "qemu.pid").exists()
        assert "poweroff" in (stubs / "ssh.log").read_text()
    finally:
        proc.kill()


def test_free_ssh_port_waits_then_fails_when_taken(stubs: Path) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = str(s.getsockname()[1])
        r = sh("free_ssh_port 1", {"SSH_PORT": port}, stubs)
        assert r.returncode == 1
        assert "still in use" in r.stdout
    assert sh("free_ssh_port 1", {"SSH_PORT": port}, stubs).returncode == 0


def test_teardown_keeps_logs_and_removes_secrets(stubs: Path) -> None:
    st = stubs / "st"
    r = sh(
        'state_init; touch "${KEY}" "${KEY}.pub" "${STATE}/PK.key" "${DISK}" "${LOGS}/i.serial.log"\n'
        'mkdir -p "${VARSTORES}/x"; cmd_teardown',
        {"IMAGE_TEST_STATE": str(st)}, stubs,
    )
    assert r.returncode == 0, r.stderr
    assert (st / "logs" / "i.serial.log").exists()
    assert not any((st / n).exists() for n in ("test-key", "test-key.pub", "PK.key", "test-disk.raw", "varstore"))
