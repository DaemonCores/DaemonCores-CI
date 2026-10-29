#!/usr/bin/env bash
#
# gateway-reconcile.sh - render a traefik file-provider dynamic.yml from the
# guest NOTES (CT description field) of a Proxmox host.
#
# Traefik-kop is ABANDONED (decision Gabriel 2026-10-30, format arbitrated
# 2026-10-30: variant B): the gateway declarations of compose guests live in
# the CT NOTES, inside a machine-readable bloc delimited by `<!--gateway`
# ... `gateway-->` holding a ```yaml fenced block with a `routers:` mapping
# (variant B, fenced YAML - see DaemonCores-VE.wiki
# compose-variant-a-plan.md for the format and the trade-offs against the
# flat key=value variant A, now rejected). Semantics: `rule` and `service`
# are mandatory per router; `entrypoints` defaults to `websecure`;
# `middlewares` and `tls: true` are optional. Router names are prefixed by
# the guest (`ct120_forge`). The prose around the bloc is never parsed
# (reflow-PROSE guard): the NOTES editor may freely reflow human text, only
# the fenced YAML matters, and indentation inside it is load-bearing.
# Requires python3-yaml (PyYAML) on the host.
#
# This script is READ-ONLY over the guests: it never edits a guest config,
# never touches the proxy VM nor traefik's own config. It only writes
# GW_OUT (default /var/lib/gateway-reconcile/dynamic.yml), atomically and
# idempotently (no rewrite when unchanged). Delivery into a live traefik is
# a separate, human-gated step.
#
# Source of truth: live `pvesh get /nodes/<node>/lxc/<id>/config` by
# default; GW_CONF_DIR points at an /etc/pve/lxc-like directory for
# offline runs and tests.
#
# Exit codes: 0 ok, 4 a NOTES bloc is malformed/unsupported (router
# skipped, other guests still reconciled), 5 collection failure.

set -euo pipefail

GW_NODE="${1:-${GW_NODE:-$(hostname -s)}}"
GW_OUT="${GW_OUT:-/var/lib/gateway-reconcile/dynamic.yml}"
GW_CONF_DIR="${GW_CONF_DIR:-}"

# Collects descriptions as one JSON object { "<vmid>": "<description>" }.
collect_descriptions() {
    python3 - "$GW_CONF_DIR" "$GW_NODE" <<'PYEOF'
import json, pathlib, subprocess, sys
conf_dir, node = sys.argv[1], sys.argv[2]
if conf_dir:
    out = {}
    for f in sorted(pathlib.Path(conf_dir).glob("*.conf")):
        for line in f.read_text().splitlines():
            if line.startswith("description: "):
                # pmxcfs keeps the description as one escaped single line
                out[f.stem] = line[len("description: "):].replace("\\n", "\n")
    print(json.dumps(out))
    sys.exit(0)
ids = subprocess.run(
    ["pvesh", "get", f"/nodes/{node}/lxc", "--output-format", "json"],
    capture_output=True, text=True, check=True)
out = {}
for guest in json.loads(ids.stdout):
    vid = str(guest["vmid"])
    cfg = subprocess.run(
        ["pvesh", "get", f"/nodes/{node}/lxc/{vid}/config", "--output-format", "json"],
        capture_output=True, text=True, check=True)
    out[vid] = json.loads(cfg.stdout).get("description", "")
print(json.dumps(out))
PYEOF
}

reconcile() {
    python3 "$gw_pylib" "$GW_OUT"
}

main() {
    gw_pylib="$(mktemp /tmp/gateway-reconcile.XXXXXX.py)"
    trap 'rm -f "$gw_pylib"' EXIT
    cat >"$gw_pylib" <<'PYEOF'
import json, os, re, sys, tempfile

GW_OUT = sys.argv[1]
BLOC = re.compile(r"<!--gateway\b(.*?)\n\s*gateway-->", re.S)
FENCE = re.compile(r"```ya?ml[^\n]*\n(.*?)```", re.S)

try:
    import yaml
except ImportError:
    print("gateway-reconcile: python3-yaml (PyYAML) is required for variant B", file=sys.stderr)
    sys.exit(5)


def warn(vid, msg):
    print(f"::warning::gateway NOTES CT {vid}: {msg}", file=sys.stderr)


def parse_bloc(bloc, vmid):
    """Parse the fenced-YAML variant B bloc -> (routers, bad?)."""
    m = FENCE.search(bloc)
    if not m:
        warn(vmid, "no ```yaml fenced block inside the gateway markers; bloc skipped "
                   "(variant B requires routers: in a fenced yaml block)")
        return {}, True
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError as e:
        warn(vmid, f"unparsable YAML in the gateway fenced block, bloc skipped: {e}")
        return {}, True
    routers_root = data.get("routers") if isinstance(data, dict) else None
    if not isinstance(routers_root, dict) or not routers_root:
        warn(vmid, "no `routers:` mapping found in the fenced YAML; bloc skipped")
        return {}, True
    routers = {}
    bad = False
    for name, spec in routers_root.items():
        if not isinstance(spec, dict) or not spec.get("rule") or not spec.get("service"):
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
            "rule": str(spec["rule"]), "service": str(spec["service"]),
            "entrypoints": eps, "middlewares": mws, "tls": tbool,
        }
    return routers, bad


def quote(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def render(routers):
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
        svc = [f"    {rid}:", "      loadBalancer:", "        servers:",
               f"          - url: {quote(o['service'])}"]
        lines[2:2] = entry
        lines += svc
    return "\n".join(lines) + "\n"


def main():
    routers = {}
    skipped = False
    for vmid, desc in sorted(json.load(sys.stdin).items(), key=lambda kv: int(kv[0])):
        m = BLOC.search(desc)
        if not m:
            continue
        bloc = m.group(1)
        # Variant A guard: flat `name: key=value` lines without a yaml fence
        # are the rejected format - refuse loudly, never silently half-parse.
        if not FENCE.search(bloc) and re.search(r"^[A-Za-z0-9_-]+:\s*\S+=", bloc, re.M):
            print(f"::warning::gateway NOTES CT {vmid}: flat key=value variant A is not "
                  "supported by this reconcile; bloc skipped (see wiki format arbitration)",
                  file=sys.stderr)
            sys.exit(4)
        if re.search(r"^\s*routers:\s*$", bloc, re.M) and not FENCE.search(bloc):
            print(f"::warning::gateway NOTES CT {vmid}: `routers:` without a ```yaml fence "
                  "is not supported; bloc skipped", file=sys.stderr)
            sys.exit(4)
        new, bad = parse_bloc(bloc, vmid)
        routers.update(new)
        skipped = skipped or bad
    content = render(routers)
    if os.path.exists(GW_OUT) and open(GW_OUT).read() == content:
        print("gateway-reconcile: unchanged", file=sys.stderr)
        sys.exit(4 if skipped else 0)
    fd, tmp = tempfile.mkstemp(prefix=".dynamic.yml.", dir=os.path.dirname(GW_OUT) or ".")
    with os.fdopen(fd, "w") as f:
        f.write(content)
    os.chmod(tmp, 0o644)
    os.replace(tmp, GW_OUT)
    print(f"gateway-reconcile: wrote {GW_OUT} ({len(routers)} routers)", file=sys.stderr)
    sys.exit(4 if skipped else 0)


main()
PYEOF
    collect_descriptions | reconcile
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi