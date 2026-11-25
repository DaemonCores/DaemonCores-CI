#!/bin/bash
#
# security-advisory.sh - advisory gate of the security channel: hold back an
# upgrade whose new version is still named by an open advisory.
#
# OSV is asked for the exact new version. The Debian tracker's own per-CVE
# records (DEBIAN-CVE-*, which include the no-dsa and postponed entries) never
# hold anything: the design says only a real version change in the security
# archive acts, and those records are open for almost every package. Any other
# record (DSA, GHSA, MAL, OSV ...) naming the new version holds the package,
# logs the verdict and opens one deduplicated issue. An unreachable OSV is a
# loud warning and a pass: the gate supports the channel, it is not its outage.
#
# Arguments:
#   $1 - OSV ecosystem (Debian:<VERSION_ID> of the image)
#   $2 - map file ("<binary> <source>" lines) of the installed packages
#   $3 - file receiving the held upgrades ("<package> <old> <new> <ids>")
# Inputs:
#   stdin: output of security-detect.sh ("<package> <old> <new>" lines)
# Outputs:
#   stdout: the upgrades that pass the gate, same format as the input
# Environment:
#   GH_TOKEN, GITHUB_REPOSITORY: hold issue delivery (no-op without them)
set -euo pipefail

here="$(dirname "${BASH_SOURCE[0]}")"
# shellcheck source=security-osv.sh
source "${here}/security-osv.sh"
# shellcheck source=../upstream-track/security_gate.sh
source "${here}/../upstream-track/security_gate.sh"

eco="${1:?ecosystem required}"
srcmap="${2:?source map required}"
held="${3:?held file required}"
: >"${held}"

while read -r pkg old new; do
  [[ -n "${pkg}" && -n "${new}" ]] || continue
  # shellcheck disable=SC2034 # read by gate_log and gate_hold_issue
  PKG="${pkg}"
  src=$(osv_source "${pkg}" "${srcmap}")
  if ! records=$(osv_records "${eco}" "${src}" "${new}"); then
    gate_log warn osv unreachable \
      "OSV unreachable for ${src} ${new}: ${pkg} passes the advisory gate, logged"
    echo "${pkg} ${old} ${new}"
    continue
  fi
  ids=$(awk -F'\t' '$1 != "" && $1 !~ /^DEBIAN-CVE-/ { print $1 }' <<<"${records}" | sort -u | xargs)
  if [[ -z "${ids}" ]]; then
    echo "${pkg} ${old} ${new}"
    continue
  fi
  gate_log hold osv "${ids}" "OSV HOLD: ${pkg} ${new} is named by ${ids}"
  echo "${pkg} ${old} ${new} ${ids}" >>"${held}"
  gate_hold_issue "security channel holds ${pkg} ${new}" \
    "The advisory gate holds ${pkg} ${old} -> ${new}: OSV names the new version in ${ids}. It is adopted again once no such advisory names the candidate."
done
