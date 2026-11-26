"""Tests for the advisory gate, the CISA KEV feed and the adoption pipeline."""

import json
import os
import subprocess
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
ECO = "Debian:13"


def _osv(
    directory: Path,
    source: str,
    version: str,
    ids: list[str],
    aliases: dict[str, list[str]] | None = None
) -> None:
    """Write the fixture answer of OSV for one source package version.

    Args:
        directory: Fixture directory.
        source: Source package name.
        version: Version asked.
        ids: Record ids the answer lists.
        aliases: Aliases per record id.
    """
    aliases = aliases or {}
    vulns = [{"id": i, "aliases": aliases.get(i, [])} for i in ids]
    target = directory / f"{source}_{version}.json"
    _ = target.write_text(json.dumps({"vulns": vulns}))


def _kev(path: Path, cves: list[str]) -> Path:
    """Write a CISA KEV fixture.

    Args:
        path: Fixture file.
        cves: CVE ids listed as exploited.

    Returns:
        The fixture path.
    """
    _ = path.write_text(
        json.dumps({"vulnerabilities": [{
            "cveID": c
        } for c in cves]})
    )
    return path


def _env(osv: Path, kev: Path) -> dict[str, str]:
    """Build the offline environment of the scripts.

    Args:
        osv: OSV fixture directory.
        kev: KEV fixture file.

    Returns:
        The environment mapping.
    """
    return {
        "PATH": os.environ["PATH"],
        "SECURITY_OSV_FIXTURE_DIR": str(osv),
        "SECURITY_KEV_FIXTURE": str(kev),
    }


def _run(script: str, args: list[str], stdin: str,
         env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run one script.

    Args:
        script: Script file name under scripts/.
        args: Script arguments.
        stdin: Standard input.
        env: Environment.

    Returns:
        The completed process.
    """
    return subprocess.run(["bash", str(SCRIPTS / script), *args],
                          input=stdin,
                          capture_output=True,
                          text=True,
                          check=True,
                          env=env)


def _srcmap(tmp: Path) -> Path:
    """Write the binary to source map.

    Args:
        tmp: Directory.

    Returns:
        The map file.
    """
    path = tmp / "srcmap"
    _ = path.write_text("libcurl4t64 curl\n")
    return path


def test_kev_fix_makes_the_package_urgent(tmp_path: Path) -> None:
    """An upgrade fixing a KEV listed CVE skips the care delay.

    Args:
        tmp_path: Pytest temporary directory.
    """
    _osv(tmp_path, "curl", "1-1", ["DSA-1"], {"DSA-1": ["CVE-2025-1"]})
    _osv(tmp_path, "curl", "1-2", [])
    kev = _kev(tmp_path / "kev.json", ["CVE-2025-1"])
    out = _run(
        "security-urgent.sh", [ECO, str(_srcmap(tmp_path))],
        "libcurl4t64 1-1 1-2\n", _env(tmp_path, kev)
    ).stdout
    assert out == "libcurl4t64\n"


def test_unlisted_fix_is_not_urgent(tmp_path: Path) -> None:
    """A fixed CVE absent from KEV keeps the care delay.

    Args:
        tmp_path: Pytest temporary directory.
    """
    _osv(tmp_path, "curl", "1-1", ["DSA-1"], {"DSA-1": ["CVE-2025-1"]})
    _osv(tmp_path, "curl", "1-2", [])
    kev = _kev(tmp_path / "kev.json", ["CVE-2024-9"])
    out = _run(
        "security-urgent.sh", [ECO, str(_srcmap(tmp_path))],
        "libcurl4t64 1-1 1-2\n", _env(tmp_path, kev)
    ).stdout
    assert out == ""


def test_still_open_kev_cve_is_not_a_fix(tmp_path: Path) -> None:
    """A KEV CVE that the new version still carries proves no fix.

    Args:
        tmp_path: Pytest temporary directory.
    """
    for ver in ("1-1", "1-2"):
        _osv(tmp_path, "curl", ver, ["DSA-1"], {"DSA-1": ["CVE-2025-1"]})
    kev = _kev(tmp_path / "kev.json", ["CVE-2025-1"])
    out = _run(
        "security-urgent.sh", [ECO, str(_srcmap(tmp_path))],
        "libcurl4t64 1-1 1-2\n", _env(tmp_path, kev)
    ).stdout
    assert out == ""


def test_advisory_holds_and_tracker_noise_does_not(tmp_path: Path) -> None:
    """An advisory naming the new version holds it; tracker records do not.

    Args:
        tmp_path: Pytest temporary directory.
    """
    _osv(tmp_path, "curl", "1-2", ["GHSA-xxxx-yyyy-zzzz", "DEBIAN-CVE-2025-2"])
    _ = (tmp_path / "srcmap2").write_text("zlib1g zlib\n")
    _osv(tmp_path, "zlib", "2-2", ["DEBIAN-CVE-2025-3"])
    held = tmp_path / "held"
    out = _run(
        "security-advisory.sh",
        [ECO, str(_srcmap(tmp_path)), str(held)], "libcurl4t64 1-1 1-2\n",
        _env(tmp_path, tmp_path / "k")
    ).stdout
    assert out == ""
    assert held.read_text() == "libcurl4t64 1-1 1-2 GHSA-xxxx-yyyy-zzzz\n"
    out = _run(
        "security-advisory.sh", [ECO, str(tmp_path / "srcmap2"),
                                 str(held)], "zlib1g 2-1 2-2\n",
        _env(tmp_path, tmp_path / "k")
    ).stdout
    assert out == "zlib1g 2-1 2-2\n"
    assert held.read_text() == ""


def test_unreachable_osv_passes_loudly(tmp_path: Path) -> None:
    """Without an OSV answer the upgrade passes and the warning is logged.

    Args:
        tmp_path: Pytest temporary directory.
    """
    held = tmp_path / "held"
    proc = _run(
        "security-advisory.sh",
        [ECO, str(_srcmap(tmp_path)), str(held)], "libcurl4t64 1-1 1-2\n",
        _env(tmp_path, tmp_path / "k")
    )
    assert proc.stdout == "libcurl4t64 1-1 1-2\n"
    assert "unreachable" in proc.stdout + proc.stderr


def test_adopt_pipeline_orders_gate_urgent_and_care(tmp_path: Path) -> None:
    """Gate drops, KEV skips the delay, the care delay holds the rest.

    Args:
        tmp_path: Pytest temporary directory.
    """
    _ = (tmp_path / "srcmap").write_text("a a\nb b\nc c\n")
    _osv(tmp_path, "a", "1", ["DSA-1"], {"DSA-1": ["CVE-2025-1"]})
    _osv(tmp_path, "a", "2", [])
    _osv(tmp_path, "b", "1", [])
    _osv(tmp_path, "b", "2", [])
    _osv(tmp_path, "c", "1", [])
    _osv(tmp_path, "c", "2", ["GHSA-aaaa-bbbb-cccc"])
    kev = _kev(tmp_path / "kev.json", ["CVE-2025-1"])
    out_dir = tmp_path / "out"
    out = _run(
        "security-adopt.sh", [
            ECO,
            str(tmp_path / "srcmap"),
            str(tmp_path / "ledger"), "24",
            str(out_dir)
        ], "a 1 2\nb 1 2\nc 1 2\n", _env(tmp_path, kev)
    ).stdout
    assert out == "a 1 2\n"
    report = (out_dir / "report.md").read_text()
    assert "- a" in report
    assert "- b 1 -> 2" in report
    assert "c 1 -> 2: GHSA-aaaa-bbbb-cccc" in report
