#!/bin/bash
#
# sbom.sh - the SBOM + per-package changelog of the published APT repository.
#
# The election selection.log (written by upstream-track/security_gate.sh) is
# the SOURCE: the track/sibling gates materialize each adopted candidate into
# a per-package record file, the build-deb step merges it into
# /debs/.meta/<pkg>-<arch>.json under the `election` key, and this script
# aggregates every record of the run into:
#
#   OUT_DIR/sbom.json      one entry per package: debs + the election record
#                          (kind, id, url, sha256, date, care/flux verdicts)
#   OUT_DIR/CHANGELOG.md   the variant-level changelog of THIS publish: every
#                          package whose elected id changed against the
#                          previously published sbom.json (no prior SBOM =
#                          the full list, honestly labelled first publish)
#
# Environment:
#   DEBS_DIR   merged deb tree (default debs)
#   OUT_DIR    output directory (default .): both files land at the Pages root
#   PAGES_URL / GITHUB_REPOSITORY  the previously published sbom.json feeds
#   the changelog diff (a repository URL is derived when PAGES_URL is unset)
set -euo pipefail

DEBS_DIR="${DEBS_DIR:-debs}"
OUT_DIR="${OUT_DIR:-.}"

#######################################
# Print the APT repo base URL (no trailing slash), empty without inputs.
#######################################
base_url() {
  if [[ -n "${PAGES_URL:-}" ]]; then
    printf '%s\n' "${PAGES_URL%/}"
  elif [[ -n "${GITHUB_REPOSITORY:-}" ]]; then
    local owner="${GITHUB_REPOSITORY%%/*}" repo="${GITHUB_REPOSITORY#*/}"
    printf 'https://%s.github.io/%s\n' "${owner,,}" "${repo}"
  fi
}

#######################################
# Print the architecture of a package from its first deb filename
# (custom packages suffix _<arch>.deb, kernel debs -<arch>; 'unknown' keeps
# the SBOM honest when nothing can be parsed).
# Arguments:
#   $1 - one deb basename
#######################################
debs_arch() {
  case "$1" in
    *_amd64.deb | *-amd64.deb) echo amd64 ;;
    *_arm64.deb | *-arm64.deb) echo arm64 ;;
    *_all.deb | *-all.deb) echo all ;;
    *) echo unknown ;;
  esac
}

#######################################
# Build OUT_DIR/sbom.json from every /debs/.meta record carrying an
# `election` object (the election selection.log source of this run).
#######################################
assemble_sbom() {
  local lines m arch debs pkg
  lines="${OUT_DIR}/sbom.lines"
  : >"${lines}"
  shopt -s nullglob
  for m in "${DEBS_DIR}/.meta/"*.json; do
    jq -e '.election != null' "${m}" >/dev/null 2>&1 || continue
    debs=$(jq -c '.debs // []' "${m}")
    arch=$(debs_arch "$(printf '%s\n' "${debs}" | jq -r '.[0] // ""')")
    pkg="$(basename "${m}" .json)"
    jq -c --arg pkg "${pkg%-*}" --arg arch "${arch}" --argjson debs "${debs}" \
      '{package: $pkg, arch: $arch, debs: $debs, election: .election}' "${m}" \
      >>"${lines}"
  done
  shopt -u nullglob
  jq -s --arg t "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
    '{generated: $t,
      source: "election selection.log (upstream-track/security_gate.sh)",
      packages: .}' "${lines}" >"${OUT_DIR}/sbom.json"
  rm -f "${lines}"
  echo "sbom.json: $(jq '.packages | length' "${OUT_DIR}/sbom.json") record(s)"
}

#######################################
# Write OUT_DIR/CHANGELOG.md: the packages of THIS run whose elected id
# differs from the previously published sbom.json. The prior SBOM (fetched
# read-only from the live Pages) is handed to the renderer as PRIOR_SBOM,
# empty on a first publish.
#######################################
assemble_changelog() {
  local out="${OUT_DIR}/CHANGELOG.md"
  if [[ -n "$(base_url)" ]]; then
    fetched=$(curl --retry 3 -fsSL "$(base_url)/sbom.json" 2>/dev/null) \
      || fetched=""
    [[ -n "${fetched}" ]] && PRIOR_SBOM="${fetched}" && export PRIOR_SBOM
  fi
  {
    echo "# Election changelog — $(date -u '+%Y-%m-%d %H:%M UTC')"
    echo
    echo 'Source: the election selection.log, the record the security gates'
    echo '(care delay, OSV/GHSA flux, KEV bypass) wrote at each adopted'
    echo 'candidate. A variant moves when its elected id changes against the'
    echo 'previously published SBOM.'
    echo
  } >"${out}"
  python3 - "${OUT_DIR}/sbom.json" >>"${out}" <<'PYEOF'
"""Render the changelog diff of this publish against the prior SBOM."""
import json
import os
import sys

current_doc = json.load(open(sys.argv[1]))


def flat(doc):
    out = []
    for p in doc.get('packages', []):
        e = p.get('election', {})
        entries = e.get('entries') or [e]
        first = entries[0] if entries else {}
        out.append({
            'package': f"{p['package']}/{p['arch']}",
            'id': first.get('id', ''),
            'url': first.get('url', ''),
            'sha256': first.get('sha256', ''),
            'date': first.get('date', ''),
            'care': first.get('care_delay', 'pass'),
            'flux': first.get('flux', 'pass'),
        })
    return out


current = flat(current_doc)
raw = os.environ.get('PRIOR_SBOM', '')
prev = flat(json.loads(raw)) if raw.strip() else []
prev_by_key = {p['package']: p['id'] for p in prev}
kept = [p for p in current if p['package'] in prev_by_key]
moved = [p for p in kept if prev_by_key[p['package']] != p['id']]
if not prev:
    print('## First published SBOM — every record below is new\n')
    for p in current:
        extra = f", {p['date']}" if p['date'] else ''
        print(f"- **{p['package']}**: {p['id']} "
              f"(care={p['care']} flux={p['flux']}{extra})")
elif not moved:
    print('_No variant moved: every elected id is unchanged._\n')
    print('## Election records (unchanged)\n')
    for p in current:
        print(f"- **{p['package']}**: {p['id']}")
else:
    print('## Variants moved this publish\n')
    for p in moved:
        extra = f", {p['date']}" if p['date'] else ''
        print(f"- **{p['package']}**: {prev_by_key[p['package']]} -> "
              f"{p['id']} (care={p['care']} flux={p['flux']}{extra})")
        print(f"  - url: {p['url'] or 'n/a'}")
        print(f"  - sha256: {p['sha256'] or 'n/a'}")
PYEOF
  echo "CHANGELOG.md written ($(grep -c '^' "${out}") lines)"
}

assemble_sbom
assemble_changelog
