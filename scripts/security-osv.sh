#!/bin/bash
#
# security-osv.sh - OSV.dev and CISA KEV helpers of the security channel.
# Source it, never execute it.
#
# Debian records in OSV are keyed by SOURCE package and by version: the query
# for a version returns every record that still affects it, so the records of
# the old version that the new version no longer returns are the ones the
# upgrade fixes.
#
# Consumed environment:
#   OSV_URL, KEV_URL           feed endpoints (defaults: api.osv.dev, cisa.gov)
#   SECURITY_OSV_FIXTURE_DIR   tests: directory of <source>_<version>.json
#                              replacing the OSV answers (absent file =
#                              unreachable)
#   SECURITY_KEV_FIXTURE       tests: file replacing the KEV feed
set -euo pipefail

OSV_URL="${OSV_URL:-https://api.osv.dev/v1/query}"
KEV_URL="${KEV_URL:-https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json}"

#######################################
# Print the source package of a binary package.
# Globals:
#   none
# Arguments:
#   $1 - binary package
#   $2 - map file ("<binary> <source>" lines)
# Outputs:
#   The source package, or the binary name when the map has no entry.
#######################################
osv_source() {
  awk -v p="$1" '$1 == p { print $2; found = 1; exit } END { if (!found) print p }' "$2"
}

#######################################
# Print the OSV records that affect one version of a source package.
# Globals:
#   OSV_URL, SECURITY_OSV_FIXTURE_DIR
# Arguments:
#   $1 - ecosystem (for example Debian:13)
#   $2 - source package
#   $3 - version
# Outputs:
#   One "<id><TAB><alias> <alias>" line per record, every page of the answer.
# Returns:
#   1 when the answer cannot be read.
#######################################
osv_records() {
  local eco="$1" pkg="$2" ver="$3" token="" query resp
  while :; do
    if [[ -n "${SECURITY_OSV_FIXTURE_DIR:-}" ]]; then
      resp=$(cat "${SECURITY_OSV_FIXTURE_DIR}/${pkg}_${ver}.json") || return 1
    else
      query=$(jq -cn --arg e "${eco}" --arg p "${pkg}" --arg v "${ver}" \
        --arg t "${token}" \
        '{package: {name: $p, ecosystem: $e}, version: $v}
         + (if $t == "" then {} else {page_token: $t} end)')
      resp=$(curl --retry 3 -fsSL -X POST -H 'Content-Type: application/json' \
        -d "${query}" "${OSV_URL}") || return 1
    fi
    jq -r '.vulns[]? | [.id, ((.aliases // []) | join(" "))] | @tsv' <<<"${resp}"
    token=$(jq -r '.next_page_token // empty' <<<"${resp}")
    [[ -n "${token}" && -z "${SECURITY_OSV_FIXTURE_DIR:-}" ]] || break
  done
}

#######################################
# Print the CVE ids of the CISA Known Exploited Vulnerabilities catalog.
# Globals:
#   KEV_URL, SECURITY_KEV_FIXTURE
# Outputs:
#   One CVE id per line.
# Returns:
#   1 when the catalog cannot be read.
#######################################
kev_cves() {
  local kev
  if [[ -n "${SECURITY_KEV_FIXTURE:-}" ]]; then
    kev=$(cat "${SECURITY_KEV_FIXTURE}") || return 1
  else
    kev=$(curl --retry 3 -fsSL "${KEV_URL}") || return 1
  fi
  jq -r '.vulnerabilities[]?.cveID' <<<"${kev}"
}
