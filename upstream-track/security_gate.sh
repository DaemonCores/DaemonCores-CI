#!/bin/bash
#
# security_gate.sh - the automatic security gates of the LATEST-PASSING-GATE
# election (owner order 2026-11-10: no human diff review, no lingering pin;
# the version moves, the gates judge it). Source it, never execute it.
#
# Behaviours, every verdict logged (selection.log JSONL + stderr):
#   CARE DELAY   CARE_DELAY (seconds, default 86400 = 24 h): a candidate
#                younger than the delay is HELD. CARE_DELAY=0 disables the
#                delay (hotfix). Applied only when the candidate age is
#                known.
#   FLUX         OSV.dev plus the source repository GHSA advisories: an open
#                advisory on the SOURCE repo or the elected COMMIT holds the
#                candidate. An unreachable advisory source = loud warning and
#                a pass (the gates are a support, never their own outage).
#   BYPASS       CISA KEV and the linux-cve-announce feed: an actively
#                exploited or critically rated kernel CVE bypasses the care
#                delay only; the log cites the CVE ids and the feed. The two
#                feeds are fetched once per process (GATE_BYPASS_VERDICT
#                caches the verdict).
#   RECORD       Every adopted election appends a JSONL line to
#                ${ELECTION_DIR}/selection.log (the log = the SBOM source)
#                and updates the per-package record file the publish step
#                merges into debs/.meta.
#
# Consumed environment:
#   CARE_DELAY    delay in seconds, 0 to disable (default 86400)
#   ELECTION_DIR  election log + records directory
#                 (default ${RUNNER_TEMP:-/tmp}/dc-election)
#   GATE_OSV_FIXTURE, GATE_KEV_FIXTURE, GATE_CVE_FEED_FIXTURE
#                 test fixtures: a file whose bytes replace the live HTTP
#                 response (no network in tests)
#   GATE_KERNEL_GHSA_REPO  advisory stream judged for kernel candidates
#                 (default torvalds/linux)
#   PKG           package name for the record file and the hold issue
#   GH_TOKEN, GITHUB_REPOSITORY, GITHUB_API_URL
#                 hold-issue delivery (no-op without a token)
#   fetch_api     provided by the sourcing script (track.sh / sibling.sh)
set -uo pipefail

ELECTION_DIR="${ELECTION_DIR:-${RUNNER_TEMP:-/tmp}/dc-election}"
KEV_URL="${KEV_URL:-https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json}"
CVE_FEED_URL="${CVE_FEED_URL:-https://lore.kernel.org/linux-cve-announce/feed.atom}"

