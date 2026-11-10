#!/bin/bash
#
# sibling.sh - PATCH SIBLING ELECTION (owner order 2026-11-10).
#
# A third-party patch that travels as SIBLINGS inside an upstream repository
# (a patch source re-basing per kernel series, or a lore mbox series) is
# elected by THE dry-run, never pinned by hand:
#
#   upstream-track/patch-siblings.yml   the registry (name -> repo, file,
#                                       history kind, manifest/entry)
#
# Commands:
#   probe NAME --version X.Y.Z --tree DIR [--pin] [--write-report FILE]
#
# The probe enumerates the newest commits of the sibling repo (git ls-remote
# / the commits API / a shallow public-inbox mirror walk), fetches the patch
# bytes of each candidate, and dry-runs (fuzz 0) the candidate set against
# the ELECTED kernel tree (upstream-track election). The first candidate —
# newest first — whose whole set applies IS the elected sibling: its blob
# sha256 is the "sha posé" (re-pinned à chaque élection with --pin, a
# script-written proof, never a hand constant). A sibling that does not apply
# is a documented K-PORT verdict, never a reason to pin a kernel series.
#
# Environment:
#   KERNEL_ELECT_CACHE  scratch dir of the probes (default
#                       ${RUNNER_TEMP:-/tmp}/dc-kernel-elect)
#   GH_TOKEN            optional token for the GitHub commits API
#   SIBLINGS_YML        registry override (default beside this script)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SIBLINGS_YML="${SIBLINGS_YML:-${HERE}/patch-siblings.yml}"
CACHE="${KERNEL_ELECT_CACHE:-${RUNNER_TEMP:-/tmp}/dc-kernel-elect}"
AUTH=()
[[ -n "${GH_TOKEN:-}" ]] && AUTH=(-H "Authorization: Bearer ${GH_TOKEN}")

#######################################
# Print one registry field of a sibling.
# Arguments:
#   $1 - sibling name
#   $2 - yq expression relative to the sibling map
#######################################
sib_field() {
  NAME="$1" yq -r '.siblings[] | select(.name == strenv(NAME)) | '"$2" \
    "${SIBLINGS_YML}" | head -n1
}

#######################################
# Print every registry field of a sibling as a list (one value per line).
# Arguments:
#   $1 - sibling name
#   $2 - yq expression relative to the sibling map
#######################################
sib_list() {
  NAME="$1" yq -r '.siblings[] | select(.name == strenv(NAME)) | '"$2" \
    "${SIBLINGS_YML}"
}

#######################################
# Dry-run one patch file (or the file list in order) on the elected tree.
# All-or-nothing fuzz 0; the tree is NEVER modified (dry-run only).
# Arguments:
#   $@ - patch files, in series order
#######################################
dryrun_files() {
  local f
  for f in "$@"; do
    if ! patch -d "${TREE}" -p1 --fuzz=0 --no-backup-if-mismatch \
      --dry-run <"${f}" >/dev/null 2>&1; then
      return 1
    fi
  done
  return 0
}

commit_history() {
  # $1 = https github repo URL, $2 = depth -> newest-first commit shas
  local repo
  repo="${1#https://github.com/}"
  curl -fsSL "${AUTH[@]}" \
    "https://api.github.com/repos/${repo}/commits?per_page=${2}" \
    | yq -r '.[].sha'
}

lore_history() {
  # $1 = public-inbox mirror git URL, $2 = depth -> matching commits,
  # newest first, tab-separated sha subject
  local dir="${CACHE}/lore-mirror"
  if [[ ! -d "${dir}" ]]; then
    rm -rf "${dir}"
    git clone --bare --depth="${2}" -q "$1" "${dir}" 2>/dev/null || return 0
  fi
  GIT_DIR="${dir}" git log --format='%H%x09%s' --extended-regexp \
    --grep="${SUBJECT_RE}" "refs/heads/master"
}

lore_message_id() {
  # $1 = mirror dir (GIT_DIR), $2 = commit -> message-id of the mail blob
  GIT_DIR="$1" git show "$2:m" 2>/dev/null \
    | grep -m1 -iE '^Message-I[Dd]' | sed -E 's/.*<([^>]+)>.*/\1/'
}

usage() {
  echo "Usage: $0 probe NAME --version X.Y.Z --tree DIR [--pin] [--write-report FILE]" >&2
  exit 2
}

[[ "${1:-}" == "probe" ]] || usage
shift
NAME="$1"
shift
VERSION="" TREE=""
PIN=false REPORT="/dev/stdout"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) VERSION="$2"; shift 2 ;;
    --tree) TREE="$2"; shift 2 ;;
    --pin) PIN=true; shift ;;
    --write-report) REPORT="$2"; shift 2 ;;
    *) usage ;;
  esac
done
[[ -n "${VERSION}" && -n "${TREE}" ]] || usage
HISTORY="$(sib_field "${NAME}" '.history // ""')"
MANIFEST_REL="$(sib_field "${NAME}" '.manifest')"
MANIFEST="${HERE}/../${MANIFEST_REL}"
ENTRY="$(sib_field "${NAME}" '.entry')"
REPO="$(sib_field "${NAME}" '.repo')"
DEPTH="$(sib_field "${NAME}" '.depth // 30')"
SUBJECT_RE="$(sib_field "${NAME}" '.subject // ""')"
mkdir -p "${CACHE}"

