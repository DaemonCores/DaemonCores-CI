#!/bin/bash
#
# security-urgent.sh - name the packages whose upgrade fixes an actively
# exploited vulnerability, so that they skip the care delay.
#
# A package is urgent when a CVE that the upgrade fixes (a record that affects
# the installed version and no longer affects the new one) is listed in the
# CISA Known Exploited Vulnerabilities catalog. An unreachable feed never
# makes a package urgent: the care delay then applies, and a warning says so.
#
# Arguments:
#   $1 - OSV ecosystem (Debian:<VERSION_ID> of the image)
#   $2 - map file ("<binary> <source>" lines) of the installed packages
# Inputs:
#   stdin: output of security-detect.sh ("<package> <old> <new>" lines)
# Outputs:
#   stdout: the urgent package names, one per line
set -euo pipefail

here="$(dirname "${BASH_SOURCE[0]}")"
# shellcheck source=security-osv.sh
source "${here}/security-osv.sh"

eco="${1:?ecosystem required}"
srcmap="${2:?source map required}"

if ! kev=$(kev_cves); then
  echo "::warning::CISA KEV unreachable: no package skips the care delay" >&2
  exit 0
fi

while read -r pkg old new; do
  [[ -n "${pkg}" && -n "${new}" ]] || continue
  src=$(osv_source "${pkg}" "${srcmap}")
  if ! before=$(osv_records "${eco}" "${src}" "${old}") \
    || ! after=$(osv_records "${eco}" "${src}" "${new}"); then
    echo "::warning::OSV unreachable for ${src}: ${pkg} keeps the care delay" >&2
    continue
  fi
  fixed=$(awk -F'\t' -v open="$(cut -f1 <<<"${after}")" \
    'BEGIN { n = split(open, a, "\n"); for (i = 1; i <= n; i++) o[a[i]] }
     $1 != "" && !($1 in o) { print $1, $2 }' <<<"${before}")
  hits=$(awk -v kev="${kev}" \
    'BEGIN { n = split(kev, a, "\n"); for (i = 1; i <= n; i++) k[a[i]] }
     { while (match($0, /CVE-[0-9]+-[0-9]+/)) {
         c = substr($0, RSTART, RLENGTH); $0 = substr($0, RSTART + RLENGTH)
         if (c in k) print c } }' <<<"${fixed}" | sort -u)
  if [[ -n "${hits}" ]]; then
    echo "security-urgent: ${pkg} fixes actively exploited $(xargs <<<"${hits}") (CISA KEV)" >&2
    echo "${pkg}"
  fi
done
