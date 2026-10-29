"""Unit tests for ``gateway-reconcile.sh`` (NOTES bloc -> dynamic.yml)."""

import json
import subprocess
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "gateway-reconcile.sh"
)

BLOC_OK = (
    "Free prose of the human.\n"
    "<!--gateway\n"
    "forge: rule=PathPrefix(`/forge`); service=http://10.0.0.120:3000; "
    "entrypoints=websecure; tls=true\n"
    "portfolio: rule=Host(`example.test`); service=http://10.0.0.120:14321\n"
    "gateway-->\n"
    "More prose below, untouched.\n"
)


def run(conf_dir: Path, out: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), "testnode"],
        capture_output=True, text=True,
        env={"GW_CONF_DIR": str(conf_dir), "GW_OUT": str(out), "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )


def make_conf(tmp_path: Path, vmid: int, description: str) -> Path:
    d = tmp_path / "conf"
    d.mkdir(exist_ok=True)
    (d / f"{vmid}.conf").write_text(
        "description: " + description.replace("\n", "\\n") + "\n"
    )
    return d


def test_renders_routers_and_ids_by_ct(tmp_path: Path) -> None:
    conf = make_conf(tmp_path, 120, BLOC_OK)
    out = tmp_path / "dynamic.yml"
    r = run(conf, out)
    assert r.returncode == 0, r.stderr
    text = out.read_text()
    assert "ct120_forge:" in text and "ct120_portfolio:" in text
    assert 'rule: "PathPrefix(`/forge`)"' in text
    assert "- websecure" in text
    assert "tls: {}" in text
    assert 'url: "http://10.0.0.120:3000"' in text
    # prose never leaks into the yaml
    assert "Free prose" not in text


def test_skips_broken_router_but_keeps_valid_ones_exit4(tmp_path: Path) -> None:
    conf = make_conf(
        tmp_path, 122,
        "<!--gateway\nbroken line no separators\ngateway-->",
    )
    make_conf(tmp_path, 120, BLOC_OK)
    out = tmp_path / "dynamic.yml"
    r = run(conf, out)
    assert r.returncode == 4, r.stderr
    text = out.read_text()
    assert "ct120_forge:" in text
    assert "ct122_" not in text
    assert "invalid or unparsable" in r.stderr


def test_idempotent_no_rewrite_when_unchanged(tmp_path: Path) -> None:
    conf = make_conf(tmp_path, 120, BLOC_OK)
    out = tmp_path / "dynamic.yml"
    assert run(conf, out).returncode == 0
    first = out.read_bytes()
    r = run(conf, out)
    assert r.returncode == 0
    assert "unchanged" in r.stderr
    assert out.read_bytes() == first


def test_guest_without_bloc_writes_empty_header(tmp_path: Path) -> None:
    conf = make_conf(tmp_path, 121, "plain prose, nothing else")
    out = tmp_path / "dynamic.yml"
    r = run(conf, out)
    assert r.returncode == 0, r.stderr
    assert out.read_text().startswith("# gateway-reconcile: no gateway NOTES bloc found")


def test_variant_b_yaml_bloc_is_refused(tmp_path: Path) -> None:
    conf = make_conf(
        tmp_path, 120,
        "<!--gateway\n```yaml\nrouters:\n  forge:\n    rule: Host(`x`)\n"
        "``` \ngateway-->",
    )
    out = tmp_path / "dynamic.yml"
    r = run(conf, out)
    assert r.returncode == 4, r.stderr
    assert "variant B" in r.stderr


def test_middlewares_list_rendered(tmp_path: Path) -> None:
    conf = make_conf(
        tmp_path, 130,
        "<!--gateway\nmail: rule=Host(`mail.test`); service=http://10.0.0.130:3500; "
        "middlewares=rate-limit+basic-auth\ngateway-->",
    )
    out = tmp_path / "dynamic.yml"
    r = run(conf, out)
    assert r.returncode == 0, r.stderr
    text = out.read_text()
    assert "      middlewares:" in text
    assert "        - rate-limit" in text and "        - basic-auth" in text