elected="" elected_ref="" elected_blob=""

if [[ "${HISTORY}" == "github-commits" ]]; then
  FILE_TPL="$(sib_field "${NAME}" '.file')"
  for c in $(commit_history "${REPO}" "${DEPTH}"); do
    while IFS= read -r s; do
      [[ -n "${s}" ]] || continue
      url="https://raw.githubusercontent.com/${REPO#https://github.com/}/${c}/$(printf '%s' "${FILE_TPL}" | sed "s/{series}/${s}/")"
      f="${CACHE}/sib-${c}-${s}.patch"
      if ! curl --retry 3 -fsSL "${url}" -o "${f}"; then
        echo "sibling ${NAME}: no ${s} sibling at commit ${c}" >&2
        continue
      fi
      if dryrun_files "${f}"; then
        elected="${c}"
        elected_ref="commit ${c}"
        elected_blob="$(sha256sum "${f}" | cut -d' ' -f1)"
        elected_pinned_url="${REPO}/${c}/$(printf '%s' "${FILE_TPL}" | sed "s/{series}/${s}/")"
        break 2
      fi
      echo "sibling ${NAME} (${s}, ${c}): dry-run FAIL" >&2
    done < <(sib_list "${NAME}" '.series[]')
  done
elif [[ "${HISTORY}" == "git-log" ]]; then
  dir="$(lore_history "${REPO}" "${DEPTH}")"
  # The sibling SET of the newest series version: every commit whose
  # message carries the same v<N> marker, ordered by its [n/N] index.
  while IFS=$'\t' read -r c subj; do
    [[ -n "${c}" ]] || continue
    mid="$(lore_message_id "${dir}" "${c}")"
    [[ -n "${mid}" ]] || continue
    mbox="${CACHE}/ifpc-${c}.mbox"
    curl --retry 3 -fsSL "${REPO%/0}/${mid}/raw" -o "${mbox}" \
      || { echo "sibling ${NAME}: raw fetch failed for ${mid}" >&2; continue; }
    echo "sibling ${NAME}: commit ${c} = ${subj} (mid ${mid})" >&2
    if dryrun_files "${mbox}"; then
      elected="${c}"
      elected_ref="lore commit ${c} (${subj})"
      elected_blob="$(sha256sum "${mbox}" | cut -d' ' -f1)"
      elected_pinned_url="${REPO%/0}/${mid}/raw"
      break
    fi
  done < <(lore_history "${REPO}" "${DEPTH}")
else
  echo "unknown history kind: ${HISTORY}" >&2
  exit 1
fi

verdict="KO"
VERDICT_KO=""
if [[ -n "${elected}" ]]; then
  verdict="OK"
else
  VERDICT_KO="K-PORT (no sibling of the last ${DEPTH} commits passes the fuzz-0 dry-run against ${VERSION})"
fi

json_verdict() {
  if [[ -n "${elected}" ]]; then
    printf '{"sibling": "%s", "kernel": "%s", "verdict": "OK", "elected": "%s", "ref": "%s", "blob_sha256": "%s", "url": "%s"}\n' \
      "${NAME}" "${VERSION}" "${elected}" "${elected_ref}" "${elected_blob}" "${elected_pinned_url}"
  else
    printf '{"sibling": "%s", "kernel": "%s", "verdict": "K-PORT", "note": "%s"}\n' \
      "${NAME}" "${VERSION}" "${VERDICT_KO}"
  fi
}

if [[ "${PIN}" == true && -n "${elected}" ]]; then
  echo "sibling ${NAME}: re-pinning ${elected_blob} (from ${elected_ref})" >&2
  python3 - "$MANIFEST" "$ENTRY" "$elected_blob" <<'PYEOF'
import sys, re
path, entry, blob = sys.argv[1:4]
text = open(path).read()
# The entry block: from the "- name: <entry>" line to the next entry.
pat = re.compile(r'(\n      - name: %s\n)((?:.*\n)*?)(?=      - name: |\Z)' % re.escape(entry))
m = pat.search(text)
if not m:
    sys.exit(f"entry {entry} not found in {path}")
block = m.group(2)
if 'sha256:' in block:
    block = re.sub(r'sha256: *[0-9a-f]{64}', f'sha256: {blob}', block)
else:
    block = block.rstrip('\n') + f'\n        sha256: {blob}\n'
text = text[:m.start(2)] + block + text[m.end(2):]
open(path, 'w').write(text)
print(f"pinned {blob} into {entry}")
PYEOF
  yq '.patches[] | select(.name == strenv(ENTRY)) | .name' \
    ENTRY="${ENTRY}" "${MANIFEST}" >/dev/null   # sanity: still parses
fi

{
  echo "sibling ${NAME} on kernel ${VERSION}: ${verdict}"
  json_verdict
} > "${REPORT}"
cat "${REPORT}"
