#!/usr/bin/env python3
"""Render the Traefik dynamic config from the gateway NOTES blocs."""

import json
import os
import re
import sys
import tempfile

GW_OUT = sys.argv[1]
BLOC = re.compile(r"<!--gateway\b(.*?)\n\s*gateway-->", re.S)
FENCE = re.compile(r"```ya?ml[^\n]*\n(.*?)```", re.S)

try:
    import yaml
except ImportError:
    print(
        "gateway-reconcile: python3-yaml (PyYAML) is required for variant B",
        file=sys.stderr
    )
    sys.exit(5)


def warn(vid: str, msg: str) -> None:
    """Emit a GitHub Actions warning annotation for one container.

    Args:
        vid: VMID of the container the gateway note comes from.
        msg: Text of the warning.
    """
    print(f"::warning::gateway NOTES CT {vid}: {msg}", file=sys.stderr)


def parse_bloc(bloc: str, vmid: str) -> tuple[dict[str, dict], bool]:
    """Parse the fenced-YAML variant B bloc -> (routers, bad?).

    Args:
        bloc: Text of the gateway NOTES bloc, between its markers.
        vmid: VMID of the container the bloc belongs to.

    Returns:
        The routers keyed by ``ct<vmid>_<name>``, and whether the bloc or one
        of its routers was rejected.
    """
    m = FENCE.search(bloc)
    if not m:
        warn(
            vmid,
            "no ```yaml fenced block inside the gateway markers; bloc skipped "
            "(variant B requires routers: in a fenced yaml block)"
        )
        return {}, True
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError as e:
        warn(
            vmid,
            f"unparsable YAML in the gateway fenced block, bloc skipped: {e}"
        )
        return {}, True
    routers_root = data.get("routers") if isinstance(data, dict) else None
    if not isinstance(routers_root, dict) or not routers_root:
        warn(
            vmid, "no `routers:` mapping found in the fenced YAML; bloc skipped"
        )
        return {}, True
    routers = {}
    bad = False
    for name, spec in routers_root.items():
        if not isinstance(spec, dict
                          ) or not spec.get("rule") or not spec.get("service"):
            warn(vmid, f"router {name!r}: missing rule/service; router skipped")
            bad = True
            continue
        eps = spec.get("entrypoints", "websecure")
        if isinstance(eps, str):
            eps = [e for e in eps.split("+") if e]
        elif eps is None:
            eps = []
        else:
            eps = [str(e) for e in eps]
        mws = spec.get("middlewares") or ""
        if isinstance(mws, str):
            mws = [w for w in mws.split("+") if w]
        else:
            mws = [str(w) for w in mws]
        tls = spec.get("tls", "")
        if isinstance(tls, bool):
            tbool = tls
        else:
            tbool = str(tls).strip().lower() in ("true", "yes", "1")
        routers[f"ct{vmid}_{name}"] = {
            "rule": str(spec["rule"]),
            "service": str(spec["service"]),
            "entrypoints": eps,
            "middlewares": mws,
            "tls": tbool,
        }
    return routers, bad


def quote(s: str) -> str:
    """Return s as a double-quoted YAML scalar with escapes.

    Args:
        s: Text to quote.

    Returns:
        The text as a double-quoted YAML scalar, with backslashes, quotes and
        newlines escaped.
    """
    return '"' + s.replace("\\",
                           "\\\\").replace('"',
                                           '\\"').replace("\n", "\\n") + '"'


def render(routers: dict[str, dict]) -> str:
    """Render the routers mapping as the Traefik dynamic YAML text.

    Args:
        routers: Router id to settings (rule, service, entrypoints,
            middlewares, tls), as built by parse_bloc.

    Returns:
        The Traefik dynamic configuration as YAML text, or a comment line when
        there are no routers.
    """
    if not routers:
        return "# gateway-reconcile: no gateway NOTES bloc found\n"
    lines = ["http:", "  routers:", "  services:"]
    for rid, o in routers.items():
        entry = [f"    {rid}:", f"      rule: {quote(o['rule'])}"]
        if o["entrypoints"]:
            entry.append("      entryPoints:")
            entry += [f"        - {e}" for e in o["entrypoints"]]
        if o["middlewares"]:
            entry.append("      middlewares:")
            entry += [f"        - {w}" for w in o["middlewares"]]
        if o["tls"]:
            entry.append("      tls: {}")
        entry.append(f"      service: {rid}")
        svc = [
            f"    {rid}:", "      loadBalancer:", "        servers:",
            f"          - url: {quote(o['service'])}"
        ]
        lines[2:2] = entry
        lines += svc
    return "\n".join(lines) + "\n"


def main() -> None:
    """Read descriptions from stdin and write the dynamic config file."""
    routers = {}
    skipped = False
    for vmid, desc in sorted(
        json.load(sys.stdin).items(), key=lambda kv: int(kv[0])
    ):
        m = BLOC.search(desc)
        if not m:
            continue
        bloc = m.group(1)
        # Variant A guard: flat `name: key=value` lines without a yaml fence
        # are the rejected format - refuse loudly, never silently half-parse.
        if not FENCE.search(bloc) and re.search(
            r"^[A-Za-z0-9_-]+:\s*\S+=", bloc, re.M
        ):
            print(
                f"::warning::gateway NOTES CT {vmid}: flat key=value "
                "variant A is not supported by this reconcile; bloc skipped "
                "(see wiki format arbitration)",
                file=sys.stderr
            )
            sys.exit(4)
        if re.search(r"^\s*routers:\s*$", bloc,
                     re.M) and not FENCE.search(bloc):
            print(
                f"::warning::gateway NOTES CT {vmid}: `routers:` without a "
                "```yaml fence is not supported; bloc skipped",
                file=sys.stderr
            )
            sys.exit(4)
        new, bad = parse_bloc(bloc, vmid)
        routers.update(new)
        skipped = skipped or bad
    content = render(routers)
    current = None
    if os.path.exists(GW_OUT):
        with open(GW_OUT) as existing:
            current = existing.read()
    if current == content:
        print("gateway-reconcile: unchanged", file=sys.stderr)
        sys.exit(4 if skipped else 0)
    fd, tmp = tempfile.mkstemp(
        prefix=".dynamic.yml.", dir=os.path.dirname(GW_OUT) or "."
    )
    with os.fdopen(fd, "w") as f:
        f.write(content)
    os.chmod(tmp, 0o644)
    os.replace(tmp, GW_OUT)
    print(
        f"gateway-reconcile: wrote {GW_OUT} ({len(routers)} routers)",
        file=sys.stderr
    )
    sys.exit(4 if skipped else 0)


main()