#######################################
# Append one JSONL verdict line to the election selection.log and echo the
# human summary to stderr.
# Arguments:
#   $1 - event (election | hold | bypass | warn)
#   $2 - gate field name (care | osv | ghsa | flux | kev | cve-announce)
#   $3 - gate field value
#   $4 - human summary
#######################################
gate_log() {
  local event="$1" fname="$2" fval="$3" what="$4" line
  mkdir -p "${ELECTION_DIR}"
  line=$(jq -cn --arg e "${event}" --arg p "${PKG:-}" \
    --arg t "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
    --arg n "${fname}" --arg v "${fval}" \
    '{event: $e, package: $p, at: $t} +
     (if $n == "" then {} else {($n): $v} end)' 2>/dev/null)
  if [[ -n "${line}" ]]; then
    printf '%s\n' "${line}"
  else
    printf '%s\n' "{\"event\": \"${event}\", \"message\": \"log-error\"}"
  fi >>"${ELECTION_DIR}/selection.log"
  echo "security-gate: ${what}" >&2
}

#######################################
# Print the epoch seconds of an ISO-8601 date (failure = non-zero rc).
# Arguments:
#   $1 - ISO date / timestamp
#######################################
gate_date_epoch() {
  [[ -n "$1" ]] || return 1
  date -u -d "$1" '+%s' 2>/dev/null
}

#######################################
# The care-delay judge. rc 0 = pass, 3 = hold (reason logged: age vs delay,
# or the hotfix/unknown-age exception). Arguments:
#   $1 - candidate date (ISO-8601, may be empty = age unknown)
#   $2 - candidate description for the log
#######################################
gate_care_delay() {
  local date="$1" what="$2" delay now then_ age
  delay="${CARE_DELAY:-86400}"
  if [[ "${delay}" == "0" ]]; then
    gate_log warn care "pass (CARE_DELAY=0, hotfix)" \
      "care-delay DISABLED (CARE_DELAY=0, hotfix): ${what}"
    return 0
  fi
  if [[ -z "${date}" ]] || ! then_=$(gate_date_epoch "${date}"); then
    gate_log warn care "unknown age" \
      "care-delay skipped (candidate age unknown): ${what}"
    return 0
  fi
  now=$(date -u '+%s')
  age=$((now - then_))
  if [[ "${age}" -lt "${delay}" ]]; then
    gate_log hold care "hold" \
      "care-delay HOLD for ${what}: age ${age}s < CARE_DELAY ${delay}s"
    return 3
  fi
  gate_log warn care "pass (age ${age}s)" \
    "care-delay pass for ${what}: age ${age}s >= ${delay}s"
  return 0
}

#######################################
# One OSV.dev query. rc 0 = no advisory, 3 = hold (ids logged),
# 1 = unreachable. Arguments:
#   $1 - query JSON as documented by https://api.osv.dev/v1/query
#        (empty = no query shape for this candidate kind: no opinion)
#######################################
gate_osv() {
  local query="$1" resp ids
  [[ -n "${query}" ]] || return 0
  if [[ -n "${GATE_OSV_FIXTURE:-}" ]]; then
    resp=$(cat "${GATE_OSV_FIXTURE}")
  else
    resp=$(curl --retry 3 -fsSL -X POST -H 'Content-Type: application/json' \
      -d "${query}" "${GATE_OSV_URL:-https://api.osv.dev/v1/query}") || return 1
  fi
  ids=$(jq -r '.vulns[]?.id' <<<"${resp}" 2>/dev/null | grep -v '^$' | sort -u)
  if [[ -n "${ids}" ]]; then
    gate_log hold osv "$(tr '\n' ' ' <<<"${ids}" | xargs)" \
      "OSV HOLD: ${ids} open advisory(ies), query ${query}"
    return 3
  fi
  return 0
}

#######################################
# The GHSA advisories of a source repository. rc 0 = none, 3 = hold
# (a published advisory open on the SOURCE, ids logged), 1 = unreachable.
# Arguments:
#   $1 - owner/name
#######################################
gate_ghsa() {
  local repo="$1" cand="${2:-}" resp hits=()
  local id state patched
  resp=$(fetch_api "repos/${repo}/security-advisories?per_page=30" 2>/dev/null) \
    || return 1
  # A published advisory HOLDS only when the CANDIDATE version is still
  # inside its vulnerable range, i.e. below every patched version it
  # declares. Without a candidate version (commit pin, no release id) any
  # published advisory holds, as before.
  local line
  while IFS= read -r line; do
    [[ -n "${line}" ]] || continue
    read -r id state patched range <<<"${line}"
    [[ "${state}" == "published" ]] || continue
    if [[ -z "${cand}" ]]; then
      hits+=("${id}")
      continue
    fi
    # Satisfied when the candidate is >= the highest patched version of
    # every advisory that carries one; an advisory without any patched
    # version holds unconditionally.
    local v satisfied=0
    for v in ${patched}; do
      [[ -n "${v}" ]] || continue
      if dpkg --compare-versions "${cand}" ge "${v}" 2>/dev/null; then
        satisfied=1
      else
        satisfied=0
        break
      fi
    done
    [[ "${satisfied}" -eq 1 ]] || hits+=("${id}")
  done < <(yq -p=json -r '.[] | select(.state == "published") |
      [.ghsa_id, .state,
       ((.vulnerabilities // []) | map(.patched_versions // "") | join(" "))]
      | @tsv' <<<"${resp}" 2>/dev/null)
  if ((${#hits[@]})); then
    gate_log hold ghsa "$(printf '%s ' "${hits[@]}" | xargs)" \
      "GHSA HOLD: $(printf '%s ' "${hits[@]}" | xargs) published advisory(ies) on source ${repo} (candidate ${cand:-unknown})"
    return 3
  fi
  return 0
}

#######################################
# The whole flux judge (OSV against the source or the commit + the GHSA
# advisories of the source repository). rc 0 = pass, 3 = hold, 1 never
# escapes: an unreachable source degrades to a loud logged pass.
# Arguments:
#   $1 - OSV query JSON (empty = OSV skipped)
#   $2 - source repository owner/name for the GHSA check (empty = skipped)
#######################################
gate_flux() {
  local rc
  gate_osv "$1"
  rc=$?
  [[ "${rc}" -eq 0 ]] || return "${rc}"
  if [[ -n "$2" ]]; then
    gate_ghsa "$2" "${3:-}"
    rc=$?
    if [[ "${rc}" -eq 1 ]]; then
      gate_log warn flux unreachable \
        "advisory source GHSA($2) unreachable: gate passes, logged"
      return 0
    fi
    [[ "${rc}" -eq 0 ]] || return "${rc}"
  fi
  return 0
}

#######################################
# The bypass judge: CISA KEV + the linux-cve-announce feed. rc 0 = a bypass
# applies (CVE ids cited in the log, GATE_BYPASS_VERDICT set), 1 = none.
# Never returns a hold: a bypass only lifts the care delay.
#######################################
gate_bypass() {
  local kev ids feed
  if [[ -n "${GATE_BYPASS_VERDICT+x}" ]]; then
    # The bypass judges are fetched once per process: 30 sibling
    # candidates must not pull the KEV feed 30 times.
    [[ "${GATE_BYPASS_VERDICT}" == none ]] && return 1 || return 0
  fi
  if [[ -n "${GATE_KEV_FIXTURE:-}" ]]; then
    kev=$(cat "${GATE_KEV_FIXTURE}")
  else
    kev=$(curl --retry 3 -fsSL "${KEV_URL}") || kev=""
  fi
  ids=$(yq -p=json -r '.vulnerabilities[]
    | select((.vendorProject // "" | test("(?i)linux"))
      or ((.product // "") | test("(?i)kernel"))) | .cveID' \
    <<<"${kev}" 2>/dev/null | grep -v '^$' | sort -u)
  if [[ -n "${ids}" ]]; then
    gate_log bypass kev "$(tr '\n' ' ' <<<"${ids}" | xargs)" \
      "care-delay BYPASS: CISA KEV lists Linux kernel CVE(s): ${ids} (${KEV_URL})"
    GATE_BYPASS_VERDICT="CISA KEV (kernel CVEs listed)"
    return 0
  fi
  if [[ -n "${GATE_CVE_FEED_FIXTURE:-}" ]]; then
    feed=$(cat "${GATE_CVE_FEED_FIXTURE}")
  else
    feed=$(curl --retry 3 -fsSL "${CVE_FEED_URL}") || feed=""
  fi
  ids=$(awk 'BEGIN { RS = "<entry>" }
    /CRITICAL/ && match($0, /CVE-[0-9][0-9][0-9][0-9]-[0-9]+/) \
      { print substr($0, RSTART, RLENGTH) }' <<<"${feed}" 2>/dev/null \
    | sort -u)
  if [[ -n "${ids}" ]]; then
    gate_log bypass cve-announce "$(tr '\n' ' ' <<<"${ids}" | xargs)" \
      "care-delay BYPASS: linux-cve-announce CRITICAL CVE(s): ${ids} (${CVE_FEED_URL})"
    GATE_BYPASS_VERDICT="linux-cve-announce (CRITICAL CVEs listed)"
    return 0
  fi
  GATE_BYPASS_VERDICT="none"
  return 1
}

#######################################
# Update the per-package election record file
# ${ELECTION_DIR}/election-<PKG>.json: the entry carrying $1 is replaced, the
# other entries are kept (multi-key packages append). The record file is the
# materialized selection.log, i.e. the SBOM source the publish step merges
# into debs/.meta.
# Arguments:
#   $1 - entry key (the track env variable / sibling name)
#   $2 - kind (kernel | commit | release | sibling)
#   $3 - elected id (version or sha)
#   $4 - source url (may be empty)
#   $5 - sha256 (may be empty)
#   $6 - upstream date of the candidate (may be empty)
#   $7 - care-delay verdict (pass | bypass | hold | unknown age)
#   $8 - flux verdict (pass | unreachable | hold:ids)
#######################################
gate_record() {
  local out="${ELECTION_DIR}/election-${PKG:-unamed-package}.json"
  mkdir -p "${ELECTION_DIR}" 2>/dev/null || true
  jq -n --arg p "${PKG:-}" \
    --arg t "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
    --arg key "$1" --arg kind "$2" --arg id "$3" --arg url "$4" \
    --arg sha "$5" --arg date "$6" --arg care "$7" --arg flux "$8" \
    '{package: $p, elected_at: $t, entries: [{
        key: $key, kind: $kind, id: $id, url: $url, sha256: $sha,
        date: $date, care_delay: $care, flux: $flux}]}' >"${out}.new" \
    || { echo "security-gate: WARNING record build failed for ${PKG}" >&2; return 0; }
  if [[ -s "${out}" ]]; then
    jq --slurpfile rec "${out}.new" \
      '.elected_at = $rec[0].elected_at
       | .entries = (((.entries // [])
           | map(select(.key != $rec[0].entries[0].key))) + $rec[0].entries)' \
      "${out}" >"${out}.n" \
      && { mv "${out}.n" "${out}"; rm -f "${out}.new"; }
  fi
  [[ -f "${out}.new" ]] && mv "${out}.new" "${out}"
  return 0
}

#######################################
# Open or refresh the deduped hold issue of this package (the 'Fix broken
# build' pattern: an existing open issue carrying the same title is updated,
# only the first hold opens the issue). A no-op without a token/repository.
# Arguments:
#   $1 - title (a hold is deduped by its full title)
#   $2 - body (markdown)
#######################################
gate_hold_issue() {
  local title="$1" body="$2" api auth num
  [[ -n "${GH_TOKEN:-}" && -n "${GITHUB_REPOSITORY:-}" ]] || return 0
  title="🛑 ${title}"
  api="${GITHUB_API_URL:-https://api.github.com}/repos/${GITHUB_REPOSITORY}"
  auth=(-H "Authorization: Bearer ***" -H "Accept: application/vnd.github+json")
  num=$(curl --retry 2 -fsSL "${auth[@]}" \
    "${api}/issues?state=open&per_page=100" 2>/dev/null \
    | jq -r --arg t "${title}" \
      'map(select(.title == $t)) | .[0].number // empty') || num=""
  if [[ -n "${num}" ]]; then
    curl --retry 2 -fsSL "${auth[@]}" -X PATCH "${api}/issues/${num}" \
      -d "$(jq -n --arg b "${body}" '{body: $b}')" >/dev/null 2>&1 || true
  else
    curl --retry 2 -fsSL "${auth[@]}" -X POST "${api}/issues" \
      -d "$(jq -n --arg t "${title}" --arg b "${body}" \
        '{title: $t, body: $b}')" >/dev/null 2>&1 || true
  fi
}
