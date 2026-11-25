#!/bin/bash
#
# security-adopt.sh - decide which security upgrades the rebuild adopts today.
#
# Order: the advisory gate holds a package named by an open advisory
# (security-advisory.sh), the critical feed names the packages that skip the
# delay (security-urgent.sh), the care delay then holds back every upgrade
# whose exact version was first seen less than the delay ago
# (security-care.sh). The versions that are adopted are the pins of the
# rebuild: security-containerfile.sh installs exactly them.
#
# Arguments:
#   $1 - OSV ecosystem (Debian:<VERSION_ID> of the image)
#   $2 - map file ("<binary> <source>" lines) of the installed packages
#   $3 - first-seen ledger file (kept between runs)
#   $4 - care delay in hours
#   $5 - directory receiving report.md and the intermediate lists
# Inputs:
#   stdin: output of security-detect.sh ("<package> <old> <new>" lines)
# Outputs:
#   stdout: the adopted upgrades, same format as the input
#   $5/report.md: what was adopted without delay, held back and why (empty
#   when nothing was held), for the release notes
set -euo pipefail

here="$(dirname "${BASH_SOURCE[0]}")"
eco="${1:?ecosystem required}"
srcmap="${2:?source map required}"
ledger="${3:?ledger required}"
hours="${4:?delay in hours required}"
out="${5:?output directory required}"
mkdir -p "${out}"

bash "${here}/security-advisory.sh" "${eco}" "${srcmap}" "${out}/held-advisory.txt" \
  >"${out}/passed.txt"
bash "${here}/security-urgent.sh" "${eco}" "${srcmap}" \
  <"${out}/passed.txt" >"${out}/urgent.txt"
SECURITY_URGENT="$(cat "${out}/urgent.txt")" \
  bash "${here}/security-care.sh" "${ledger}" "${hours}" \
  <"${out}/passed.txt" >"${out}/adopted.txt"
awk 'FILENAME == ARGV[1] { done[$0]; next } !($0 in done)' \
  "${out}/adopted.txt" "${out}/passed.txt" >"${out}/held-care.txt"

{
  if [[ -s "${out}/urgent.txt" ]]; then
    printf '## Adopted without care delay (CISA KEV)\n\n'
    sed 's/^/- /' "${out}/urgent.txt"
    printf '\n'
  fi
  if [[ -s "${out}/held-care.txt" ]]; then
    printf '## Held back by the %s h care delay\n\n' "${hours}"
    awk '{ printf "- %s %s -> %s\n", $1, $2, $3 }' "${out}/held-care.txt"
    printf '\n'
  fi
  if [[ -s "${out}/held-advisory.txt" ]]; then
    printf '## Held by the advisory gate\n\n'
    awk '{ ids = $4; for (i = 5; i <= NF; i++) ids = ids ", " $i
      printf "- %s %s -> %s: %s\n", $1, $2, $3, ids }' "${out}/held-advisory.txt"
    printf '\n'
  fi
} >"${out}/report.md"

cat "${out}/adopted.txt"
