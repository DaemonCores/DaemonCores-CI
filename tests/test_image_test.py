"""Unit tests for ``image-test/image-test.sh`` and ``image-test/lib.sh``.

Everything that needs QEMU, podman or root is out of reach here; what is tested:
sub-command argument parsing, state directory handling, the Secure Boot varstore
fingerprint and cache (virt-fw-vars stubbed), manifest validation, category
selection, and stopping a VM (``sudo``/``ssh`` stubbed).
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

needs_yq = pytest.mark.skipif(
    shutil.which("yq") is None, reason="yq is required"
)

MANIFEST = textwrap.dedent(
    """\
    boot_timeout: 60
    categories:
      - {name: plain, arch: amd64, suffix: "", secure_boot: 1, reference: true}
      - {name: nosign, arch: amd64, suffix: "-nosign", secure_boot: 1}
      - {name: k4, arch: amd64, suffix: "-4k", secure_boot: 0}
      - {name: enforce, arch: amd64, suffix: "-enf", secure_boot: 1,
         kargs: ["module.sig_enforce=1", "quiet"]}
      - {name: lax, arch: amd64, suffix: "-enf", secure_boot: 1}
    tests:
      - {name: a, run: "true", diag: "echo a"}
    """
)


def sh(snippet: str, env: dict[str, str],
       tmp: Path) -> subprocess.CompletedProcess[str]:
    """Run a bash snippet with lib.sh sourced under the harness options.

    Args:
        snippet: Bash snippet run after sourcing lib.sh.
        env: Environment variables set on top of PATH and HOME.
        tmp: Directory holding the stub bin dir and HOME.

    Returns:
        The finished process, with stdout and stderr as text.
    """
    full = {
        "PATH": f"{tmp / 'bin'}:{os.environ['PATH']}",
        "HOME": str(tmp),
        **env
    }
    return subprocess.run(
        ["bash", "-c", f'set -euo pipefail\nsource "{LIB}"\n{snippet}'],
        capture_output=True,
        text=True,
        env=full,
    )


def cli(args: list[str], env: dict[str, str],
        tmp: Path) -> subprocess.CompletedProcess[str]:
    """Run image-test.sh with the given arguments and environment.

    Args:
        args: Arguments passed to image-test.sh.
        env: Environment variables set on top of PATH and HOME.
        tmp: Directory holding the stub bin dir and HOME.

    Returns:
        The finished process, with stdout and stderr as text.
    """
    full = {
        "PATH": f"{tmp / 'bin'}:{os.environ['PATH']}",
        "HOME": str(tmp),
        **env
    }
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=full,
    )


@pytest.fixture
def stubs(tmp_path: Path) -> Path:
    """A bin dir with a pass-through sudo and an ssh that only logs.

    Args:
        tmp_path: Pytest temporary directory.

    Returns:
        The temporary directory holding the stub bin dir.
    """
    b = tmp_path / "bin"
    b.mkdir()
    (b / "sudo").write_text('#!/bin/bash\nexec "$@"\n')
    (b / "ssh").write_text(f'#!/bin/bash\necho "$@" >>"{tmp_path}/ssh.log"\n')
    for f in b.iterdir():
        f.chmod(0o755)
    return tmp_path


def manifest(tmp: Path, text: str = MANIFEST) -> Path:
    """Write a tests manifest file under tmp and return its path.

    Args:
        tmp: Directory the manifest is written under.
        text: Manifest text to write.

    Returns:
        The path of the manifest file.
    """
    p = tmp / "tests.yml"
    p.write_text(text)
    return p


# --- boot-timeout calibration (DEBUG.md:565) --------------------------------


def test_calibrated_timeout_caps_measured_plus_margin(stubs: Path) -> None:
    """The calibrated timeout is measured x margin + SSH grace, max 600.

    Args:
        stubs: Temporary directory holding the stub bin dir.
    """
    r = sh(
        f"""
        BOOT_CALIB_FILE="{stubs}/calib.env"
        printf 'arm64:signed=310\n' >"${{BOOT_CALIB_FILE}}"
        KVM=0
        compute_timeout 60 arm64:signed
        echo "${{CALIBRATED_TIMEOUT}}"
        """,
        {
            "ARCH": "arm64",
            "TCG_TIMEOUT_SCALE": "5"
        },
        stubs,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines()[-1] == "600"


def test_calibrated_timeout_not_scaled_when_calibrated(stubs: Path) -> None:
    """A calibration entry replaces manifest x TCG_TIMEOUT_SCALE.

    Args:
        stubs: Temporary directory holding the stub bin dir.
    """
    r = sh(
        f"""
        BOOT_CALIB_FILE="{stubs}/calib.env"
        printf 'arm64:signed=110\n' >"${{BOOT_CALIB_FILE}}"
        KVM=0
        compute_timeout 60 arm64:signed
        echo "${{CALIBRATED_TIMEOUT}}"
        """,
        {
            "ARCH": "arm64",
            "TCG_TIMEOUT_SCALE": "5"
        },
        stubs,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines(
    )[-1] == "225"  # 110 * 2 + 5, no TCG scale


def test_uncalibrated_timeout_keeps_manifest_and_tcg_scale(stubs: Path) -> None:
    """First run for a key: manifest value, scaled by TCG without KVM.

    Args:
        stubs: Temporary directory holding the stub bin dir.
    """
    r = sh(
        f"""
        BOOT_CALIB_FILE="{stubs}/calib.env"
        BOOT_CALIB_SEED="{stubs}/absent.env"
        KVM=0
        compute_timeout 60 amd64:signed
        echo "${{CALIBRATED_TIMEOUT}}"
        """,
        {
            "ARCH": "amd64",
            "TCG_TIMEOUT_SCALE": "5"
        },
        stubs,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines()[-1] == "300"


def test_uncalibrated_kvm_timeout_is_the_manifest_value(stubs: Path) -> None:
    """Without calibration and with KVM, the manifest value is used as is.

    Args:
        stubs: Temporary directory holding the stub bin dir.
    """
    r = sh(
        f"""
        BOOT_CALIB_FILE="{stubs}/calib.env"
        BOOT_CALIB_SEED="{stubs}/absent.env"
        KVM=1
        compute_timeout 60 amd64:signed
        echo "${{CALIBRATED_TIMEOUT}}"
        """,
        {
            "ARCH": "amd64",
            "TCG_TIMEOUT_SCALE": "5"
        },
        stubs,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines()[-1] == "60"


def test_calibrated_timeout_never_below_manifest(stubs: Path) -> None:
    """The manifest value floors the calibration (KVM boots measured faster).

    Args:
        stubs: Temporary directory holding the stub bin dir.
    """
    r = sh(
        f"""
        BOOT_CALIB_FILE="{stubs}/calib.env"
        printf 'amd64:signed=19\n' >"${{BOOT_CALIB_FILE}}"
        KVM=1
        compute_timeout 60 amd64:signed
        echo "${{CALIBRATED_TIMEOUT}}"
        """,
        {
            "ARCH": "amd64",
            "TCG_TIMEOUT_SCALE": "5"
        },
        stubs,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines()[-1] == "60"


def test_calibrated_timeout_ceiling_600(stubs: Path) -> None:
    """Slow arm64 measured boots cap at 600 s (8b6d023 ceiling kept).

    Args:
        stubs: Temporary directory holding the stub bin dir.
    """
    r = sh(
        f"""
        BOOT_CALIB_FILE="{stubs}/calib.env"
        printf 'arm64:uboot=340\n' >"${{BOOT_CALIB_FILE}}"
        KVM=0
        compute_timeout 60 arm64:uboot
        echo "${{CALIBRATED_TIMEOUT}}"
        """,
        {
            "ARCH": "arm64",
            "TCG_TIMEOUT_SCALE": "5"
        },
        stubs,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines()[-1] == "600"


def test_calibration_seed_used_when_state_file_has_no_entry(
    stubs: Path
) -> None:
    """The committed seed file calibrates the first run after a job restart.

    Args:
        stubs: Temporary directory holding the stub bin dir.
    """
    seed = stubs / "seed.env"
    seed.write_text("amd64:signed=19\n")
    r = sh(
        f"""
        BOOT_CALIB_FILE="{stubs}/calib.env"
        BOOT_CALIB_SEED="{seed}"
        KVM=1
        compute_timeout 60 amd64:signed
        echo "${{CALIBRATED_TIMEOUT}}"
        """,
        {
            "ARCH": "amd64",
            "TCG_TIMEOUT_SCALE": "5"
        },
        stubs,
    )
    assert r.returncode == 0, r.stderr
    # 19 * 2 + 5 = 43 is below the manifest floor of 60.
    assert r.stdout.strip().splitlines()[-1] == "60"


def test_calibration_record_upserts_and_get_reads_it(stubs: Path) -> None:
    """Recording a key twice keeps the last value; unknown keys read empty.

    Args:
        stubs: Temporary directory holding the stub bin dir.
    """
    r = sh(
        f"""
        BOOT_CALIB_FILE="{stubs}/calib.env"
        boot_calib_record amd64:signed 19
        boot_calib_record amd64:signed 21
        boot_calib_record arm64:uboot 105
        g1=$(boot_calib_get amd64:signed)
        g2=$(boot_calib_get arm64:uboot)
        g3=$(boot_calib_get missing:key)
        echo "${{g1}}/${{g2}}/${{g3}}"
        """,
        {"ARCH": "amd64"},
        stubs,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines()[-1] == "21/105/"


def test_calibration_reboot_key_recorded_separately(stubs: Path) -> None:
    """The reboot key is stored apart from the plain boot key.

    Args:
        stubs: Temporary directory holding the stub bin dir.
    """
    r = sh(
        f"""
        BOOT_CALIB_FILE="{stubs}/calib.env"
        boot_calib_record arm64:signed 127
        boot_calib_record arm64:signed:reboot 145
        g1=$(boot_calib_get arm64:signed)
        g2=$(boot_calib_get arm64:signed:reboot)
        echo "${{g1}}/${{g2}}"
        """,
        {"ARCH": "arm64"},
        stubs,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines()[-1] == "127/145"


def test_state_init_touches_the_calibration_file_and_seed_exists(
    stubs: Path
) -> None:
    """Prepare leaves the calibration file ready for record and cache.

    Args:
        stubs: Temporary directory holding the stub bin dir.
    """
    r = sh(
        f"""
        STATE="{stubs}/st"
        ARCH=amd64
        state_init
        [ -f "${{STATE}}/boot-calibration.env" ] && echo ok
        """,
        {},
        stubs,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "ok"
    seed = ROOT / "boot-calibration.env"
    assert seed.is_file(), "the committed boot-calibration.env seed must exist"


def test_seed_entries_are_key_seconds_pairs(stubs: Path) -> None:
    """Every seed entry is a key=seconds pair with numeric seconds.

    Args:
        stubs: Temporary directory holding the stub bin dir.
    """
    for line in (ROOT / "boot-calibration.env").read_text().splitlines():
        if line == "" or line.startswith("#"):
            continue
        k, _, v = line.partition("=")
        assert k, f"bad seed entry: {line}"
        assert v.isdigit(), f"bad seed entry: {line}"


# --- argument parsing ------------------------------------------------------


def test_unknown_subcommand_is_rejected(stubs: Path) -> None:
    """An unknown sub-command exits 2 with an explicit message.

    Args:
        stubs: Temporary directory holding the stub bin dir.
    """
    r = cli(["frobnicate"], {"IMAGE_TEST_STATE": str(stubs / "st")}, stubs)
    assert r.returncode == 2
    assert "unknown sub-command" in r.stderr


@pytest.mark.parametrize(
    "args", [["test"], ["test", ""], ["test", "i", "1", "x"], ["prepare", "x"],
             ["teardown", "x"]]
)
def test_bad_arity_prints_usage(stubs: Path, args: list[str]) -> None:
    """A wrong argument count exits 2 and prints the usage.

    Args:
        stubs: Temporary directory holding the stub bin dir.
        args: Arguments passed to image-test.sh.
    """
    r = cli(args, {"IMAGE_TEST_STATE": str(stubs / "st")}, stubs)
    assert r.returncode == 2
    assert "usage:" in r.stderr


def test_ram_max_must_be_an_integer(stubs: Path) -> None:
    """A non-numeric ram-max is rejected with exit code 2.

    Args:
        stubs: Temporary directory holding the stub bin dir.
    """
    r = cli(["test", "img:latest", "abc"],
            {"IMAGE_TEST_STATE": str(stubs / "st")}, stubs)
    assert r.returncode == 2
    assert "ram-max" in r.stderr


def test_no_subcommand_needs_image(stubs: Path) -> None:
    """Without a sub-command and without IMAGE, exit 2 with a message.

    Args:
        stubs: Temporary directory holding the stub bin dir.
    """
    r = cli([], {"IMAGE_TEST_STATE": str(stubs / "st")}, stubs)
    assert r.returncode == 2
    assert "IMAGE is not set" in r.stderr


def test_test_without_prepare_fails_clearly(stubs: Path) -> None:
    """Running test before prepare fails and points to prepare.

    Args:
        stubs: Temporary directory holding the stub bin dir.
    """
    r = cli(["test", "img:latest", "15"],
            {"IMAGE_TEST_STATE": str(stubs / "st")}, stubs)
    assert r.returncode == 1
    assert "image-test.sh prepare" in r.stdout


def test_help_exits_zero(stubs: Path) -> None:
    """The --help flag exits 0 and lists prepare and teardown.

    Args:
        stubs: Temporary directory holding the stub bin dir.
    """
    r = cli(["--help"], {}, stubs)
    assert r.returncode == 0
    assert "prepare" in r.stdout
    assert "teardown" in r.stdout


# --- state directory -------------------------------------------------------


def test_state_dir_from_env_is_private(stubs: Path) -> None:
    """IMAGE_TEST_STATE is honoured and the directory is mode 700.

    Args:
        stubs: Temporary directory holding the stub bin dir.
    """
    st = stubs / "custom" / "st"
    r = sh(
        "state_init; stat -c %a \"${STATE}\"; ls \"${STATE}\"",
        {"IMAGE_TEST_STATE": str(st)}, stubs
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.split()[0] == "700"
    assert {"logs", "varstore"} <= set(r.stdout.split()[1:])


def test_state_dir_defaults_under_runner_temp(stubs: Path) -> None:
    """Without IMAGE_TEST_STATE, the state lives under RUNNER_TEMP.

    Args:
        stubs: Temporary directory holding the stub bin dir.
    """
    rt = stubs / "rt"
    r = sh("state_init; echo \"${STATE}\"", {"RUNNER_TEMP": str(rt)}, stubs)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == f"{rt}/image-test"
    assert (rt / "image-test" / "logs").is_dir()


def test_state_roundtrip_and_env_wins(stubs: Path) -> None:
    """save_state/load_state round-trip, and the environment wins.

    Args:
        stubs: Temporary directory holding the stub bin dir.
    """
    env = {"IMAGE_TEST_STATE": str(stubs / "st")}
    r = sh(
        'state_init; ARCH=arm64; KVM=0; OVMF_DIR="/a b"; '
        'TESTS_FILE=/m/t.yml; save_state\n'
        'ARCH=amd64; KVM=1; OVMF_DIR=; TESTS_FILE=; load_state\n'
        'echo "${ARCH}|${KVM}|${OVMF_DIR}|${TESTS_FILE}"',
        env,
        stubs,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "arm64|0|/a b|/m/t.yml"
    r = sh(
        'TESTS_FILE=/other.yml; load_state; echo "${TESTS_FILE}"', {
            **env, "ARCH": "amd64"
        }, stubs
    )
    assert r.stdout.strip() == "/other.yml"
    assert sh('echo "${ARCH}"; load_state', {
        **env
    }, stubs).stdout.startswith("amd64")


def test_state_dir_root_is_refused(stubs: Path) -> None:
    """A state directory of / is refused.

    Args:
        stubs: Temporary directory holding the stub bin dir.
    """
    r = sh("state_init", {"IMAGE_TEST_STATE": "/"}, stubs)
    assert r.returncode == 1
    assert "refusing" in r.stdout


# --- varstore fingerprint and cache ---------------------------------------


def certdir(tmp: Path, name: str, crt: str, mok: dict[str, str]) -> Path:
    """Create a cert directory with sb.crt and mok.d entries; return it.

    Args:
        tmp: Directory the certificate directory is created under.
        name: Name of the certificate directory below tmp.
        crt: Contents of sb.crt.
        mok: Map of mok.d file name to its contents.

    Returns:
        The certificate directory.
    """
    d = tmp / name
    (d / "mok.d").mkdir(parents=True)
    (d / "sb.crt").write_text(crt)
    for k, v in mok.items():
        (d / "mok.d" / k).write_text(v)
    return d


def test_fingerprint_tracks_crt_and_mok(stubs: Path) -> None:
    """The fingerprint changes with the cert, MOK files and arch.

    Args:
        stubs: Temporary directory holding the stub bin dir.
    """
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
    other_arch = sh(f'ARCH=arm64; sb_fingerprint "{a}"', {},
                    stubs).stdout.strip()
    assert other_arch != fps["a"]


FAKE_VIRT_FW = """#!/bin/bash
# Stub of virt-fw-vars: records the call, writes the -o file.
while [ $# -gt 0 ]; do [ "$1" = -o ] && out=$2; shift; done
echo build >>"$(dirname "$0")/calls"
echo "varstore" >"$out"
"""


def varstore_env(tmp: Path) -> dict[str, str]:
    """Build a stubbed varstore environment under tmp; return its env.

    Args:
        tmp: Directory the stubbed varstore environment is built under.

    Returns:
        The environment for the stubbed varstore build.
    """
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
    """The varstore is cached and rebuilt only when the certs change.

    Args:
        stubs: Temporary directory holding the stub bin dir.
    """
    env = varstore_env(stubs)
    a = certdir(stubs, "a", "CRT1", {})
    b = certdir(stubs, "b", "CRT2", {})
    calls = stubs / "st" / "sbtool" / "bin" / "calls"
    snippet = (
        'state_init; SB_GUID=g; SB_CERTS=/c\n'
        f'prepare_sb_varstore "{a}"; prepare_sb_varstore "{a}"; '
        f'prepare_sb_varstore "{b}"'
    )
    r = sh(snippet, env, stubs)
    assert r.returncode == 0, r.stderr
    assert calls.read_text().count("build") == 2
    assert r.stdout.count("cache hit") == 1
    assert r.stdout.count("cache miss") == 2


def test_varstore_given_to_vm_is_a_pristine_copy(stubs: Path) -> None:
    """The VM varstore is a fresh copy of the cached one on each run.

    Args:
        stubs: Temporary directory holding the stub bin dir.
    """
    env = varstore_env(stubs)
    a = certdir(stubs, "a", "CRT1", {})
    r = sh(
        'state_init; SB_GUID=g; SB_CERTS=/c\n'
        f'prepare_sb_varstore "{a}"\n'
        'echo "BootOrder-leak" >>"${VARS_RUN}"\n'
        f'prepare_sb_varstore "{a}"\n'
        'cat "${VARS_RUN}"',
        env,
        stubs,
    )
    assert r.returncode == 0, r.stderr
    assert "leak" not in r.stdout.split("cache hit")[-1]
    assert (stubs / "st" / "OVMF_VARS.fd").read_text() == "varstore\n"


def test_varstore_without_tooling_fails(stubs: Path) -> None:
    """Building a varstore without virt-fw-vars fails clearly.

    Args:
        stubs: Temporary directory holding the stub bin dir.
    """
    a = certdir(stubs, "a", "CRT1", {})
    r = sh(
        f'state_init; prepare_sb_varstore "{a}"',
        {"IMAGE_TEST_STATE": str(stubs / "st")}, stubs
    )
    assert r.returncode == 1
    assert "Secure Boot needed" in r.stdout


@pytest.mark.skipif(
    shutil.which("openssl") is None, reason="openssl is required"
)
def test_build_skips_private_keys_and_passes_certs(stubs: Path) -> None:
    """Private keys and non-PEM files are not passed to virt-fw-vars.

    Args:
        stubs: Temporary directory holding the stub bin dir.
    """
    env = varstore_env(stubs)
    d = certdir(stubs, "a", "CRT1", {})
    pem = stubs / "m.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-subj",
            "/CN=t", "-keyout",
            str(stubs / "m.key"), "-out",
            str(pem)
        ],
        check=True,
        capture_output=True,
    )
    (d / "mok.d" / "pub.pem").write_text(pem.read_text())
    (d / "mok.d" / "priv.pem").write_text((stubs / "m.key").read_text())
    (d / "mok.d" / "note.txt").write_text("ignored")
    stub = stubs / "st" / "sbtool" / "bin" / "virt-fw-vars"
    stub.write_text(
        '#!/bin/bash\nprintf "%s\\n" "$@" >"$(dirname "$0")/args"\n'
        'while [ $# -gt 0 ]; do [ "$1" = -o ] && : >"$2"; shift; done\n'
    )
    r = sh(
        f'state_init; SB_GUID=g; SB_CERTS=/c; '
        f'build_sb_vars "{d}" "{stubs}/out.fd"', env, stubs
    )
    assert r.returncode == 0, r.stderr
    args = (stubs / "st" / "sbtool" / "bin" / "args").read_text()
    assert "pub.pem.pem" in args
    assert "priv.pem" not in args
    assert "note.txt" not in args
    assert "--secure-boot" in args


# --- manifest --------------------------------------------------------------


@needs_yq
def test_validate_manifest_accepts_complete_manifest(stubs: Path) -> None:
    """A complete manifest passes validation.

    Args:
        stubs: Temporary directory holding the stub bin dir.
    """
    m = manifest(stubs)
    r = sh("validate_manifest", {"TESTS_FILE": str(m)}, stubs)
    assert r.returncode == 0, r.stdout + r.stderr


@needs_yq
@pytest.mark.parametrize("missing", ["diag", "run", "name"])
def test_validate_manifest_rejects_missing_field(
    stubs: Path, missing: str
) -> None:
    """A test entry missing a required field is rejected.

    Args:
        stubs: Temporary directory holding the stub bin dir.
        missing: Required field left out of the test entry.
    """
    fields = {"name": "a", "run": "true", "diag": "echo a"}
    del fields[missing]
    body = ", ".join(f'{k}: "{v}"' for k, v in fields.items())
    m = manifest(stubs, f"tests:\n  - {{{body}}}\n")
    r = sh("validate_manifest", {"TESTS_FILE": str(m)}, stubs)
    assert r.returncode == 1
    assert "::error::" in r.stdout


@needs_yq
def test_validate_manifest_checks_reboot_tests(stubs: Path) -> None:
    """reboot_tests entries are validated too.

    Args:
        stubs: Temporary directory holding the stub bin dir.
    """
    m = manifest(
        stubs, 'tests:\n  - {name: a, run: x, diag: y}\n'
        'reboot_tests:\n  - {name: b, run: x}\n'
    )
    r = sh("validate_manifest", {"TESTS_FILE": str(m)}, stubs)
    assert r.returncode == 1
    assert "reboot_tests" in r.stdout


@needs_yq
@pytest.mark.parametrize(
    ("tag", "category", "secure"),
    [
        ("img:latest_amd64", "plain", "1"),
        ("img:latest-nosign_amd64", "nosign", "1"),
        ("img:extra-4k_amd64", "k4", "0"),
    ],
)
def test_select_category_secure_boot_per_tag(
    stubs: Path, tag: str, category: str, secure: str
) -> None:
    """The category and Secure Boot flag are derived from the image tag.

    Args:
        stubs: Temporary directory holding the stub bin dir.
        tag: Image tag the category is selected for.
        category: Category expected for the tag, or set in the environment.
        secure: Expected SECURE_BOOT flag, 1 or 0.
    """
    m = manifest(stubs)
    r = sh(
        f'IMAGE="{tag}"; select_category >/dev/null; '
        'echo "${CATEGORY} ${SECURE_BOOT}"',
        {
            "TESTS_FILE": str(m),
            "ARCH": "amd64"
        },
        stubs,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.split() == [category, secure]


@needs_yq
def test_secure_boot_env_overrides_category(stubs: Path) -> None:
    """SECURE_BOOT in the environment overrides the category value.

    Args:
        stubs: Temporary directory holding the stub bin dir.
    """
    m = manifest(stubs)
    r = sh(
        'IMAGE="img:x-4k_amd64"; select_category >/dev/null; '
        'echo "${SECURE_BOOT}"', {
            "TESTS_FILE": str(m),
            "SECURE_BOOT": "1"
        }, stubs
    )
    assert r.stdout.strip().splitlines()[-1] == "1"


@needs_yq
@pytest.mark.parametrize(
    ("env_sb", "arch", "needed"),
    [("", "amd64", 0), ("0", "amd64", 1), ("1", "amd64", 0)],
)
def test_needs_secure_boot(
    stubs: Path, env_sb: str, arch: str, needed: int
) -> None:
    """The manifest needs Secure Boot depending on env and categories.

    Args:
        stubs: Temporary directory holding the stub bin dir.
        env_sb: Value of the SECURE_BOOT variable.
        arch: Architecture the manifest is checked for.
        needed: Expected exit status: 0 when Secure Boot is needed, 1 when not.
    """
    m = manifest(stubs)
    env = {"TESTS_FILE": str(m), "ARCH": arch, "SECURE_BOOT": env_sb}
    r = sh("manifest_needs_secure_boot && echo yes || echo no", env, stubs)
    want = {0: "yes", 1: "no"}[needed]
    assert r.stdout.strip() == want


@needs_yq
def test_no_secure_boot_category_for_arch(stubs: Path) -> None:
    """No Secure Boot is needed when no category enables it.

    Args:
        stubs: Temporary directory holding the stub bin dir.
    """
    m = manifest(
        stubs, "categories:\n"
        "  - {name: p, arch: amd64, suffix: '', secure_boot: 0,"
        " reference: true}\n"
        "tests:\n  - {name: a, run: x, diag: y}\n"
    )
    r = sh(
        "manifest_needs_secure_boot && echo yes || echo no",
        {"TESTS_FILE": str(m)}, stubs
    )
    assert r.stdout.strip() == "no"


# --- kernel args per category ----------------------------------------------


@needs_yq
def test_two_categories_on_one_tag_are_all_listed(stubs: Path) -> None:
    """All categories matching one tag are listed.

    Args:
        stubs: Temporary directory holding the stub bin dir.
    """
    m = manifest(stubs)
    r = sh(
        'IMAGE="img:x-enf_amd64"; list_categories', {
            "TESTS_FILE": str(m),
            "ARCH": "amd64"
        }, stubs
    )
    assert r.stdout.split() == ["enforce", "lax"]


@needs_yq
@pytest.mark.parametrize(
    ("category", "kargs"),
    [("", "module.sig_enforce=1 quiet"),
     ("enforce", "module.sig_enforce=1 quiet"), ("lax", "")],
)
def test_select_category_from_environment_exports_kargs(
    stubs: Path, category: str, kargs: str
) -> None:
    """CATEGORY from the environment selects and exports the kargs.

    Args:
        stubs: Temporary directory holding the stub bin dir.
        category: Category expected for the tag, or set in the environment.
        kargs: Expected INSTALL_KARGS value.
    """
    m = manifest(stubs)
    r = sh(
        'IMAGE="img:x-enf_amd64"; select_category >/dev/null; '
        'echo "${CATEGORY}|${INSTALL_KARGS}"; '
        'bash -c \'echo "exp:${INSTALL_KARGS}"\'',
        {
            "TESTS_FILE": str(m),
            "ARCH": "amd64",
            "CATEGORY": category
        },
        stubs,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    lines = r.stdout.splitlines()
    assert lines[0] == f"{category or 'enforce'}|{kargs}"
    assert lines[1] == f"exp:{kargs}"


@needs_yq
def test_category_not_matching_the_tag_is_rejected(stubs: Path) -> None:
    """A CATEGORY that does not match the tag is rejected.

    Args:
        stubs: Temporary directory holding the stub bin dir.
    """
    m = manifest(stubs)
    r = sh(
        'IMAGE="img:x-enf_amd64"; select_category', {
            "TESTS_FILE": str(m),
            "ARCH": "amd64",
            "CATEGORY": "k4"
        }, stubs
    )
    assert r.returncode == 1
    assert 'no category "k4"' in r.stdout


@needs_yq
@pytest.mark.parametrize(
    "karg", ["a b", "x;rm", "$(id)", "=v", "a=b c", "a='b'"]
)
def test_bad_karg_is_rejected(stubs: Path, karg: str) -> None:
    """Malformed kernel arguments make the manifest invalid.

    Args:
        stubs: Temporary directory holding the stub bin dir.
        karg: Malformed kernel argument put in the category.
    """
    m = manifest(
        stubs,
        "categories:\n"
        "  - {name: p, arch: amd64, suffix: '', secure_boot: 0,"
        ' reference: true, kargs: ["' + karg + '"]}\n'
        "tests:\n  - {name: a, run: x, diag: y}\n",
    )
    r = sh("validate_manifest", {"TESTS_FILE": str(m)}, stubs)
    assert r.returncode == 1
    assert "kargs must be a list" in r.stdout


@needs_yq
def test_kargs_must_be_a_list(stubs: Path) -> None:
    """A scalar kargs value makes the manifest invalid.

    Args:
        stubs: Temporary directory holding the stub bin dir.
    """
    m = manifest(
        stubs,
        "categories:\n"
        "  - {name: p, arch: amd64, suffix: '', secure_boot: 0,"
        " reference: true, kargs: quiet}\n"
        "tests:\n  - {name: a, run: x, diag: y}\n",
    )
    r = sh("validate_manifest", {"TESTS_FILE": str(m)}, stubs)
    assert r.returncode == 1


@needs_yq
@pytest.mark.parametrize(("second_name", "ok"), [("other", True), ("p", False)])
def test_duplicate_rule_is_arch_suffix_name(
    stubs: Path, second_name: str, ok: bool
) -> None:
    """Categories sharing arch, suffix and name are duplicates.

    Args:
        stubs: Temporary directory holding the stub bin dir.
        second_name: Name of the second category.
        ok: Whether the manifest is expected to validate.
    """
    m = manifest(
        stubs,
        "categories:\n"
        "  - {name: p, arch: amd64, suffix: '', secure_boot: 0,"
        " reference: true}\n"
        f"  - {{name: {second_name}, arch: amd64, suffix: '',"
        " secure_boot: 0}\n"
        "tests:\n  - {name: a, run: x, diag: y}\n",
    )
    r = sh("validate_manifest", {"TESTS_FILE": str(m)}, stubs)
    assert (r.returncode == 0) is ok, r.stdout + r.stderr
    if not ok:
        assert "duplicate (arch:suffix:name)" in r.stdout


# --- stopping the VM -------------------------------------------------------


def test_stop_vm_without_pidfile_is_a_noop(stubs: Path) -> None:
    """stop_vm without a pidfile succeeds and does nothing.

    Args:
        stubs: Temporary directory holding the stub bin dir.
    """
    r = sh(
        "state_init; stop_vm", {"IMAGE_TEST_STATE": str(stubs / "st")}, stubs
    )
    assert r.returncode == 0, r.stderr


def test_stop_vm_kills_a_guest_that_ignores_poweroff(stubs: Path) -> None:
    """A guest ignoring poweroff is killed after the grace period.

    Args:
        stubs: Temporary directory holding the stub bin dir.
    """
    st = stubs / "st"
    st.mkdir()
    proc = subprocess.Popen(["sleep", "60"])
    try:
        (st / "qemu.pid").write_text(str(proc.pid))
        r = sh(
            "stop_vm", {
                "IMAGE_TEST_STATE": str(st),
                "STOP_GRACE_S": "1"
            }, stubs
        )
        assert r.returncode == 0, r.stderr
        assert "poweroff requested" in r.stdout
        assert "killing QEMU" in r.stdout
        assert proc.wait(timeout=10) != 0
        assert not (st / "qemu.pid").exists()
        assert "poweroff" in (stubs / "ssh.log").read_text()
    finally:
        proc.kill()


def test_free_ssh_port_waits_then_fails_when_taken(stubs: Path) -> None:
    """free_ssh_port fails while the port is taken, passes once free.

    Args:
        stubs: Temporary directory holding the stub bin dir.
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = str(s.getsockname()[1])
        r = sh("free_ssh_port 1", {"SSH_PORT": port}, stubs)
        assert r.returncode == 1
        assert "still in use" in r.stdout
    assert sh("free_ssh_port 1", {"SSH_PORT": port}, stubs).returncode == 0


def test_teardown_keeps_logs_and_removes_secrets(stubs: Path) -> None:
    """Teardown keeps the logs and removes keys, disks and varstores.

    Args:
        stubs: Temporary directory holding the stub bin dir.
    """
    st = stubs / "st"
    r = sh(
        'state_init; touch "${KEY}" "${KEY}.pub" "${STATE}/PK.key" "${DISK}" '
        '"${LOGS}/i.serial.log"\n'
        'mkdir -p "${VARSTORES}/x"; cmd_teardown',
        {"IMAGE_TEST_STATE": str(st)},
        stubs,
    )
    assert r.returncode == 0, r.stderr
    assert (st / "logs" / "i.serial.log").exists()
    assert not any(
        (st / n).exists() for n in
        ("test-key", "test-key.pub", "PK.key", "test-disk.raw", "varstore")
    )
