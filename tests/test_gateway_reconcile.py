"""Unit tests for ``gateway-reconcile.sh`` (NOTES bloc -> dynamic.yml).

Format B (arbitrated 2026-10-30): the gateway bloc is a ```yaml fenced
block inside the ``<!--gateway`` markers, holding a ``routers:`` mapping.
"""

import json
import subprocess
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "gateway-reconcile.sh"
)

BLOC_OK = (
    "Free prose of the human.\n"
    "<!--gateway\n"
    "```yaml\n"
    "routers:\n"
    "  forge:\n"
    "    rule: Host(`example.test`) && PathPrefix(`/forge`)\n"
    "    service: http://10.0.0.120:3000\n"
    "    entrypoints:\n"
    "      - websecure\n"
    "    tls: true\n"
    "  portfolio:\n"
    "    rule: Host(`example.test`)\n"
    "    service: http://10.0.0.120:14321\n"
    "```\n"
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
    assert 'rule: "Host(`example.test`) && PathPrefix(`/forge`)"' in text
    assert "- websecure" in text
    assert "tls: {}" in text
    assert 'url: "http://10.0.0.120:3000"' in text
    assert "ct120_portfolio" in text and "no entryPoints" not in text
    # default entrypoints for portfolio (no entrypoints key)
    text_portfolio = text.split("ct120_portfolio:\n")[1]
    assert "entryPoints" in text_portfolio and "- websecure" in text_portfolio
    # prose never leaks into the yaml
    assert "Free prose" not in text


def test_skips_broken_router_but_keeps_valid_ones_exit4(tmp_path: Path) -> None:
    conf = make_conf(
        tmp_path, 122,
        "<!--gateway\n```yaml\nrouters:\n  broken:\n    rule: Host(`x`)\n"
        "  good:\n    rule: Host(`y`)\n    service: http://10.0.0.122:80\n"
        "```\ngateway-->",
    )
    make_conf(tmp_path, 120, BLOC_OK)
    out = tmp_path / "dynamic.yml"
    r = run(conf, out)
    assert r.returncode == 4, r.stderr
    text = out.read_text()
    assert "ct120_forge:" in text
    assert "ct122_good:" in text
    assert "ct122_broken:" not in text
    assert "missing rule/service" in r.stderr


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


def test_variant_a_flat_bloc_is_refused(tmp_path: Path) -> None:
    conf = make_conf(
        tmp_path, 120,
        "<!--gateway\nforge: rule=Host(`x`); service=http://10.0.0.120:3000\n"
        "gateway-->",
    )
    out = tmp_path / "dynamic.yml"
    r = run(conf, out)
    assert r.returncode == 4, r.stderr
    assert "variant A" in r.stderr


def test_routers_without_fence_is_refused(tmp_path: Path) -> None:
    conf = make_conf(
        tmp_path, 140,
        "<!--gateway\nrouters:\n  forge:\n    rule: Host(`x`)\n"
        "    service: http://10.0.0.140:3000\ngateway-->",
    )
    out = tmp_path / "dynamic.yml"
    r = run(conf, out)
    assert r.returncode == 4, r.stderr
    assert "without a ```yaml fence" in r.stderr


def test_unparsable_yaml_refused_exit4(tmp_path: Path) -> None:
    conf = make_conf(
        tmp_path, 120,
        "<!--gateway\n```yaml\nrouters:\n  forge:\n    rule: {unclosed\n"
        "```\ngateway-->",
    )
    out = tmp_path / "dynamic.yml"
    r = run(conf, out)
    assert r.returncode == 4, r.stderr
    assert "unparsable YAML" in r.stderr


def test_middlewares_list_rendered(tmp_path: Path) -> None:
    conf = make_conf(
        tmp_path, 130,
        "<!--gateway\n```yaml\nrouters:\n  mail:\n    rule: Host(`mail.test`)\n"
        "    service: http://10.0.0.130:3500\n"
        "    middlewares:\n      - rate-limit\n      - basic-auth\n"
        "```\ngateway-->",
    )
    out = tmp_path / "dynamic.yml"
    r = run(conf, out)
    assert r.returncode == 0, r.stderr
    text = out.read_text()
    assert "      middlewares:" in text
    assert "        - rate-limit" in text and "        - basic-auth" in text