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
# skipped, other guests still reconciled; the rejected variant A and a
# routers block without a fence abort loudly the run instead), 5
# collection failure or missing PyYAML.

set -euo pipefail

GW_NODE="${1:-${GW_NODE:-$(hostname -s)}}"
GW_OUT="${GW_OUT:-/var/lib/gateway-reconcile/dynamic.yml}"
GW_CONF_DIR="${GW_CONF_DIR:-}"
# Helper programs live next to this script as real files (no heredoc writes).
GW_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Collects descriptions as one JSON object { "<vmid>": "<description>" }.
collect_descriptions() {
    python3 "${GW_SCRIPT_DIR}/gateway-reconcile-collect.py" "$GW_CONF_DIR" "$GW_NODE"
}

reconcile() {
    python3 "${GW_SCRIPT_DIR}/gateway-reconcile-render.py" "$GW_OUT"
}

main() {
    collect_descriptions | reconcile
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi