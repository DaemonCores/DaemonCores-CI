#!/bin/bash
#
# security-care.sh - hold a security upgrade back until it is old enough.
#
# The Debian security archive gives no per-package age, so the age is the time
# since this ledger first saw the exact candidate version: a version that was
# withdrawn and replaced restarts its own delay. A package named in
# SECURITY_URGENT (one per line, fed by the critical-CVE feeds) skips the
# delay. Every candidate is recorded, held back or not.
#
# Arguments:
#   $1 - ledger file ("<package> <new> <first-seen epoch>" lines), created if absent
#   $2 - care delay in hours
#   $3 - current time, epoch seconds (default: now)
# Inputs:
#   stdin: output of security-detect.sh ("<package> <old> <new>" lines)
#   env SECURITY_URGENT: packages that skip the delay, one per line
# Outputs:
#   stdout: the upgrades that are adopted, same format as the input
# Returns:
#   1 when an argument is invalid.
set -euo pipefail

ledger="${1:?ledger required}"
hours="${2:?delay in hours required}"
now="${3:-$(date -u +%s)}"

if [[ ! "${hours}" =~ ^[0-9]+$ || ! "${now}" =~ ^[0-9]+$ ]]; then
  echo "::error::delay and time must be integers" >&2
  exit 1
fi

touch "${ledger}"
urgent="${SECURITY_URGENT:-}"

while read -r pkg old new; do
  [[ -n "${pkg}" && -n "${new}" ]] || continue
  seen=$(awk -v p="${pkg}" -v v="${new}" '$1 == p && $2 == v {print $3; exit}' "${ledger}")
  if [[ -z "${seen}" ]]; then
    seen="${now}"
    echo "${pkg} ${new} ${seen}" >>"${ledger}"
  fi
  if grep -qxF -- "${pkg}" <<<"${urgent}" \
    || ((now - seen >= hours * 3600)); then
    echo "${pkg} ${old} ${new}"
  fi
done
